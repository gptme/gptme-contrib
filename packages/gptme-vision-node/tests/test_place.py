"""Tests for place recognition: embedder, wifi similarity, recognizer."""

from __future__ import annotations

import json

import numpy as np
import pytest
from gptme_vision_node.place import (
    HistogramEmbedder,
    PlaceRecognizer,
    WifiSignature,
    cosine_similarity,
)

from .conftest import scene_frame, solid_frame

KITCHEN = scene_frame(101)
KITCHEN_2 = scene_frame(101).copy()
OFFICE = scene_frame(202)


def _noisy(frame, seed=0, amount=6):
    rng = np.random.default_rng(seed)
    noise = rng.integers(-amount, amount + 1, frame.shape)
    return np.clip(frame.astype(int) + noise, 0, 255).astype(np.uint8)


# -- HistogramEmbedder -----------------------------------------------------


def test_embedder_output_is_normalized():
    vec = HistogramEmbedder().embed(KITCHEN)
    assert vec.ndim == 1
    assert vec.dtype == np.float64
    assert np.linalg.norm(vec) == pytest.approx(1.0)


def test_embedder_handles_frames_smaller_than_grid():
    vec = HistogramEmbedder(grid=2).embed(np.zeros((1, 1, 3), dtype=np.uint8))

    assert vec.shape == ((24 * 8 + 8) * 5,)
    assert np.isfinite(vec).all()


@pytest.mark.parametrize(
    ("kwargs", "parameter"),
    [({"h_bins": 0}, "h_bins"), ({"s_bins": 0}, "s_bins"), ({"grid": 0}, "grid")],
)
def test_embedder_rejects_non_positive_dimensions(kwargs, parameter):
    with pytest.raises(ValueError, match=parameter):
        HistogramEmbedder(**kwargs)


def test_embedder_discriminates_scenes():
    emb = HistogramEmbedder()
    kitchen = emb.embed(KITCHEN)
    kitchen_again = emb.embed(_noisy(KITCHEN, seed=7))
    office = emb.embed(OFFICE)

    same = cosine_similarity(kitchen, kitchen_again)
    different = cosine_similarity(kitchen, office)
    assert same > 0.95, f"near-identical scenes should score high, got {same}"
    assert same > different + 0.1, f"distinct scenes too close: {same} vs {different}"


def test_embedder_spatial_grid_matters():
    """Same colors, different layout -> distinguishable via the grid cells."""
    emb = HistogramEmbedder()
    left = solid_frame((0, 0, 0))
    left[:, :160] = (0, 0, 255)  # red on the left
    right = solid_frame((0, 0, 0))
    right[:, 160:] = (0, 0, 255)  # red on the right
    sim = cosine_similarity(emb.embed(left), emb.embed(right))
    assert sim < 0.999  # global histograms alone would be ~identical


# -- WifiSignature ---------------------------------------------------------


def test_wifi_parse_nmcli():
    out = "AA\\:BB\\:CC\\:DD\\:EE\\:FF:82\n11\\:22\\:33\\:44\\:55\\:66:47\n\nbad-line\n"
    sig = WifiSignature.parse_nmcli(out)
    assert sig == {"AA:BB:CC:DD:EE:FF": 82.0, "11:22:33:44:55:66": 47.0}


def test_wifi_similarity_math():
    a = {"AA": 80.0, "BB": 60.0, "CC": 40.0}
    assert WifiSignature.similarity(a, dict(a)) == pytest.approx(1.0)
    assert WifiSignature.similarity(a, {"XX": 80.0, "YY": 60.0}) == 0.0
    assert WifiSignature.similarity(a, {}) == 0.0
    assert WifiSignature.similarity({}, {}) == 0.0

    # Partial overlap with similar strengths beats disjoint, loses to identical.
    partial = WifiSignature.similarity(a, {"AA": 78.0, "BB": 62.0})
    assert 0.0 < partial < 1.0

    # Same APs but very different strengths scores lower than same strengths.
    shifted = WifiSignature.similarity(a, {"AA": 10.0, "BB": 5.0, "CC": 99.0})
    assert shifted < WifiSignature.similarity(a, dict(a))


def test_wifi_scan_graceful_on_failure(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("nmcli not installed")

    monkeypatch.setattr(subprocess, "run", boom)
    assert WifiSignature.scan() == {}


# -- PlaceRecognizer -------------------------------------------------------

KITCHEN_WIFI = {"AA": 80.0, "BB": 60.0}
OFFICE_WIFI = {"CC": 70.0, "DD": 50.0}


def test_recognize_vision_only():
    rec = PlaceRecognizer()
    rec.enroll("kitchen", KITCHEN)
    rec.enroll("office", OFFICE)
    results = rec.recognize(_noisy(KITCHEN, seed=3))
    assert results[0][0] == "kitchen"
    assert results[0][1] > results[-1][1]


def test_recognize_wifi_only():
    rec = PlaceRecognizer()
    rec.enroll("kitchen", wifi_sig=KITCHEN_WIFI)
    rec.enroll("office", wifi_sig=OFFICE_WIFI)
    results = rec.recognize(wifi_sig={"AA": 78.0, "BB": 63.0})
    assert results[0][0] == "kitchen"
    assert results[0][1] > 0.5


def test_recognize_combined_wifi_disambiguates():
    """Two visually similar places separated by their wifi fingerprints."""
    rec = PlaceRecognizer(wifi_weight=0.5)
    rec.enroll("kitchen", KITCHEN, wifi_sig=KITCHEN_WIFI)
    rec.enroll("kitchen-clone", KITCHEN_2, wifi_sig=OFFICE_WIFI)
    results = dict(rec.recognize(_noisy(KITCHEN, seed=5), wifi_sig=KITCHEN_WIFI))
    assert results["kitchen"] > results["kitchen-clone"]


def test_multiple_samples_per_place():
    rec = PlaceRecognizer()
    rec.enroll("kitchen", KITCHEN)
    rec.enroll("kitchen", _noisy(KITCHEN, seed=11))
    assert len(rec.places["kitchen"]) == 2
    assert rec.recognize(KITCHEN)[0][0] == "kitchen"


def test_enroll_requires_signal():
    rec = PlaceRecognizer()
    with pytest.raises(ValueError):
        rec.enroll("nowhere")
    with pytest.raises(ValueError):
        rec.recognize()


def test_save_load_roundtrip(tmp_path):
    gallery = tmp_path / "places.json"
    rec = PlaceRecognizer(wifi_weight=0.3)
    rec.enroll("kitchen", KITCHEN, wifi_sig=KITCHEN_WIFI)
    rec.enroll("office", OFFICE, wifi_sig=OFFICE_WIFI)
    rec.enroll("hallway", wifi_sig={"EE": 40.0})  # wifi-only sample
    rec.save(gallery)

    loaded = PlaceRecognizer.from_file(gallery)
    assert loaded.wifi_weight == pytest.approx(0.3)
    assert set(loaded.places) == {"kitchen", "office", "hallway"}
    assert loaded.places["hallway"][0].embedding is None

    original = rec.recognize(_noisy(KITCHEN, seed=9), wifi_sig=KITCHEN_WIFI)
    reloaded = loaded.recognize(_noisy(KITCHEN, seed=9), wifi_sig=KITCHEN_WIFI)
    assert [name for name, _ in original] == [name for name, _ in reloaded]
    for (_, s1), (_, s2) in zip(original, reloaded):
        assert s1 == pytest.approx(s2)


def test_load_restores_wifi_weight(tmp_path):
    gallery = tmp_path / "places.json"
    saved = PlaceRecognizer(wifi_weight=0.7)
    saved.enroll("kitchen", KITCHEN, wifi_sig=KITCHEN_WIFI)
    saved.save(gallery)

    loaded = PlaceRecognizer(wifi_weight=0.2)
    loaded.load(gallery)

    assert loaded.wifi_weight == pytest.approx(0.7)


def test_from_file_explicit_wifi_weight_overrides_saved_value(tmp_path):
    gallery = tmp_path / "places.json"
    saved = PlaceRecognizer(wifi_weight=0.7)
    saved.enroll("kitchen", KITCHEN, wifi_sig=KITCHEN_WIFI)
    saved.save(gallery)

    loaded = PlaceRecognizer.from_file(gallery, wifi_weight=0.2)

    assert loaded.wifi_weight == pytest.approx(0.2)


@pytest.mark.parametrize("payload", ["[]", '"gallery"', "null"])
def test_load_rejects_non_object_payload(tmp_path, payload):
    gallery = tmp_path / "places.json"
    gallery.write_text(payload)

    with pytest.raises(ValueError, match="must contain a JSON object"):
        PlaceRecognizer().load(gallery)
    with pytest.raises(ValueError, match="must contain a JSON object"):
        PlaceRecognizer.from_file(gallery)


@pytest.mark.parametrize("places", [[], "kitchen", None])
def test_load_rejects_non_object_places(tmp_path, places):
    gallery = tmp_path / "places.json"
    gallery.write_text(json.dumps({"places": places}))

    with pytest.raises(ValueError, match="places must be a JSON object"):
        PlaceRecognizer().load(gallery)


@pytest.mark.parametrize(
    "samples",
    [{"embedding": [1.0]}, [1], ["sample"], [[1.0]]],
)
def test_load_rejects_malformed_place_samples(tmp_path, samples):
    gallery = tmp_path / "places.json"
    gallery.write_text(json.dumps({"places": {"kitchen": samples}}))

    with pytest.raises(ValueError, match="samples must be a list of JSON objects"):
        PlaceRecognizer().load(gallery)


@pytest.mark.parametrize("wifi", ["kitchen-ap", [1, 2], 3])
def test_load_rejects_non_object_wifi(tmp_path, wifi):
    gallery = tmp_path / "places.json"
    gallery.write_text(
        json.dumps({"places": {"kitchen": [{"embedding": [1.0], "wifi": wifi}]}})
    )

    with pytest.raises(ValueError, match="sample wifi must be a JSON object"):
        PlaceRecognizer().load(gallery)
    with pytest.raises(ValueError, match="sample wifi must be a JSON object"):
        PlaceRecognizer.from_file(gallery)


class CustomEmbedder:
    embedder_id = "test-custom-v1"

    def embed(self, frame):
        return np.asarray([1.0, 0.0])


def test_from_file_rejects_mismatched_embedder(tmp_path):
    gallery = tmp_path / "places.json"
    rec = PlaceRecognizer(CustomEmbedder())
    rec.enroll("kitchen", KITCHEN)
    rec.save(gallery)

    with pytest.raises(ValueError, match="embedder mismatch"):
        PlaceRecognizer.from_file(gallery)

    loaded = PlaceRecognizer.from_file(gallery, CustomEmbedder())
    assert set(loaded.places) == {"kitchen"}


# -- brightness (gptme-contrib#2381) -----------------------------------------


def test_embedder_separates_black_gray_white():
    emb = HistogramEmbedder()
    black, gray, white = (
        emb.embed(solid_frame(c)) for c in ((0, 0, 0), (128, 128, 128), (255,) * 3)
    )
    # Hue/saturation alone put all three into one bin (cosine exactly 1.0).
    assert cosine_similarity(black, white) < 0.6
    assert cosine_similarity(black, gray) < 0.6
    assert cosine_similarity(gray, white) < 0.6


def test_lens_capped_frame_does_not_match_bright_room_at_full_confidence():
    rec = PlaceRecognizer()
    rec.enroll("white-office", solid_frame((245, 245, 245)))
    rec.enroll("dark-hallway", solid_frame((25, 25, 25)))
    results = rec.recognize(solid_frame((0, 0, 0)))
    assert results[0][0] == "dark-hallway"
    scores = dict(results)
    assert scores["white-office"] < 0.6


def test_embedder_id_includes_brightness_and_legacy_v1_is_kept():
    assert HistogramEmbedder().embedder_id == "histogram-h24-s8-val8-grid2-v2"
    legacy = HistogramEmbedder(v_bins=0)
    assert legacy.embedder_id == "histogram-h24-s8-grid2-v1"
    assert legacy.embed(solid_frame((1, 2, 3))).shape == (24 * 8 * 5,)


def test_embedder_rejects_negative_v_bins():
    with pytest.raises(ValueError, match="v_bins"):
        HistogramEmbedder(v_bins=-1)


# -- atomic save (gptme-contrib#2381) ----------------------------------------


def test_save_through_symlink_updates_target_and_preserves_link(tmp_path):
    target = tmp_path / "shared" / "gallery.json"
    rec = PlaceRecognizer()
    rec.enroll("kitchen", KITCHEN)
    rec.save(target)
    link = tmp_path / "places.json"
    link.symlink_to("shared/gallery.json")

    loaded = PlaceRecognizer.from_file(link)
    loaded.enroll("office", OFFICE)
    loaded.save(link)

    assert link.is_symlink()
    assert set(PlaceRecognizer.from_file(target).places) == {"kitchen", "office"}
    assert link.read_bytes() == target.read_bytes()
    assert list(target.parent.glob("*.tmp")) == []


def test_save_failure_keeps_existing_gallery(tmp_path, monkeypatch):
    path = tmp_path / "places.json"
    rec = PlaceRecognizer()
    rec.enroll("kitchen", KITCHEN)
    rec.save(path)
    before = path.read_text()

    rec.enroll("office", OFFICE)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("gptme_vision_node.place.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        rec.save(path)

    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["places.json"]
