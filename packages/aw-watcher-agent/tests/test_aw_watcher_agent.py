"""Tests for aw-watcher-agent: core conventions, state, and the REST client."""

from __future__ import annotations


import pytest

from aw_watcher_agent import core
from aw_watcher_agent.client import AWClient, AWClientError, Event


# --- core ------------------------------------------------------------------


def test_bucket_id_convention():
    assert core.bucket_id("bob") == "aw-watcher-agent_bob"


def test_session_data_drops_empty_and_adds_outcome():
    args = {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "category": "",  # dropped
        "session_id": "8531",
        "trigger": None,  # dropped
        "workspace": "bob",
    }
    start = core.session_data(args)
    assert start == {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "session_id": "8531",
        "workspace": "bob",
    }
    assert "outcome" not in start
    end = core.session_data(args, outcome="productive")
    assert end["outcome"] == "productive"


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert core.read_state("abc") is None
    core.write_state("abc", {"bucket_id": "b", "event_id": 7, "start": "t"})
    assert core.read_state("abc") == {"bucket_id": "b", "event_id": 7, "start": "t"}
    core.clear_state("abc")
    assert core.read_state("abc") is None


def test_state_path_sanitizes_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = core.state_path("a/b c")
    assert "/" not in path.name
    assert path.name == "session-a_b_c.json"


# --- client (mocked transport) ---------------------------------------------


class _FakeClient(AWClient):
    """AWClient with ``_request`` stubbed by a scripted response queue."""

    def __init__(self, responses):
        super().__init__("http://test")
        self._responses = list(responses)
        self.calls = []

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._responses.pop(0)


def test_ensure_bucket_creates_when_missing():
    c = _FakeClient([(200, {}), (200, None)])  # buckets(), POST create
    assert c.ensure_bucket("aw-watcher-agent_bob", "app.agent.session", "x", "bob") is True
    assert c.calls[1][0] == "POST"


def test_ensure_bucket_idempotent_when_present():
    c = _FakeClient([(200, {"aw-watcher-agent_bob": {}})])
    assert c.ensure_bucket("aw-watcher-agent_bob", "app.agent.session", "x", "bob") is False
    assert len(c.calls) == 1  # no POST


def test_post_event_returns_id_from_dict():
    c = _FakeClient([(200, {"id": 42, "timestamp": "t", "duration": 0})])
    eid = c.post_event("b", Event("t", 0.0, {"harness": "gptme"}))
    assert eid == 42
    assert c.calls[0] == (
        "POST",
        "/api/0/buckets/b/events",
        {"timestamp": "t", "duration": 0.0, "data": {"harness": "gptme"}},
    )


def test_post_event_returns_id_from_list():
    c = _FakeClient([(201, [{"id": 9}])])
    assert c.post_event("b", Event("t", 1.0, {})) == 9


def test_post_event_raises_on_error():
    c = _FakeClient([(500, "boom")])
    with pytest.raises(AWClientError):
        c.post_event("b", Event("t", 0.0, {}))


def test_delete_event():
    c = _FakeClient([(200, None)])
    assert c.delete_event("b", 5) is True
    assert c.calls[0] == ("DELETE", "/api/0/buckets/b/events/5", None)


def test_info_validates_shape():
    c = _FakeClient([(200, {"hostname": "bob"})])
    assert c.info()["hostname"] == "bob"
    c2 = _FakeClient([(200, "not-a-dict")])
    with pytest.raises(AWClientError):
        c2.info()


# --- CLI emit-start / emit-end metadata persistence -------------------------


def test_emit_end_preserves_start_metadata(tmp_path, monkeypatch):
    """emit-end must carry harness/model/workspace even when not re-supplied."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from aw_watcher_agent import cli

    # Simulate what emit-start writes into the state file.
    saved_data = {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "session_id": "abc1",
        "workspace": "bob",
    }
    core.write_state(
        "abc1",
        {
            "bucket_id": "aw-watcher-agent_host",
            "event_id": 42,
            "start": "2026-01-01T00:00:00+00:00",
            "data": saved_data,
        },
    )

    # emit-end is called with only --session-id and --outcome (the typical case).
    import argparse

    args = argparse.Namespace(
        server="http://localhost:5600",
        hostname="host",  # matches the bucket_id saved in state above
        session_id="abc1",
        outcome="productive",
        harness=None,
        model=None,
        category=None,
        trigger=None,
        workspace=None,
        duration=None,
        strict=False,
    )

    posted_events: list[dict] = []

    class _CapturingClient(_FakeClient):
        def __init__(self):
            # ensure_bucket check, delete placeholder, heartbeat for final event
            super().__init__(
                [
                    (200, {"aw-watcher-agent_host": {}}),  # ensure_bucket
                    (200, None),  # delete_event
                    (200, {"id": 99, "timestamp": "t", "duration": 0}),  # heartbeat
                ]
            )

        def heartbeat(self, bid, event, pulsetime):
            posted_events.append(event.data)
            return super().heartbeat(bid, event, pulsetime)

    monkeypatch.setattr(cli, "AWClient", lambda _url: _CapturingClient())
    rc = cli.cmd_emit_end(args)
    assert rc == 0
    assert posted_events, "no heartbeat was posted"
    final_data = posted_events[-1]
    assert final_data.get("harness") == "claude-code", "harness lost from final event"
    assert final_data.get("model") == "claude-opus-4-7", "model lost from final event"
    assert final_data.get("workspace") == "bob", "workspace lost from final event"
    assert final_data.get("outcome") == "productive"
