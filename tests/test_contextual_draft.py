"""Real local HTTP/subprocess fixtures; no model or personal archive required."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "contextual_draft.py"
spec = importlib.util.spec_from_file_location("contextual_draft", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TARGET = {
    "schema_version": 1,
    "role": "AXTextArea",
    "secure": False,
    "editable": True,
    "text": "Can we meet?",
}


@pytest.fixture
def daemon(tmp_path):
    calls = []
    state = {"service": "goldfish_d", "redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
                return
            payload: Any
            if self.path == "/health":
                payload = {"service": state["service"], "status": "ok"}
            elif self.path.startswith("/search?"):
                payload = [
                    {"snapshot_id": "s1", "content_excerpt": "not full evidence"}
                ]
            else:
                payload = [
                    {
                        "snapshot_id": "s1",
                        "content": "A synthetic conversation",
                        "captured_at": "2026-01-01T12:00:00Z",
                    }
                ]
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "goldfish"
    home.mkdir()
    (home / "effective-port").write_text(str(server.server_port))
    (home / "api-token").write_text("synthetic-test-token")
    try:
        yield home, calls, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_health_before_auth_and_full_snapshot_fetch(daemon):
    home, calls, _ = daemon
    rows = module.Goldfish(home).snapshots("meeting", 1)
    assert calls[0] == ("/health", None)
    assert calls[1][0].startswith("/search?")
    assert calls[2][0] == "/snapshots?ids=s1"
    assert all(auth == "Bearer synthetic-test-token" for _, auth in calls[1:])
    assert rows[0]["content"] == "A synthetic conversation"
    assert rows[0]["is_live_target"] is False


def test_wrong_service_never_receives_token(daemon):
    home, calls, state = daemon
    state["service"] = "another_service"
    with pytest.raises(module.DraftError, match="no token sent"):
        module.Goldfish(home)
    assert calls == [("/health", None)]


def test_redirect_is_refused(daemon):
    home, calls, state = daemon
    client = module.Goldfish(home)
    state["redirect"] = True
    with pytest.raises(module.DraftError, match="redirected"):
        client.snapshots(None, 1)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "update",
    [
        {"secure": True},
        {"secure": None},
        {"editable": False},
        {"schema_version": 2},
        {"text": None},
    ],
)
def test_unsafe_target_refused(update):
    with pytest.raises(module.DraftError):
        module.validate_target(TARGET | update)


def test_excerpts_are_not_full_evidence():
    with pytest.raises(module.DraftError):
        module.normalize_snapshot({"snapshot_id": "s", "content_excerpt": "partial"})


def test_truncation_is_explicit():
    row = module.normalize_snapshot({"snapshot_id": "s", "content": "x" * 15000})
    assert row["content_truncated"] and len(row["content"]) == 12000


def test_private_output_no_overwrite_or_symlink(tmp_path):
    path = tmp_path / "request.json"
    module.private_write(path, "private")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(module.DraftError):
        module.private_write(path, "replacement")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(module.DraftError):
        module.private_write(link, "replacement")
    assert path.read_text() == "private"


def test_prepare_cli_combines_explicit_context(daemon, tmp_path):
    home, _, _ = daemon
    target = tmp_path / "target.json"
    target.write_text(json.dumps(TARGET))
    context = tmp_path / "preferences.txt"
    context.write_text("Prefer meetings after lunch.")
    out = tmp_path / "request.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "prepare",
            "--target",
            str(target),
            "--context",
            str(context),
            "--goldfish-home",
            str(home),
            "--instruction",
            "Draft a reply",
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    request = json.loads(out.read_text())
    assert request["agent_context"][0]["content"] == context.read_text()
    assert len(request["observations"]) == 1
    assert "Prefer meetings" not in proc.stdout


def test_generate_requires_disclosure_flag(tmp_path):
    args = argparse.Namespace(allow_model_disclosure=False)
    with pytest.raises(module.DraftError, match="allow-model-disclosure"):
        module.generate(args)


def test_generation_subprocess_contract(tmp_path):
    # A tiny executable fixture, not an actual provider call. Verifies stdin,
    # model selection, clean output and error handling without inference spend.
    exe = tmp_path / "gptme"
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys,json\nassert sys.argv[1:3] == ['llm','generate']\nassert '--model' in sys.argv\np=sys.stdin.read()\nassert 'Prefer meetings after lunch' in p\nprint('How about after lunch?')\n"
    )
    exe.chmod(0o700)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instruction": "Reply",
                "target": TARGET,
                "agent_context": [{"content": "Prefer meetings after lunch"}],
            }
        )
    )
    output = tmp_path / "draft.txt"
    env = os.environ | {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    cmd = [
        sys.executable,
        str(SCRIPT),
        "generate",
        "--request",
        str(request),
        "--model",
        "test/synthetic",
        "--allow-model-disclosure",
        "--output",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == "How about after lunch?\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "after lunch" not in proc.stdout
    assert subprocess.run(cmd, capture_output=True, env=env).returncode != 0
