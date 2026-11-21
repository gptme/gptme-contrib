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


# --- writer-port slice 2c: diff shaping, notes and render ---------------------

_SUBMODULE_DIFF = (
    "diff --git a/gptme-contrib b/gptme-contrib\n"
    "index 1111111..2222222 160000\n"
    "--- a/gptme-contrib\n"
    "+++ b/gptme-contrib\n"
    "@@ -1 +1 @@\n"
    "-Subproject commit 1111111111111111111111111111111111111111\n"
    "+Subproject commit 2222222222222222222222222222222222222222\n"
)
_LOCK_DIFF = (
    "diff --git a/uv.lock b/uv.lock\n"
    "--- a/uv.lock\n"
    "+++ b/uv.lock\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)
_CODE_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _render(w, findings=None, **kwargs):
    defaults = dict(
        model="m",
        engine="llm",
        elapsed=1.0,
        head_sha="abc1234",
        full_sha="abc1234deadbeef",
        repo_slug="o/r",
        anchored=set(),
        history=[],
    )
    defaults.update(kwargs)
    return w.render(findings or [], **defaults)


def test_submodule_only_diff_is_pure():
    assert writer.is_submodule_only_diff(_SUBMODULE_DIFF) is True
    assert writer.is_submodule_only_diff(_CODE_DIFF) is False
    assert writer.is_submodule_only_diff(_SUBMODULE_DIFF + _CODE_DIFF) is False
    assert writer.is_submodule_only_diff("") is False


@pytest.mark.parametrize(
    "diff",
    [
        _SUBMODULE_DIFF.replace("160000", "100644"),
        _SUBMODULE_DIFF.replace("index 1111111..2222222 160000\n", ""),
        _SUBMODULE_DIFF + "diff --git a/a.png b/a.png\nBinary files differ\n",
        _SUBMODULE_DIFF
        + "diff --git a/a b/b\nsimilarity index 100%\nrename from a\nrename to b\n",
        _SUBMODULE_DIFF + "diff --git a/a b/a\nold mode 100644\nnew mode 100755\n",
        _SUBMODULE_DIFF.replace(
            "+Subproject commit", "+source mentions Subproject commit"
        ),
    ],
)
def test_submodule_detection_requires_gitlink_metadata_in_every_section(diff):
    assert writer.is_submodule_only_diff(diff) is False


@pytest.mark.parametrize("adding", [True, False])
def test_submodule_addition_and_deletion_are_pointer_only(adding):
    diff = (
        "diff --git a/sub b/sub\n"
        + ("new file mode 160000\n" if adding else "deleted file mode 160000\n")
        + "index 0000000..1111111\n"
        + ("--- /dev/null\n+++ b/sub\n" if adding else "--- a/sub\n+++ /dev/null\n")
        + "@@ -0,0 +1 @@\n"
        + ("+" if adding else "-")
        + "Subproject commit "
        + "1" * 40
        + "\n"
    )
    assert writer.is_submodule_only_diff(diff) is True


def test_generated_files_are_elided_unless_the_diff_is_only_them():
    mixed = _CODE_DIFF + _LOCK_DIFF
    touched, reviewable, _, elided = writer.extract_touched_files_and_reviewable_diff(
        mixed
    )
    assert touched == {"src/a.py"}
    assert "uv.lock" not in reviewable
    assert [e["path"] for e in elided] == ["uv.lock"]
    # lockfile-only stays whole so a 5/5 cannot launder an unexamined change
    touched, reviewable, _, elided = writer.extract_touched_files_and_reviewable_diff(
        _LOCK_DIFF
    )
    assert elided == []
    assert "uv.lock" in reviewable
    assert touched == {"uv.lock"}


def test_elision_note_names_the_skipped_file():
    assert writer.elision_note(None) == ""
    note = writer.elision_note(
        [{"path": "uv.lock", "added": 1, "removed": 1, "bytes": 2048}]
    )
    assert "uv.lock" in note and "NOT reviewed" in note


def test_drop_out_of_scope_splits_on_touched_files():
    findings = [
        {"file": "src/a.py", "body": "in"},
        {"file": "src/old.py", "body": "out"},
        {"file": "", "body": "pr-level"},
    ]
    in_scope, out = writer.drop_out_of_scope_findings(findings, {"src/a.py"})
    assert in_scope == [findings[0]]
    assert out == [findings[1], findings[2]]


def test_head_path_exists_uses_bound_run_and_encodes_the_path(alice_io):
    seen: list[str] = []

    def fake_run(cmd, **kw):
        seen.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    alice_io.run = fake_run
    w = writer.bind_io(alice_io)
    assert w.head_path_exists("o/r", "head#1", "docs/a b?#.md") is True
    assert "repos/o/r/contents/docs/a%20b%3F%23.md" in seen
    assert seen[-2:] == ["-f", "ref=head#1"]


def test_drop_nonexistent_head_findings_honours_a_host_path_probe(alice_io):
    alice_io.head_path_exists = lambda *_args: False
    w = writer.bind_io(alice_io)
    kept, dropped = w.drop_nonexistent_head_findings(
        [
            {"file": "src/gone.py", "body": "x"},
            {"file": "", "body": "pr-level"},
        ],
        repo="o/r",
        ref="abc",
        known_existing_paths={"src/kept.py"},
    )
    assert [f["file"] for f in kept] == [""]
    assert [f["file"] for f in dropped] == ["src/gone.py"]


def test_unbound_render_refuses_instead_of_posting_as_bob():
    with pytest.raises(RuntimeError, match="no host bound"):
        writer.render(
            [],
            model="m",
            engine="llm",
            elapsed=1.0,
            head_sha="abc",
            full_sha="abc",
            repo_slug="o/r",
            anchored=set(),
            history=[],
        )


def test_render_footer_uses_the_bound_identity(alice_io):
    w = writer.bind_io(alice_io)
    body = _render(w)
    assert "`@alice-agent review`" in body
    assert "`@alice-agent fix`" in body
    assert "@TimeToBuildBob" not in body
    assert "alice-ai-review" not in body or "<!--" not in body.split("Maintainer")[-1]


def test_two_hosts_render_independent_footers():
    a = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"))
    b = writer.default_io(sweep_identity("TimeToBuildBob", "bob-ai-review"))
    wa, wb = writer.bind_io(a), writer.bind_io(b)
    assert "`@alice-agent review`" in _render(wa)
    assert "`@TimeToBuildBob review`" in _render(wb)
