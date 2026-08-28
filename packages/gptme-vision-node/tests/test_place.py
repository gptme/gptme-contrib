"""Tests for place recognition: embedder, wifi similarity, recognizer."""

from __future__ import annotations

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
