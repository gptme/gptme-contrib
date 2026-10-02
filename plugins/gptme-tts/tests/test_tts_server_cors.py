"""Exercise browser requests without loading a speech model."""

import importlib.util
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient


@pytest.fixture
def server(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "tts_cors_server", Path(__file__).resolve().parent.parent / "tts_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TTS_CORS_ORIGIN", raising=False)
    monkeypatch.setattr(
        module.TTSBackendLoader, "get_available_backends", lambda: ["kokoro"]
    )
    module.current_backend = SimpleNamespace(
        synthesize=lambda **kwargs: BytesIO(b"RIFFtest-audio")
    )
    return module


def start_server(server, monkeypatch, *args):
    """Capture the actual ASGI app passed by the CLI, without starting uvicorn."""
    apps = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: apps.append(app))
    result = CliRunner().invoke(server.main, list(args))
    assert result.exit_code == 0, result.output
    return TestClient(apps[0])  # No context manager: do not start model lifespan.


@pytest.mark.parametrize("origin", ["http://localhost:5173", "https://chat.gptme.org"])
def test_allowed_origin_preflight_and_audio(server, monkeypatch, origin):
    client = start_server(
        server,
        monkeypatch,
        "--cors-origin",
        " https://chat.gptme.org, http://localhost:5173 ",
    )
    preflight = client.options(
        "/tts",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert preflight.headers["access-control-allow-methods"] == "GET"

    audio = client.get("/tts", params={"text": "Hello"}, headers={"Origin": origin})
    assert audio.status_code == 200
    assert audio.content == b"RIFFtest-audio"
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.headers["access-control-allow-origin"] == origin
    assert "Origin" in audio.headers["vary"]


@pytest.mark.parametrize(
    ("params", "status"),
    [({"text": " "}, 400), ({"text": "Hello", "speed": "invalid"}, 422)],
)
def test_cors_on_validation_errors(server, monkeypatch, params, status):
    client = start_server(
        server, monkeypatch, "--cors-origin", "https://chat.gptme.org"
    )
    response = client.get(
        "/tts", params=params, headers={"Origin": "https://chat.gptme.org"}
    )
    assert response.status_code == status
    assert response.headers["access-control-allow-origin"] == "https://chat.gptme.org"


def test_cors_on_backend_error(server, monkeypatch):
    client = start_server(
        server, monkeypatch, "--cors-origin", "https://chat.gptme.org"
    )
    server.current_backend = None
    response = client.get(
        "/tts?text=Hello", headers={"Origin": "https://chat.gptme.org"}
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Backend not initialized"}
    assert response.headers["access-control-allow-origin"] == "https://chat.gptme.org"


def test_disallowed_origin(server, monkeypatch):
    client = start_server(
        server, monkeypatch, "--cors-origin", "https://chat.gptme.org"
    )
    origin = "https://chat.gptme.org.evil.example"
    response = client.options(
        "/tts",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    response = client.get("/", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers


def test_cors_disabled_by_default(server, monkeypatch):
    client = start_server(server, monkeypatch)
    response = client.options(
        "/tts",
        headers={
            "Origin": "https://chat.gptme.org",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers
    assert client.get("/").status_code == 200


def test_cors_env_and_cli_override(server, monkeypatch):
    monkeypatch.setenv("TTS_CORS_ORIGIN", "https://chat.gptme.org")
    client = start_server(server, monkeypatch)
    assert (
        client.get("/", headers={"Origin": "https://chat.gptme.org"}).headers[
            "access-control-allow-origin"
        ]
        == "https://chat.gptme.org"
    )
    client = start_server(server, monkeypatch, "--cors-origin", "http://localhost:5173")
    assert (
        "access-control-allow-origin"
        not in client.get("/", headers={"Origin": "https://chat.gptme.org"}).headers
    )
    assert (
        client.get("/", headers={"Origin": "http://localhost:5173"}).headers[
            "access-control-allow-origin"
        ]
        == "http://localhost:5173"
    )


def test_wildcard_cors(server, monkeypatch):
    client = start_server(server, monkeypatch, "--cors-origin", "*")
    response = client.options(
        "/tts",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    response = client.get("/", headers={"Origin": "https://example.com"})
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
