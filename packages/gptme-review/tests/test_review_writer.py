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


# --- writer-port slice 2b: threads, suppression and dispositions ---------------


def _stamp(ns: str, fp: str) -> str:
    return f'Settled.\n\n<!-- {ns}-disposition {{"fp": "{fp}"}} -->'


def test_unbound_disposition_helpers_refuse():
    with pytest.raises(RuntimeError, match="no host bound"):
        writer.disposition_prefix()


def test_disposition_markers_use_the_bound_identity(alice_io):
    w = writer.bind_io(alice_io)
    assert w.disposition_prefix() == "<!-- alice-ai-review-disposition "
    assert w.inline_finding_marker() == "<!-- alice-ai-review-finding -->"
    assert w.disposition_note_markers() == (
        "<!-- alice-ai-review-dispositions -->",
        "<!-- /alice-ai-review-dispositions -->",
    )


def test_dispositions_read_only_the_bound_namespace(alice_io):
    w = writer.bind_io(alice_io)
    comments = [
        {"id": 1, "in_reply_to_id": 10, "body": _stamp("alice-ai-review", "a1")},
        {"id": 2, "in_reply_to_id": 11, "body": _stamp("bob-ai-review", "b1")},
        # a finding root is never its own disposition
        {"id": 3, "body": _stamp("alice-ai-review", "root")},
    ]
    assert w.disposed_fingerprints(comments) == {"a1"}


def test_machine_comment_is_keyed_on_the_bound_marker(alice_io):
    w = writer.bind_io(alice_io)
    assert w._is_machine_comment("<!-- alice-ai-review {} -->\nbody")
    assert not w._is_machine_comment("<!-- bob-ai-review {} -->\nbody")
    assert not w._is_machine_comment("> <!-- alice-ai-review {} -->")


def test_disposition_note_splices_idempotently(alice_io):
    w = writer.bind_io(alice_io)
    body = "## 🤖 AI code review\n\nfindings"
    first = w.render_disposed_body(body, w.disposition_note("5/5", "| t |"))
    second = w.render_disposed_body(first, w.disposition_note("4/5", "| u |"))
    start, end = w.disposition_note_markers()
    assert second.count(start) == 1 and second.count(end) == 1
    assert "| u |" in second and "| t |" not in second


def test_host_seams_reach_calls_inside_the_writer(alice_io):
    calls: list[tuple] = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_comments(repo, number):
        calls.append(("comments", repo, number))
        return []

    ns = SimpleNamespace(
        **{**vars(alice_io), "run": fake_run, "fetch_review_comments": fake_comments}
    )
    w = writer.bind_io(ns)
    assert w.resolve_review_thread("T_1", repo="o/r") is True
    assert calls[0][1]["BOB_GRAPHQL_MUTATION_REPO"] == "o/r"
    assert w.dismissed_findings("o/r", 7, threads=[]) == []
    assert ("comments", "o/r", 7) in calls
