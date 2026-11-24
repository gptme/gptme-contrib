"""Regression tests for the 2026-11-24 verified bug hunt (error handling / data loss)."""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aw_watcher_agent import cli, core
from aw_watcher_agent.client import AWClient


class FakeAW:
    """Minimal in-memory aw-server with failure injection."""

    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str]] = []
        self.next_id = 1
        self.fail_heartbeat_after: int | None = None  # fail heartbeats once N succeeded
        self.heartbeats_ok = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _parts(self):
                u = urllib.parse.urlsplit(self.path)
                segs = [urllib.parse.unquote(s) for s in u.path.split("/")]
                return segs, urllib.parse.parse_qs(u.query)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.requests.append(("GET", self.path))
                segs, _ = self._parts()
                if segs[:4] == ["", "api", "0", "buckets"]:
                    return self._send(200, fake.buckets)
                return self._send(404, {"message": "nf"})

            def do_POST(self):
                fake.requests.append(("POST", self.path))
                segs, q = self._parts()
                body = self._body()
                # /api/0/buckets/<id>[/events|/heartbeat]
                if segs[:4] != ["", "api", "0", "buckets"] or len(segs) < 5:
                    return self._send(404, {"message": "nf"})
                bid = segs[4]
                rest = segs[5:]
                if not rest:
                    if bid in fake.buckets:
                        return self._send(304)
                    if not isinstance(body, dict) or "type" not in body:
                        return self._send(400, {"message": "bad bucket"})
                    fake.buckets[bid] = {"id": bid, **body}
                    fake.events[bid] = []
                    return self._send(200)
                if bid not in fake.buckets:
                    return self._send(404, {"message": "no bucket"})
                if rest == ["heartbeat"]:
                    if (
                        fake.fail_heartbeat_after is not None
                        and fake.heartbeats_ok >= fake.fail_heartbeat_after
                    ):
                        return self._send(500, {"message": "transient"})
                    fake.heartbeats_ok += 1
                    ev = {"id": fake.next_id, **body}
                    fake.next_id += 1
                    fake.events[bid].append(ev)
                    return self._send(200, ev)
                if rest == ["events"]:
                    evs = body if isinstance(body, list) else [body]
                    for e in evs:
                        fake.events[bid].append({"id": fake.next_id, **e})
                        fake.next_id += 1
                    return self._send(200, None)
                return self._send(404, {"message": "nf"})

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path))
                segs, _ = self._parts()
                bid, eid = segs[4], int(segs[6])
                before = len(fake.events.get(bid, []))
                fake.events[bid] = [e for e in fake.events.get(bid, []) if e["id"] != eid]
                return self._send(200 if len(fake.events[bid]) < before else 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def aw():
    f = FakeAW()
    yield f
    f.close()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


# ---------------------------------------------------------------------------
# AE1: bucket ids (from hostname) are interpolated into the URL unquoted.
# ---------------------------------------------------------------------------


def test_ae1_hostname_with_hash_goes_to_wrong_bucket(aw):
    rc = cli.main(["emit-start", "--server", aw.url, "--hostname", "box#1", "--session-id", "s1"])
    assert rc == 0
    expected = "aw-watcher-agent_box#1"
    # The session must land in the bucket named after the given hostname.
    assert expected in aw.buckets, f"buckets created: {list(aw.buckets)}"
    assert len(aw.events[expected]) == 1


# ---------------------------------------------------------------------------
# AE2: http.client.HTTPException (not OSError) escapes main()'s non-fatal guard.
# ---------------------------------------------------------------------------


def _garbage_server():
    """Something that is not an HTTP server listening on the aw port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def run():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                conn.recv(65536)
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            finally:
                conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv, stop, f"http://127.0.0.1:{srv.getsockname()[1]}"


def test_ae2_non_http_listener_crashes_hook_cli():
    srv, stop, url = _garbage_server()
    try:
        # Documented contract: errors go to stderr and the CLI exits 0.
        rc = cli.main(["emit-start", "--server", url, "--session-id", "s1"])
        assert rc == 0
    finally:
        stop.set()
        srv.close()


def test_ae2_hostname_with_space_crashes_hook_cli(aw):
    rc = cli.main(["emit-end", "--server", aw.url, "--hostname", "my host", "--session-id", "s"])
    assert rc == 0


# ---------------------------------------------------------------------------
# AE3: emit-end deletes the placeholder before posting the final event, so a
# transient failure of the final post erases every record of the session.
# ---------------------------------------------------------------------------


def test_ae3_emit_end_transient_failure_loses_session(aw):
    host = "h1"
    bid = core.bucket_id(host)
    assert (
        cli.main(
            [
                "emit-start",
                "--server",
                aw.url,
                "--hostname",
                host,
                "--session-id",
                "s1",
                "--harness",
                "claude-code",
            ]
        )
        == 0
    )
    assert len(aw.events[bid]) == 1  # placeholder present

    aw.fail_heartbeat_after = aw.heartbeats_ok  # next heartbeat returns 500
    rc = cli.main(
        [
            "emit-end",
            "--server",
            aw.url,
            "--hostname",
            host,
            "--session-id",
            "s1",
            "--outcome",
            "productive",
        ]
    )
    assert rc == 0  # non-fatal, as designed
    # The session must still be recorded somewhere (placeholder or final event).
    assert len(aw.events[bid]) >= 1, "session vanished from the timeline"


# ---------------------------------------------------------------------------
# AE4: tail-codex only writes its cursor after the whole loop; a mid-loop
# failure makes the next run re-send already-accepted heartbeats (duplicates).
# ---------------------------------------------------------------------------


def _rollout(n):
    lines = [
        json.dumps(
            {"timestamp": "2026-11-24T10:00:00Z", "type": "session_meta", "payload": {"id": "sess"}}
        )
    ]
    for i in range(n):
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-11-24T10:{i:02d}:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": f"tool{i}",
                        "call_id": f"c{i}",
                        "arguments": "{}",
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-11-24T10:{i:02d}:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"c{i}",
                        "output": "Process exited with code 0",
                    },
                }
            )
        )
    return "\n".join(lines) + "\n"


def test_ae4_tail_codex_partial_failure_duplicates_on_retry(aw, tmp_path):
    f = tmp_path / "rollout-x.jsonl"
    f.write_text(_rollout(3))
    host = "h1"
    aw.fail_heartbeat_after = 2  # third heartbeat fails transiently
    assert cli.main(["tail-codex", "--server", aw.url, "--hostname", host, "--file", str(f)]) == 0
    aw.fail_heartbeat_after = None  # server recovered
    assert cli.main(["tail-codex", "--server", aw.url, "--hostname", host, "--file", str(f)]) == 0
    tools = [e["data"]["tool"] for e in aw.events[core.activity_bucket_id(host)]]
    assert sorted(tools) == ["tool0", "tool1", "tool2"], f"events stored: {tools}"


def test_emit_end_keeps_final_event_merged_into_placeholder(tmp_path, monkeypatch):
    """If the final heartbeat merges into the placeholder, do not delete it."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    deleted: list[int] = []

    class _Stub(AWClient):
        def ensure_bucket(self, *a, **k):
            return False

        def heartbeat(self, bucket_id, event, pulsetime):
            return 7

        def delete_event(self, bucket_id, event_id):
            deleted.append(event_id)
            return True

    monkeypatch.setattr(cli, "AWClient", _Stub)
    assert cli.main(["emit-start", "--hostname", "h1", "--session-id", "s1"]) == 0
    assert cli.main(["emit-end", "--hostname", "h1", "--session-id", "s1"]) == 0
    assert deleted == []
