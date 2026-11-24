"""A gallery with a wrong-shaped embedding passes load() and crashes whereami."""

import json

import cv2
import numpy as np
from click.testing import CliRunner
from gptme_vision_node import cli
from gptme_vision_node.place import HistogramEmbedder, PlaceRecognizer


def _img(tmp_path):
    p = tmp_path / "a.png"
    f = np.zeros((240, 320, 3), dtype=np.uint8)
    f[:, :160] = (0, 0, 255)
    assert cv2.imwrite(str(p), f)
    return p


def _gallery(tmp_path, embedding):
    g = tmp_path / "places.json"
    g.write_text(
        json.dumps(
            {
                "version": 1,
                "embedder": HistogramEmbedder().embedder_id,
                "wifi_weight": 0.5,
                "places": {"kitchen": [{"embedding": embedding, "wifi": {}}]},
            }
        )
    )
    return g


def _run(tmp_path, embedding):
    img = _img(tmp_path)
    g = _gallery(tmp_path, embedding)
    return CliRunner().invoke(
        cli.main, ["whereami", "--source", str(img), "--gallery", str(g), "--no-wifi"]
    )


def test_truncated_embedding_is_clean_invalid_gallery_error(tmp_path):
    result = _run(tmp_path, [0.1, 0.2, 0.3])
    assert result.exit_code != 0
    assert not (
        result.exception and not isinstance(result.exception, SystemExit)
    ), f"uncaught {type(result.exception).__name__}: {result.exception}"
    assert "invalid gallery" in result.output


def test_scalar_embedding_is_clean_invalid_gallery_error(tmp_path):
    result = _run(tmp_path, 5)
    assert not (
        result.exception and not isinstance(result.exception, SystemExit)
    ), f"uncaught {type(result.exception).__name__}: {result.exception}"
    assert "invalid gallery" in result.output


def test_load_rejects_wrong_dimension_embedding(tmp_path):
    g = _gallery(tmp_path, [0.1, 0.2, 0.3])
    rec = PlaceRecognizer()
    try:
        rec.load(g)
    except ValueError:
        return
    raise AssertionError("load() accepted a 3-dim embedding for a 960-dim embedder")
