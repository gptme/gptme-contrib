"""Peer-host contract for gptme_review.writer (writer-port slices 2a–2d, 3a, 3b)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest
from gptme_review import writer
from gptme_review.core import ReviewError
from gptme_review.shard import Shard
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

    alice_io.gh_json = lambda args: [comments]  # one --slurp page
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
    alice_io.gh_json = lambda args: [[real, copied]]
    alice_io.run = lambda cmd, **kw: calls.append(cmd)
    keeper = writer.bind_io(alice_io).find_marker_comment("o/r", 7)
    assert keeper is not None and keeper["id"] == 1
    assert calls == []


@pytest.mark.parametrize(
    ("marker", "sha_key"),
    [
        ('<!-- rev {"sha": "abc"} -->', "sha"),
        ('<!-- rev-publishing {"publishing_sha": "abc"} -->', "publishing_sha"),
        ("<!-- rev head-sha: abc -->", "sha"),
    ],
)
def test_marker_namespace_is_not_matched_as_a_prefix(marker, sha_key):
    # Same author runs namespace `rev` and `rev-ai`: `<!-- rev` is a substring
    # of `<!-- rev-ai`, which made the newer rev-ai marker the keeper and
    # deleted the real `rev` marker (contrib#2384 defect 12).
    io = writer.default_io(sweep_identity("alice-agent", "rev"))
    me = {"login": "alice-agent"}
    own = {"id": 1, "body": marker, "updated_at": "2026-01-01", "user": me}
    other = {
        "id": 2,
        "body": "<!-- rev-ai {} -->",
        "updated_at": "2026-02-01",
        "user": me,
    }
    calls: list[list[str]] = []
    io.gh_json = lambda args: [[own, other]]
    io.run = lambda cmd, **kw: calls.append(cmd)
    w = writer.bind_io(io)
    keeper = w.find_marker_comment("o/r", 7)
    assert keeper is not None and keeper["id"] == 1
    assert w.marker_state(keeper["body"])[sha_key] == "abc"
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


@pytest.mark.parametrize(
    "path", ["src/é.py", "src/a\tb.py", 'src/a"b.py', "src/a b.py"]
)
@pytest.mark.parametrize("delete", [False, True])
def test_diff_paths_decode_real_git_quoting(tmp_path, path, delete):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    git("init", "-q")
    source = tmp_path / path
    source.parent.mkdir(parents=True)
    source.write_text("before\n")
    git("add", ".")
    base = git("write-tree").strip()
    if delete:
        source.unlink()
    else:
        source.write_text("after\n")
    lock = tmp_path / "déps" / "uv.lock"
    lock.parent.mkdir()
    lock.write_text("generated\n")
    git("add", "-A")
    diff = git("-c", "core.quotePath=true", "diff", "--cached", base)
    touched, reviewable, size, elided = (
        writer.extract_touched_files_and_reviewable_diff(diff)
    )
    assert touched == {path}
    assert [e["path"] for e in elided] == ["déps/uv.lock"]
    assert size == len(reviewable.encode())
    finding = {"file": path, "body": "in scope"}
    assert writer.drop_out_of_scope_findings([finding], touched) == ([finding], [])


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


@pytest.mark.parametrize("paths", [["old.py"], [""], ["old.py", ""]])
def test_suppression_note_covers_unanchored_and_untouched_findings(paths):
    suppressed = [{"file": path, "suppressed_reason": "out_of_scope"} for path in paths]
    note = writer.suppression_note(suppressed)
    assert f"{len(paths)} finding" in note
    assert "not anchored to a file this PR changed" in note
    assert "on files this PR did not touch" not in note
    assert writer.suppression_note([{"suppressed_reason": "consensus_rejected"}]) == ""


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
    assert "`@alice-agent fix`" not in body  # no fix worker on a default host
    assert "@TimeToBuildBob" not in body
    assert "alice-ai-review" not in body or "<!--" not in body.split("Maintainer")[-1]


def test_two_hosts_render_independent_footers():
    a = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"))
    b = writer.default_io(sweep_identity("TimeToBuildBob", "bob-ai-review"))
    wa, wb = writer.bind_io(a), writer.bind_io(b)
    assert "`@alice-agent review`" in _render(wa)
    assert "`@TimeToBuildBob review`" in _render(wb)


#: Bob's footer before maintainer commands became host-supplied; his explicit
#: two-command host must keep it byte-for-byte.
_BOB_COMMANDS_LINE = (
    "<sub>`@TimeToBuildBob review` (own line) — fresh review · "
    "`@TimeToBuildBob fix` — a worker acts on the findings. "
    "Once per comment; 👀 = received.</sub>"
)


def _bob_io(**overrides):
    ns = writer.default_io(sweep_identity("TimeToBuildBob", "bob-ai-review"))
    return SimpleNamespace(**{**vars(ns), **overrides})


def test_default_peer_footer_advertises_review_only(alice_io):
    body = _render(writer.bind_io(alice_io))
    assert (
        "<sub>`@alice-agent review` (own line) — fresh review. "
        "Once per comment; 👀 = received.</sub>"
    ) in body
    assert " fix`" not in body


def test_explicit_two_command_host_keeps_bobs_footer_byte_for_byte():
    w = writer.bind_io(_bob_io(MAINTAINER_COMMANDS=("review", "fix")))
    assert _BOB_COMMANDS_LINE in _render(w)


def test_bare_string_declaration_is_one_command():
    w = writer.bind_io(_bob_io(MAINTAINER_COMMANDS="fix"))
    body = _render(w)
    assert "`@TimeToBuildBob fix`" in body
    assert "review` (own line)" not in body


def test_namespace_without_the_setting_is_review_only():
    ns = vars(_bob_io()).copy()
    del ns["MAINTAINER_COMMANDS"]
    body = _render(writer.bind_io(SimpleNamespace(**ns)))
    assert "`@TimeToBuildBob review`" in body and " fix`" not in body


def test_two_hosts_do_not_leak_command_sets_or_logins(alice_io):
    bob = writer.bind_io(_bob_io(MAINTAINER_COMMANDS=("review", "fix")))
    alice = writer.bind_io(alice_io)
    bob_body, alice_body = _render(bob), _render(alice)
    assert _BOB_COMMANDS_LINE in bob_body and "alice-agent" not in bob_body
    assert " fix`" not in alice_body and "TimeToBuildBob" not in alice_body


@pytest.mark.parametrize(
    "commands",
    [
        (),
        ("review", "deploy"),
        "deploy",
        None,
        42,
        [["review"]],
        ("review", ["fix"]),
    ],
)
def test_unsupported_command_sets_are_refused(commands):
    w = writer.bind_io(_bob_io(MAINTAINER_COMMANDS=commands))
    with pytest.raises(ValueError, match="unsupported maintainer commands"):
        _render(w)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("<outside> ``<inside>``", "&lt;outside&gt; ``<inside>``"),
        ("``the tag `<x>` is literal``", "``the tag `<x>` is literal``"),
        ("```a ``<x>`` b```", "```a ``<x>`` b```"),
        ("``<a>\n<b>``", "``<a>\n<b>``"),
        ("``<unclosed>`", "``&lt;unclosed&gt;`"),
        ("``<a>``` <b>", "``&lt;a&gt;``` &lt;b&gt;"),
        ("`<a>` and ``<b>`` <c>", "`<a>` and ``<b>`` &lt;c&gt;"),
    ],
)
def test_md_text_preserves_equal_length_backtick_spans(text, expected):
    assert writer.md_text(text) == expected
    assert writer.md_text(text + "|", escape_pipes=True) == expected + "\\|"


@pytest.mark.parametrize(
    "path, code, encoded",
    [
        ("src/lib(1).py", "`src/lib(1).py`", "src/lib%281%29.py"),
        ("src/a)b].py", "`src/a)b].py`", "src/a%29b%5D.py"),
        ("src/a`b.py", "``src/a`b.py``", "src/a%60b.py"),
        ("`src/a``.py`", "``` `src/a``.py` ```", "%60src/a%60%60.py%60"),
        ("src/<x>&#?.py", "`src/<x>&#?.py`", "src/%3Cx%3E%26%23%3F.py"),
        ("src/a b.py", "`src/a b.py`", "src/a%20b.py"),
    ],
)
def test_render_locations_and_file_lists_quote_literal_paths(
    alice_io, path, code, encoded
):
    w = writer.bind_io(alice_io)
    finding = {"file": path, "line": 10, "severity": "P2", "body": "Bug."}
    # Unanchored findings get a permalink; anchored ones use a plain code span.
    with_line = code.replace(path, path + ":10")
    body = _render(w, [finding])
    assert (
        f"[{with_line}](https://github.com/o/r/blob/abc1234deadbeef/{encoded}#L10)"
        in body
    )
    anchored = _render(w, [finding], anchored={id(finding)})
    assert f" — {with_line}\n" in anchored
    no_line = _render(w, [{**finding, "line": None}])
    assert f" — {code}\n" in no_line
    files = "\n".join(
        w.render_files_changed(
            {"files": [{"path": path, "description": "Touches <tag>."}]}, [path]
        )
    )
    assert f"- {code} — Touches &lt;tag&gt;." in files
    assert f"Also changed, not described above: {code}</sub>" in files


def test_mixed_advisory_and_informational_notes_do_not_claim_all_are_outside(alice_io):
    body = _render(
        writer.bind_io(alice_io),
        advisory_findings=[{"file": "changed.py", "line": 1, "body": "Advisory."}],
        informational_findings=[
            {"file": "untouched.py", "line": 2, "body": "Outside."}
        ],
    )
    assert "No thread-worthy findings." in body
    assert "What follows is outside them" not in body
    assert "changed.py:1" in body and "untouched.py:2" in body


# --- writer-port slice 2d: checkout helpers and static preflight ---------------


def _bound_with(alice_io, **overrides):
    return writer.bind_io(SimpleNamespace(**{**vars(alice_io), **overrides}))


def _scripted_run(responses):
    """A fake ``run`` answering each command by its leading argv words."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        for prefix, (code, out, *err) in responses.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(
                    cmd, code, out, err[0] if err else "err"
                )
        raise AssertionError(f"unexpected command {cmd}")

    return fake_run, calls


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Owner/Repo.git",
        "https://github.com/owner/repo",
        "ssh://git@github.com/owner/repo.git",
        "https://token@github.com/owner/repo.git",
    ],
)
def test_slug_from_remote_url_shapes(alice_io, url):
    assert writer.bind_io(alice_io)._slug_from_remote_url(url) == "owner/repo"


def test_review_checkout_root_is_per_host(alice_io, tmp_path, monkeypatch):
    monkeypatch.delenv("GPTME_REVIEW_CHECKOUT", raising=False)
    a = _bound_with(alice_io, CHECKOUT_ROOT=tmp_path / "a")
    b = _bound_with(alice_io, CHECKOUT_ROOT=tmp_path / "b")
    assert a.review_checkout_for("o/r") == tmp_path / "a" / "o-r-review"
    # A traversal owner collapses to a safe component, never escapes the root.
    assert b.review_checkout_for("../x/y") == tmp_path / "b" / "unknown-x-y-review"
    monkeypatch.setenv("GPTME_REVIEW_CHECKOUT", str(tmp_path / "pinned"))
    assert a.review_checkout_for("o/r") == tmp_path / "pinned"


def test_default_io_supplies_a_checkout_root(monkeypatch, tmp_path):
    monkeypatch.setenv("GPTME_REVIEW_CHECKOUT_ROOT", str(tmp_path))
    ns = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"))
    assert ns.CHECKOUT_ROOT == tmp_path


def test_sibling_note_lists_only_siblings_under_the_bound_root(alice_io, tmp_path):
    w = _bound_with(alice_io, CHECKOUT_ROOT=tmp_path)
    assert w.build_cross_repo_sibling_note("gptme/gptme-contrib") == ""
    (tmp_path / "gptme-gptme-review").mkdir()
    (tmp_path / "gptme-gptme-contrib-review").mkdir()  # the reviewed repo itself
    note = w.build_cross_repo_sibling_note("gptme/gptme-contrib")
    assert "`gptme/gptme`" in note and "shared-tooling" in note
    assert "`gptme/gptme-contrib`" not in note


def test_prepare_agent_checkout_refuses_a_remote_that_does_not_own_the_repo(
    alice_io, tmp_path
):
    (tmp_path / ".git").mkdir()
    fake_run, calls = _scripted_run(
        {
            ("git", "status"): (0, ""),
            ("git", "remote"): (0, "origin\tgit@github.com:other/repo.git (fetch)\n"),
        }
    )
    w = _bound_with(alice_io, run=fake_run)
    with pytest.raises(writer.ReviewError, match="no remote pointing at o/r"):
        w.prepare_agent_checkout("o/r", "a" * 40, tmp_path)
    assert not any(c[:2] == ["git", "fetch"] for c in calls)


def test_prepare_agent_checkout_verifies_the_checked_out_head(alice_io, tmp_path):
    (tmp_path / ".git").mkdir()
    sha = "b" * 40
    remotes = "fork\thttps://github.com/o/r (fetch)\norigin\tgit@github.com:o/r.git\n"
    responses = {
        ("git", "status"): (0, ""),
        ("git", "remote"): (0, remotes),
        ("git", "fetch"): (0, ""),
        ("git", "checkout"): (0, ""),
        ("git", "rev-parse"): (0, sha + "\n"),
    }
    fake_run, calls = _scripted_run(responses)
    _bound_with(alice_io, run=fake_run).prepare_agent_checkout("o/r", sha, tmp_path)
    assert ["git", "fetch", "--quiet", "origin", sha] in calls  # origin wins

    responses[("git", "rev-parse")] = (0, "c" * 40 + "\n")
    fake_run, _ = _scripted_run(responses)
    with pytest.raises(writer.ReviewError, match="checkout mismatch"):
        _bound_with(alice_io, run=fake_run).prepare_agent_checkout("o/r", sha, tmp_path)


def test_prepare_agent_checkout_refuses_a_dirty_tree(alice_io, tmp_path):
    (tmp_path / ".git").mkdir()
    fake_run, _ = _scripted_run({("git", "status"): (0, " M a.py\n")})
    with pytest.raises(writer.ReviewError, match="dirty"):
        _bound_with(alice_io, run=fake_run).prepare_agent_checkout(
            "o/r", "a" * 40, tmp_path
        )


def test_checkout_lock_refuses_a_held_lock_without_hanging(alice_io, tmp_path):
    w = writer.bind_io(alice_io)
    checkout = tmp_path / "o-r-review"
    held = w.acquire_agent_checkout_lock(checkout, wait_secs=0)
    try:
        with pytest.raises(writer.ReviewError, match="stayed locked"):
            w.acquire_agent_checkout_lock(checkout, wait_secs=0)
    finally:
        held.close()
    w.acquire_agent_checkout_lock(checkout, wait_secs=0).close()


def test_read_checkout_file_refuses_paths_outside_the_checkout(alice_io, tmp_path):
    w = writer.bind_io(alice_io)
    (tmp_path / "co").mkdir()
    (tmp_path / "co" / "a.txt").write_text("ok")
    (tmp_path / "secret").write_text("no")
    assert w.read_checkout_file(tmp_path / "co", "a.txt") == "ok"
    assert w.read_checkout_file(tmp_path / "co", "../secret") is None


def test_cited_sha_lookup_goes_through_the_bound_run(alice_io):
    fake_run, calls = _scripted_run(
        {("gh", "api"): (1, "", "gh: No commit found for SHA: abc1234 (HTTP 422)")}
    )
    w = _bound_with(alice_io, run=fake_run)
    body = "Fixed in `abc1234`. Also bumps the pin, commit 2222222."
    findings = w._check_cited_shas(body, "o/r", ["src/a.py"], _SUBMODULE_DIFF)
    assert [f["title"] for f in findings] == ["Cited commit `abc1234` not found in o/r"]
    assert findings[0]["severity"] == "P1" and findings[0]["static"] is True
    # The gitlink SHA the PR moves is accepted without a lookup.
    assert calls == [["gh", "api", "repos/o/r/commits/abc1234", "--jq", ".sha"]]


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: API rate limit exceeded (HTTP 403)",
        "gh: Server Error (HTTP 502)",
        "error connecting to api.github.com",
        # A repo this host cannot read answers 404, not 422.
        "gh: Not Found (HTTP 404)",
    ],
)
def test_cited_sha_lookup_error_is_not_a_missing_commit(alice_io, stderr):
    """A failed lookup is unknown, not 'not found' (contrib#2384, #2273)."""
    fake_run, _calls = _scripted_run({("gh", "api"): (1, "", stderr)})
    w = _bound_with(alice_io, run=fake_run)
    assert w._check_cited_shas("Fixed in `abc1234`.", "o/r", ["src/a.py"]) == []


def test_cited_sha_unreadable_cited_repo_blocks_the_finding(alice_io):
    """Absent in the parent proves nothing if a cited repo cannot be read."""
    fake_run, calls = _scripted_run(
        {
            ("gh", "api", "repos/o/r/commits/abc1234"): (
                1,
                "",
                "gh: No commit found for SHA: abc1234 (HTTP 422)",
            ),
            ("gh", "api", "repos/private/repo/commits/abc1234"): (
                1,
                "",
                "gh: Not Found (HTTP 404)",
            ),
        }
    )
    w = _bound_with(alice_io, run=fake_run)
    body = "See private/repo#3, fixed in `abc1234`."
    assert w._check_cited_shas(body, "o/r", ["src/a.py"]) == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user-attachments/assets/0a1b2c3d",
        "https://github.com/orgs/gptme/projects/1",
    ],
)
def test_cited_sha_ignores_github_urls_that_name_no_repo(alice_io, url):
    """Their 404 would otherwise suppress a real missing-commit finding."""
    fake_run, calls = _scripted_run(
        {("gh", "api"): (1, "", "gh: No commit found for SHA: abc1234 (HTTP 422)")}
    )
    w = _bound_with(alice_io, run=fake_run)
    findings = w._check_cited_shas(f"![s]({url}) fixed in abc1234", "o/r", ["a.py"])
    assert [f["severity"] for f in findings] == ["P1"]
    assert len(calls) == 1


def test_cited_sha_does_not_truncate_a_full_ref(alice_io):
    """A 40-char SHA is a full ref: it must not be looked up as a prefix.

    Truncating it to 12 hex chars produced a false P1 when the real commit
    lived in a submodule or another repo (gptme-cloud#902/#912 class).
    """
    fake_run, calls = _scripted_run({("gh",): (0, "abc1234\n")})
    w = _bound_with(alice_io, run=fake_run)
    full = "abcdef0123456789abcdef0123456789abcdef01"
    assert w._check_cited_shas(f"fixed in {full}", "o/r", ["src/a.py"]) == []
    assert calls == []
    # Uppercase hex is normalized before the lookup, since git only resolves
    # lowercase.
    assert w._check_cited_shas("fixed in ABC1234", "o/r", ["src/a.py"]) == []
    assert calls == [["gh", "api", "repos/o/r/commits/abc1234", "--jq", ".sha"]]


@pytest.mark.parametrize(
    "path,is_test",
    [
        ("tests/test_a.py", True),
        ("web/src/components/A.test.tsx", True),
        ("web/src/components/A.spec.ts", True),
        ("src/test_render_helpers.tsx", True),
        # `test_` as a substring of a production filename is not a test file.
        ("src/latest_compile.py", False),
        ("src/contest_config.py", False),
    ],
)
def test_test_file_regex_is_segment_anchored(alice_io, path, is_test):
    assert bool(writer._TEST_FILE_RE.search(path)) is is_test


def test_unrelated_ci_claim_reads_checks_through_the_bound_gh_json(alice_io):
    seen: list[list[str]] = []

    def fake_gh_json(cmd):
        seen.append(cmd)
        return [
            {"name": "Test", "state": "FAILURE"},
            {"name": "Lint", "state": "SUCCESS"},
        ]

    w = _bound_with(alice_io, gh_json=fake_gh_json)
    assert (
        w._check_unrelated_ci_claim("unrelated to the version bump", "o/r", 5, []) == []
    )
    assert seen == []  # prose without a CI term is not a claim
    findings = w._check_unrelated_ci_claim(
        "The CI failure is unrelated.", "o/r", 5, ["a"]
    )
    assert seen == [["pr", "checks", "5", "--repo", "o/r", "--json", "name,state"]]
    assert len(findings) == 1 and "`Test`" in findings[0]["body"]


@pytest.mark.parametrize("response", [None, {}, "invalid", [None], [{}]])
def test_unrelated_ci_claim_refuses_unknown_check_state(alice_io, response):
    w = _bound_with(alice_io, gh_json=lambda cmd: response)
    with pytest.raises(writer.ReviewError, match="cannot verify CI checks"):
        w._check_unrelated_ci_claim("The CI failure is unrelated.", "o/r", 5, ["a"])


@pytest.mark.parametrize("response", [[], [{"name": "Test", "state": "SUCCESS"}]])
def test_unrelated_ci_claim_accepts_known_nonfailing_checks(alice_io, response):
    w = _bound_with(alice_io, gh_json=lambda cmd: response)
    assert w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"]) == []


@pytest.mark.parametrize(
    "state", ["PENDING", "IN_PROGRESS", "QUEUED", "NEUTRAL", "SKIPPED", "STALE"]
)
def test_unrelated_ci_claim_pending_or_neutral_checks_are_not_failures(alice_io, state):
    w = _bound_with(alice_io, gh_json=lambda cmd: [{"name": "Test", "state": state}])
    assert w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"]) == []


@pytest.mark.parametrize("state", ["ACTION_REQUIRED", "STARTUP_FAILURE"])
def test_unrelated_ci_claim_counts_action_required_and_startup_failure(alice_io, state):
    w = _bound_with(alice_io, gh_json=lambda cmd: [{"name": "Test", "state": state}])
    findings = w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"])
    assert len(findings) == 1 and "`Test`" in findings[0]["body"]


def test_unrelated_ci_claim_refuses_an_unrecognised_state(alice_io):
    w = _bound_with(
        alice_io, gh_json=lambda cmd: [{"name": "Test", "state": "SOMETHING_NEW"}]
    )
    with pytest.raises(writer.ReviewError, match="unknown check state.*SOMETHING_NEW"):
        w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"])


@pytest.mark.parametrize("unknown_state", ["SOMETHING_NEW", "COMPLETED"])
def test_unrelated_ci_claim_known_failure_survives_unknown_peer(
    alice_io, unknown_state
):
    w = _bound_with(
        alice_io,
        gh_json=lambda cmd: [
            {"name": "Test", "state": "FAILURE"},
            {"name": "Other", "state": unknown_state},
        ],
    )
    findings = w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"])
    assert len(findings) == 1
    assert "1 check(s)" in findings[0]["body"]
    assert "`Test`" in findings[0]["body"]
    # The unknown state is not counted as a failing check, but it must not be
    # silently dropped either — the finding names it as unclassifiable.
    assert "Unrecognised check state(s)" in findings[0]["body"]
    assert unknown_state in findings[0]["body"]


@pytest.mark.parametrize("state", ["", None])
def test_unrelated_ci_claim_missing_state_is_unverifiable(alice_io, state):
    w = _bound_with(alice_io, gh_json=lambda cmd: [{"name": "Test", "state": state}])
    with pytest.raises(writer.ReviewError, match="cannot verify CI checks"):
        w._check_unrelated_ci_claim("Flaky test.", "o/r", 5, ["a"])


def test_visual_rule_checks_renamed_component_using_extracted_new_path(alice_io):
    w = writer.bind_io(alice_io)
    diff = (
        "diff --git a/web/Old.tsx b/web/New.tsx\n"
        "similarity index 80%\nrename from web/Old.tsx\nrename to web/New.tsx\n"
        "--- a/web/Old.tsx\n+++ b/web/New.tsx\n"
        "@@ -1 +1 @@\n-<div className='old'/>\n+<div className='new'/>\n"
    )
    touched, reviewable, _, _ = w.extract_touched_files_and_reviewable_diff(diff)
    assert touched == {"web/New.tsx"}
    findings = w._check_visual_screenshot("", reviewable, touched)
    assert len(findings) == 1 and findings[0]["severity"] == "P1"


class _JournalHost(writer.DefaultWriterHost):
    def extra_static_checks(self, diff, touched_files):
        return [{"title": "host rule", "static": True}]


def test_static_preflight_adds_host_checks_only_when_a_host_supplies_them(alice_io):
    fake_run, calls = _scripted_run({})
    w = _bound_with(alice_io, run=fake_run)
    kwargs = dict(
        pr_title="fix(x): y",
        pr_body="",
        diff=_CODE_DIFF,
        touched_files=["src/a.py"],
        repo="o/r",
        number=1,
    )
    default = w.static_preflight_findings(**kwargs)
    assert [f["title"] for f in default] == ["fix PR has no regression test"]
    hosted = w.static_preflight_findings(**kwargs, host=_JournalHost())
    assert [f["title"] for f in hosted] == [
        "fix PR has no regression test",
        "host rule",
    ]
    assert calls == []  # no cited SHA, no CI claim: no I/O


@pytest.mark.parametrize(
    "title, flagged",
    [
        ("fix: plain", True),
        ("fix(review): scoped", True),
        ("fix!: breaking", True),
        ("fix(review)!: scoped breaking", True),
        ("feat!: not a fix", False),
        ("fixup: not a fix type", False),
    ],
)
def test_rule_7_recognises_breaking_change_fix_titles(alice_io, title, flagged):
    w = writer.bind_io(alice_io)
    findings = w._check_fix_without_test(title, ["src/a.py"])
    assert [f["title"] for f in findings] == (
        ["fix PR has no regression test"] if flagged else []
    )
    assert w._check_fix_without_test(title, ["src/a.py", "tests/test_a.py"]) == []


@pytest.mark.parametrize("attribute", ["#[test]", "#[tokio::test]", "#[cfg(test)]"])
def test_rule_7_accepts_inline_rust_unit_tests(alice_io, attribute):
    """Rust keeps unit tests in ``#[cfg(test)]`` modules inside ``src/``.

    A fix that adds ``#[test]`` functions there carries a regression test
    even though no touched path looks like a test file
    (ActivityWatch/aw-notify-rs#49 got the advisory anyway).
    """
    w = writer.bind_io(alice_io)
    diff = (
        "diff --git a/src/main.rs b/src/main.rs\n"
        "--- a/src/main.rs\n"
        "+++ b/src/main.rs\n"
        "@@ -1,2 +1,6 @@\n"
        f"+    {attribute}\n"
        "+    fn threshold_alerts_rearm_on_new_day() {}\n"
    )
    assert w._check_fix_without_test("fix(alerts): re-arm", ["src/main.rs"], diff) == []
    # An existing, unchanged test attribute is context, not a new test.
    context_only = diff.replace(f"+    {attribute}", f"     {attribute}")
    assert w._check_fix_without_test(
        "fix(alerts): re-arm", ["src/main.rs"], context_only
    )


def test_rule_7_accepts_inline_rust_tests_in_git_quoted_paths(alice_io):
    w = writer.bind_io(alice_io)
    diff = (
        'diff --git "a/src/caf\\303\\251.rs" "b/src/caf\\303\\251.rs"\n'
        '--- "a/src/caf\\303\\251.rs"\n'
        '+++ "b/src/caf\\303\\251.rs"\n'
        "@@ -0,0 +1,2 @@\n"
        "+#[test]\n"
        "+fn regression() {}\n"
    )
    assert w._check_fix_without_test("fix: regression", ["src/café.rs"], diff) == []


@pytest.mark.parametrize("example_path", ["docs/guide.md", "src/app.py"])
@pytest.mark.parametrize("example_first", [False, True])
def test_rule_7_ignores_rust_attributes_outside_rust_files(
    alice_io, example_path, example_first
):
    w = writer.bind_io(alice_io)
    rust_diff = (
        "diff --git a/src/main.rs b/src/main.rs\n"
        "--- a/src/main.rs\n"
        "+++ b/src/main.rs\n"
        "@@ -1 +1 @@\n"
        "-fn helper() {}\n"
        "+fn helper() { fixed(); }\n"
    )
    example_diff = (
        f"diff --git a/{example_path} b/{example_path}\n"
        f"--- a/{example_path}\n"
        f"+++ b/{example_path}\n"
        "@@ -0,0 +1,4 @@\n"
        "+```rust\n"
        "+#[test]\n"
        "+fn example() {}\n"
        "+```\n"
    )
    diff = example_diff + rust_diff if example_first else rust_diff + example_diff
    findings = w._check_fix_without_test(
        "fix(helper): handle input", ["src/main.rs", example_path], diff
    )
    assert [f["title"] for f in findings] == ["fix PR has no regression test"]


def test_rule_7_context_test_attribute_is_not_bridged_by_a_blank_line(alice_io):
    """An added blank line must not reach the next context line's ``#[test]``.

    ``\\s`` matches newlines, so ``^\\+\\s*#\\[`` could consume the line break of
    an added empty line and match a test attribute that was already there. That
    suppressed rule 7 without any regression test being added.
    """
    w = writer.bind_io(alice_io)
    diff = (
        "diff --git a/src/main.rs b/src/main.rs\n"
        "--- a/src/main.rs\n"
        "+++ b/src/main.rs\n"
        "@@ -1,2 +1,3 @@\n"
        "+    fn helper() {}\n"
        "+\n"
        "     #[test]\n"
    )
    findings = w._check_fix_without_test("fix(alerts): re-arm", ["src/main.rs"], diff)
    assert [f["title"] for f in findings] == ["fix PR has no regression test"]


def test_visual_rule_keys_sections_by_new_path_not_header_split(alice_io):
    """A path containing `` b/ `` must still be keyed correctly (rule 1c).

    Splitting ``diff --git a/<old> b/<new>`` on the first `` b/ `` lands
    mid-path for such a file, so the per-file lookup missed and a visual
    change was silently exempted from the screenshot requirement.
    """
    w = writer.bind_io(alice_io)
    path = "web/src/components/foo b/Bar.tsx"
    diff = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n-<div className='a'/>\n+<div className='b'/>\n"
    )
    findings = w._check_visual_screenshot("", diff, [path])
    assert len(findings) == 1 and findings[0]["severity"] == "P1"


def test_visual_rule_does_not_skip_a_css_universal_selector(alice_io):
    """A bare ``*`` rule is markup, not a block-comment continuation (rule 1c).

    ``_COMMENT_LINE_RE`` used to swallow any line starting with ``*``, so
    ``* { color: red }`` in a stylesheet was filtered before the visual-change
    scan and a real UI change passed without a screenshot.
    """
    w = writer.bind_io(alice_io)
    path = "web/src/static/dark.css"
    diff = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n-* { color: blue }\n+* { color: red }\n"
    )
    findings = w._check_visual_screenshot("", diff, [path])
    assert len(findings) == 1 and findings[0]["severity"] == "P1"
    assert w._check_visual_screenshot("![After](u)", diff, [path]) == []


def test_visual_rule_reads_a_hunk_line_that_starts_with_double_dash(alice_io):
    """Only pre-hunk ``+++``/``---`` are headers; hunk content is inspected."""
    w = writer.bind_io(alice_io)
    path = "web/src/static/dark.css"
    diff = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n---border: 1px\n+++border: 2px\n"
    )
    findings = w._check_visual_screenshot("", diff, [path])
    assert len(findings) == 1 and findings[0]["severity"] == "P1"


def test_visual_rule_still_skips_block_comment_continuations(alice_io):
    """Prose in a ``*``-prefixed comment line still renders nothing."""
    w = writer.bind_io(alice_io)
    path = "web/src/components/A.tsx"
    diff = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "- * old comment mentioning color\n"
        "+ * new comment mentioning border\n"
    )
    assert w._check_visual_screenshot("", diff, [path]) == []


def test_visual_rule_ignores_generics_and_whole_file_deletions(alice_io):
    w = writer.bind_io(alice_io)
    generic = (
        "diff --git a/web/src/components/A.tsx b/web/src/components/A.tsx\n"
        "--- a/web/src/components/A.tsx\n+++ b/web/src/components/A.tsx\n"
        "@@ -1 +1 @@\n-const x: Promise<void> = f()\n+const y: Promise<void> = f()\n"
    )
    assert w._check_visual_screenshot("", generic, ["web/src/components/A.tsx"]) == []
    markup = generic.replace("Promise<void> = f()", "<div className='a'/>")
    found = w._check_visual_screenshot("", markup, ["web/src/components/A.tsx"])
    assert found and found[0]["severity"] == "P1"
    assert (
        w._check_visual_screenshot("![After](u)", markup, ["web/src/components/A.tsx"])
        == []
    )


def test_cited_sha_requires_a_word_boundary_before_the_trigger(alice_io):
    """Prose that merely contains a trigger word is not a citation.

    ``unfixed in <sha>`` / ``prefixcommit <sha>`` must not fire a lookup: the
    same hyphen/word-boundary misfire the ``merged`` alternative already guards
    against (gptme-cloud#902).
    """
    fake_run, calls = _scripted_run({("gh",): (0, "")})
    w = _bound_with(alice_io, run=fake_run)
    for body in (
        "the bug is unfixed in abc1234",
        "prefixcommit abc1234",
        "xfollow-up-commit abc1234",
    ):
        assert w._check_cited_shas(body, "o/r", ["src/a.py"]) == []
    assert calls == []


@pytest.mark.parametrize("patched", [False, True])
def test_preload_files_minimal_host_does_not_need_private_sentinel(patched):
    calls = []
    contents = {"a.py": "source"}
    coverage = {"files_in_diff": 2, "files_preloaded": 1, "files_truncated": 1}

    def fake_contents(*args):
        calls.append(("contents", args))
        return contents

    def fake_preload(*args):
        calls.append(("preload", args))
        return SimpleNamespace(
            contents=contents, fully_preloaded={"a.py"}, coverage=coverage
        )

    io = SimpleNamespace(
        fetch_file_contents=fake_contents if patched else writer.fetch_file_contents,
        fetch_file_preload=fake_preload,
    )
    w = writer.bind_io(io)
    paths = ["a.py", "b.py"]
    assert w.preload_files(
        _engine_args(no_preload=False), {"headRefOid": "abc"}, paths
    ) == (contents, {"a.py"}, coverage)
    assert calls == [("contents" if patched else "preload", ("o/r", "abc", paths))]


class _RecordingHost(writer.DefaultWriterHost):
    def __init__(self) -> None:
        self.runs: list[dict] = []

    def log_run(self, **kw) -> None:
        self.runs.append(kw)


def _engine_args(**over) -> SimpleNamespace:
    base = {"engine": "llm", "dry_run": False, "repo": "o/r", "number": 7}
    return SimpleNamespace(**{**base, **over})


def test_llm_review_call_logs_the_run_through_the_peer_host():
    host = _RecordingHost()
    io = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"), host)
    io._openrouter_review_call = lambda *a, **k: ([{"title": "t"}], {"cost": 0.5})
    w = writer.bind_io(io)
    findings, usage = w.llm_review_call(
        _engine_args(),
        {"headRefOid": "abc"},
        model="openrouter/m",
        key="k",
        diff="d",
        contents={},
        preload_coverage=None,
        note="",
    )
    assert findings == [{"title": "t"}] and usage == {"cost": 0.5}
    assert [(r["repo"], r["number"], r["sha"], r["model"]) for r in host.runs] == [
        ("o/r", 7, "abc", "m")
    ]


def test_llm_review_call_logs_errors_through_the_peer_host():
    host = _RecordingHost()
    io = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"), host)
    error = ReviewError("provider unavailable")

    def fail(*args, **kwargs):
        raise error

    io._openrouter_review_call = fail
    with pytest.raises(ReviewError) as caught:
        writer.bind_io(io).llm_review_call(
            _engine_args(),
            {"headRefOid": "abc"},
            model="openrouter/m",
            key="k",
            diff="d",
            contents={},
            preload_coverage=None,
            note="",
        )
    assert caught.value is error
    assert len(host.runs) == 1
    run = host.runs[0]
    assert (run["repo"], run["number"], run["sha"], run["model"]) == (
        "o/r",
        7,
        "abc",
        "m",
    )
    assert run["usage"] == {} and run["findings"] == []
    assert run["error"] == "provider unavailable" and run["wall_s"] >= 0


def _sharded_io(host, review_call):
    io = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"), host)
    io.preload_files = lambda args, pr, paths: ({}, set(), {})
    io.fetch_submodule_context = lambda *a, **k: ""
    io._openrouter_review_call = review_call
    return writer.bind_io(io)


def _run_sharded(w, args):
    return w.run_sharded_llm(
        args,
        {"headRefOid": "abc"},
        [Shard(index=1), Shard(index=2)],
        reviewable_diff="",
        model="openrouter/m",
        key="k",
        base_note="",
    )


def test_run_sharded_llm_logs_one_merged_run():
    # Shard calls are labelled, so llm_review_call skips log_run for each of
    # them; the merged review must still reach the run ledger once (#2282).
    host = _RecordingHost()
    w = _sharded_io(host, lambda *a, **k: ([{"title": "t"}], {"cost": 0.25}))
    findings, usage, *_ = _run_sharded(w, _engine_args())
    assert len(findings) == 2
    assert len(host.runs) == 1
    run = host.runs[0]
    assert (run["repo"], run["number"], run["sha"], run["model"]) == (
        "o/r",
        7,
        "abc",
        "m",
    )
    assert run["findings"] == findings and run["usage"] == usage
    assert run.get("error") is None and run["wall_s"] >= 0


def test_run_sharded_llm_logs_a_failed_review():
    host = _RecordingHost()

    def fail(*args, **kwargs):
        raise ReviewError("provider unavailable")

    w = _sharded_io(host, fail)
    with pytest.raises(ReviewError, match="provider unavailable"):
        _run_sharded(w, _engine_args())
    assert len(host.runs) == 1
    run = host.runs[0]
    assert "cost" not in run["usage"] and run["findings"] == []
    assert "provider unavailable" in run["error"]


def test_run_sharded_llm_failed_review_keeps_completed_shard_cost():
    # A shard that finished before a sibling failed was still billed.
    host = _RecordingHost()
    first_running = threading.Event()

    def one_fails(*args, **kwargs):
        if kwargs["label"].startswith("[shard 1/"):
            first_running.set()
            return [{"title": "t"}], {"cost": 0.25, "prompt_tokens": 10}
        # Fail only once shard 1 is running, so it cannot be cancelled.
        assert first_running.wait(5)
        raise ReviewError("provider unavailable")

    w = _sharded_io(host, one_fails)
    with pytest.raises(ReviewError, match="shard 2/2 failed"):
        _run_sharded(w, _engine_args())
    (run,) = host.runs
    assert (run["usage"]["cost"], run["usage"]["prompt_tokens"]) == (0.25, 10)
    assert run["findings"] == [] and "provider unavailable" in run["error"]


def test_run_sharded_llm_dry_run_logs_nothing():
    host = _RecordingHost()
    w = _sharded_io(host, lambda *a, **k: ([], {}))
    _run_sharded(w, _engine_args(dry_run=True))
    assert host.runs == []


def test_subscription_engine_is_unavailable_on_a_default_peer_host(alice_io):
    w = writer.bind_io(alice_io)
    with pytest.raises(ReviewError, match="subscription engine"):
        w.llm_review_call(
            _engine_args(engine="subscription"),
            {},
            model="m",
            key="k",
            diff="d",
            contents={},
            preload_coverage=None,
            note="",
        )


def test_run_shards_fails_closed_and_keeps_the_error_type(alice_io):
    w = writer.bind_io(alice_io)
    plan = [Shard(index=1), Shard(index=2)]

    def one(shard: Shard):
        if shard.index == 2:
            raise ReviewError("boom")
        return [], {}

    with pytest.raises(ReviewError, match=r"shard 2/2 failed: boom"):
        w._run_shards(plan, one)
    assert w._run_shards([Shard(index=1)], lambda s: ([{"a": 1}], {})) == [
        ([{"a": 1}], {})
    ]


# --- writer-port slice 3b: two-phase publication ------------------------------


class _Lock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AdvisoryHost(writer.DefaultWriterHost):
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append_advisories(self, repo, number, sha, advisories):
        self.rows.append((repo, number, sha, list(advisories)))
        return len(advisories), []


def _publish_io(alice_io, fake_run, *, current=None):
    lock = _Lock()
    w = _bound_with(
        alice_io,
        run=fake_run,
        find_marker_comment=lambda repo, number: current,
        _publication_lock=lambda repo, number: lock,
    )
    return w, lock


def _publish_kwargs(**over):
    base = dict(
        repo="o/r",
        number=7,
        full_sha="a" * 40,
        summary_body="## review",
        final_state={"sha": "a" * 12, "advisories": [{"title": "adv"}]},
        previous_state={},
        inline=[{"path": "x.py", "line": 1, "body": "finding"}],
        existing=None,
    )
    return {**base, **over}


def test_publish_review_orders_mutations_through_the_bound_io(alice_io):
    fake_run, calls = _scripted_run(
        {
            ("gh", "api", "repos/o/r/issues/7/comments"): (0, "123\n"),
            ("gh", "api", "repos/o/r/pulls/7/reviews"): (0, ""),
            ("gh", "api", "--method", "PATCH"): (0, "https://x/1\n"),
        }
    )
    w, lock = _publish_io(alice_io, fake_run)
    host = _AdvisoryHost()
    assert w.publish_review(**_publish_kwargs(), host=host) is True
    assert [c[2] if c[2] != "--method" else c[4] for c in calls] == [
        "repos/o/r/issues/7/comments",
        "repos/o/r/pulls/7/reviews",
        "repos/o/r/issues/comments/123",
    ]
    provisional, final = calls[0][4], calls[2][6]
    assert "<!-- alice-ai-review-publishing " in provisional
    assert "<!-- alice-ai-review " in final and "bob-ai-review" not in final
    assert host.rows == [("o/r", 7, "a" * 40, [{"title": "adv"}])]
    assert lock.closed


def test_publish_review_default_host_records_no_advisories(alice_io):
    fake_run, _ = _scripted_run(
        {
            ("gh", "api", "repos/o/r/issues/7/comments"): (0, "123\n"),
            ("gh", "api", "repos/o/r/pulls/7/reviews"): (0, ""),
            ("gh", "api", "--method", "PATCH"): (0, "https://x/1\n"),
        }
    )
    w, _ = _publish_io(alice_io, fake_run)
    assert w.publish_review(**_publish_kwargs()) is True


def test_publish_review_failed_native_review_leaves_the_provisional_marker(alice_io):
    fake_run, calls = _scripted_run(
        {
            ("gh", "api", "repos/o/r/issues/7/comments"): (0, "123\n"),
            ("gh", "api", "repos/o/r/pulls/7/reviews"): (1, ""),
        }
    )
    w, lock = _publish_io(alice_io, fake_run)
    host = _AdvisoryHost()
    assert w.publish_review(**_publish_kwargs(), host=host) is False
    assert len(calls) == 2  # no final PATCH: the sweep retries, consumers fail closed
    assert host.rows == [] and lock.closed


@pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError, KeyError])
def test_publish_review_advisory_audit_failure_does_not_fail_publication(
    alice_io, error
):
    class BrokenAuditHost(writer.DefaultWriterHost):
        def append_advisories(self, *args):
            raise error("audit unavailable")

    fake_run, calls = _scripted_run(
        {
            ("gh", "api", "repos/o/r/issues/7/comments"): (0, "123\n"),
            ("gh", "api", "repos/o/r/pulls/7/reviews"): (0, ""),
            ("gh", "api", "--method", "PATCH"): (0, "https://x/1\n"),
        }
    )
    w, lock = _publish_io(alice_io, fake_run)
    assert w.publish_review(**_publish_kwargs(), host=BrokenAuditHost()) is True
    assert len(calls) == 3 and lock.closed


@pytest.mark.parametrize("failure", ["final_patch", "review_response"])
@pytest.mark.parametrize("changed_inline", [False, True])
def test_publish_review_retry_does_not_duplicate_native_threads(
    alice_io, failure, changed_inline
):
    w = writer.bind_io(alice_io)
    current = None
    reviews = []
    native_posts = 0
    lookups = []
    locks = []
    fail_once = True

    def fake_lock(repo, number):
        lock = _Lock()
        locks.append(lock)
        return lock

    def fake_run(cmd, **kw):
        nonlocal current, native_posts, fail_once
        if "--input" in cmd:
            native_posts += 1
            payload = json.loads(kw["input"])
            reviews.append(
                {
                    "body": payload["body"],
                    "commit_id": payload["commit_id"],
                    "user": {"login": "Alice-Agent"},
                    "state": "COMMENTED",
                }
            )
            if failure == "review_response" and fail_once:
                fail_once = False
                return subprocess.CompletedProcess(cmd, 1, "", "response lost")
        elif "--paginate" in cmd:
            lookups.append(cmd)
            # The matching review need not be on the first API page.
            return subprocess.CompletedProcess(cmd, 0, json.dumps([[], reviews]), "")
        else:
            body = next(arg[5:] for arg in cmd if arg.startswith("body="))
            if "--method" in cmd and "-publishing " not in body and fail_once:
                fail_once = False
                return subprocess.CompletedProcess(cmd, 1, "", "PATCH failed")
            current = {"id": 123, "body": body}
        return subprocess.CompletedProcess(cmd, 0, "123\n", "")

    alice_io.run = fake_run
    alice_io._publication_lock = fake_lock
    alice_io.find_marker_comment = lambda repo, number: current
    host = _AdvisoryHost()
    assert w.publish_review(**_publish_kwargs(), host=host) is False
    assert current is not None
    previous = w.marker_state(current["body"])
    assert previous["publishing_sha"] == "a" * 12
    assert "sha" not in previous and host.rows == []
    retry_kwargs = _publish_kwargs(existing=current, previous_state=previous)
    if changed_inline:
        retry_kwargs["inline"] = [{"path": "y.py", "line": 2, "body": "new finding"}]
    assert w.publish_review(**retry_kwargs, host=host) is True
    assert native_posts == (2 if changed_inline else 1)
    if changed_inline:
        assert reviews[0]["body"] != reviews[1]["body"]
    else:
        assert len(lookups) == 1 and "--slurp" in lookups[0]
    assert w.marker_state(current["body"])["sha"] == "a" * 12
    assert len(host.rows) == 1
    assert all(lock.closed for lock in locks)


@pytest.mark.parametrize(
    "lookup",
    [
        (1, "", "network error"),
        (0, "invalid json", ""),
        (0, '{"message": "API error"}', ""),
        (0, '[{"body": "not slurped"}]', ""),
        (0, "[[null]]", ""),
    ],
)
def test_publish_review_retry_fails_closed_on_unreadable_receipt(alice_io, lookup):
    w = writer.bind_io(alice_io)
    previous = {
        "publication_id": "retry-id",
        "publishing_sha": "a" * 12,
        "publishing_inline_sha256": hashlib.sha256(
            json.dumps(
                _publish_kwargs()["inline"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }
    current = {"id": 123, "body": w.provisional_marker_body("pending", previous)}
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--paginate" in cmd:
            assert kw["env"]["GH_API_CACHE_TTL"] == "0"
            return subprocess.CompletedProcess(cmd, *lookup)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    w, lock = _publish_io(alice_io, fake_run, current=current)
    assert not w.publish_review(
        **_publish_kwargs(existing=current, previous_state=previous)
    )
    assert len(calls) == 2  # provisional PATCH and lookup only; no POST/final PATCH
    assert "--input" not in calls[1] and lock.closed


@pytest.mark.parametrize(
    "receipt",
    [
        {},  # an interrupted publication that never reached the POST
        {"body": None},  # unrelated reviews can have no body
        {"user": {"login": "mallory"}},  # copied receipt is not ours
        {"commit_id": "b" * 40},
        {"body": "<!-- automated-ai-review-publication:other-id -->"},
        {"state": "PENDING"},
    ],
)
def test_publish_review_retry_posts_when_no_matching_receipt(alice_io, receipt):
    w = writer.bind_io(alice_io)
    previous = {
        "publication_id": "retry-id",
        "publishing_sha": "a" * 12,
        "publishing_inline_sha256": hashlib.sha256(
            json.dumps(
                _publish_kwargs()["inline"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }
    current = {"id": 123, "body": w.provisional_marker_body("pending", previous)}
    review = {
        "body": w.render_review_event_body("retry-id"),
        "user": {"login": "alice-agent"},
        "commit_id": "a" * 40,
        "state": "COMMENTED",
    }
    review.update(receipt)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps([[review]] if receipt else [[]]), ""
        )

    w, lock = _publish_io(alice_io, fake_run, current=current)
    assert w.publish_review(
        **_publish_kwargs(existing=current, previous_state=previous)
    )
    assert sum("--input" in cmd for cmd in calls) == 1
    assert len(calls) == 4 and lock.closed


def test_publish_review_yields_to_a_marker_that_appeared_while_analysing(alice_io):
    fake_run, calls = _scripted_run({})
    current = {"id": 9, "body": "<!-- alice-ai-review-publishing {} -->"}
    w, lock = _publish_io(alice_io, fake_run, current=current)
    assert w.publish_review(**_publish_kwargs()) is False
    assert calls == [] and lock.closed


def test_unreadable_comment_list_raises_instead_of_reading_as_no_marker(alice_io):
    # bobutils' gh_paginated_list reads a failed call as []; the marker read must not.
    alice_io.gh_paginated_list = lambda args: []
    alice_io.gh_json = lambda args: None  # breaker refusal / 403 / timeout
    w = writer.bind_io(alice_io)
    with pytest.raises(writer.ReviewError, match="cannot read o/r#7 comments"):
        w.find_marker_comment("o/r", 7)
    alice_io.gh_json = lambda args: [[]]  # a real, empty page
    assert w.find_marker_comment("o/r", 7) is None


@pytest.mark.parametrize(
    "existing", [None, {"id": 123, "body": "<!-- alice-ai-review {} -->"}]
)
def test_refused_marker_reread_publishes_nothing(alice_io, capsys, existing):
    calls: list[list[str]] = []
    alice_io.gh_json = lambda args, **kw: None  # REST core exhausted
    alice_io.gh_paginated_list = lambda args: []
    alice_io.run = lambda cmd, **kw: calls.append(cmd)
    alice_io._publication_lock = lambda repo, number: _Lock()
    w = writer.bind_io(alice_io)
    assert (
        w.publish_review(**_publish_kwargs(existing=existing), host=_AdvisoryHost())
        is False
    )
    err = capsys.readouterr().err
    assert "cannot read o/r#7 comments" in err
    assert "disappeared" not in err
    assert calls == []  # no POST/PATCH: an unreadable marker must not be duplicated
