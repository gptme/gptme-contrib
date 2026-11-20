"""Peer-host contract for gptme_review.writer (writer-port slice 2a)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from gptme_review import writer
from gptme_review.sweep import sweep_identity


@pytest.fixture
def alice_io():
    return writer.default_io(sweep_identity("alice-agent", "alice-ai-review"))


def test_unbound_writer_refuses_instead_of_defaulting_to_an_identity():
    with pytest.raises(RuntimeError, match="no host bound"):
        writer.marker_body("body", {"sha": "abc"})


def test_markers_use_the_bound_identity(alice_io):
    w = writer.bind_io(alice_io)
    body = w.marker_body("review", {"sha": "abc"})
    assert '<!-- alice-ai-review {"sha": "abc"} -->' in body
    assert w.marker_state(body) == {"sha": "abc"}
    assert w.marker_state(body.replace("alice-ai-review", "bob-ai-review")) == {}
    start, end = w.unreviewable_note_markers()
    assert start == "<!-- alice-ai-review-unreviewable -->"
    note = w.render_unreviewable_body(body, "diff_too_large", "too big")
    assert note.count(start) == 1 and end in note
    assert "alice-ai-review {" not in note  # the old marker JSON is stripped


def test_find_marker_comment_reads_through_the_bound_io(alice_io):
    me = {"login": "Alice-Agent"}  # GitHub logins compare case-insensitively
    comments = [
        {
            "id": 1,
            "body": "<!-- alice-ai-review {} -->",
            "updated_at": "2026-01-01",
            "user": me,
        },
        {
            "id": 2,
            "body": "<!-- alice-ai-review {} -->",
            "updated_at": "2026-02-01",
            "user": me,
        },
        {
            "id": 3,
            "body": "<!-- bob-ai-review {} -->",
            "updated_at": "2026-03-01",
            "user": me,
        },
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    alice_io.gh_paginated_list = lambda args: comments
    alice_io.run = fake_run
    keeper = writer.bind_io(alice_io).find_marker_comment("o/r", 7)
    assert keeper is not None and keeper["id"] == 2
    assert calls == [["gh", "api", "--method", "DELETE", "repos/o/r/issues/comments/1"]]


def test_copied_marker_from_another_author_is_ignored_not_deleted(alice_io):
    real = {
        "id": 1,
        "body": "<!-- alice-ai-review {} -->",
        "updated_at": "2026-01-01",
        "user": {"login": "alice-agent"},
    }
    copied = {
        "id": 2,
        "body": "<!-- alice-ai-review {} -->",
        "updated_at": "2026-02-01",
        "user": {"login": "mallory"},
    }
    calls: list[list[str]] = []
    alice_io.gh_paginated_list = lambda args: [real, copied]
    alice_io.run = lambda cmd, **kw: calls.append(cmd)
    keeper = writer.bind_io(alice_io).find_marker_comment("o/r", 7)
    assert keeper is not None and keeper["id"] == 1
    assert calls == []


def test_two_hosts_resolve_independently_and_at_call_time():
    a = SimpleNamespace(MARKER_NAMESPACE="a", MARKER_PREFIX="<!-- a")  # no I/O needed
    b = SimpleNamespace(MARKER_NAMESPACE="b", MARKER_PREFIX="<!-- b")
    wa, wb = writer.bind_io(a), writer.bind_io(b)
    assert wa.marker_body("x", {}).endswith("<!-- a {} -->")
    assert wb.marker_body("x", {}).endswith("<!-- b {} -->")
    a.MARKER_PREFIX = "<!-- a2"  # a host patching its namespace is seen next call
    assert wa.marker_body("x", {}).endswith("<!-- a2 {} -->")
