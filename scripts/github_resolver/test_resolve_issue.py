"""Tests for resolve_issue orchestrator.

These cover the pure parts the prototype must get right before it is pointed
at a real repo: the stable idempotency marker on comments, the prompt
template round-trip, and the `RESOLVER_STATUS`/`RESOLVER_SUMMARY` parser.

External I/O (`gh`, `gptme`, `git`) is stubbed — those are thin subprocess
wrappers exercised in the staged rollout, not in unit tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import resolve_issue  # type: ignore[import-not-found]  # noqa: E402,I001


REPO = "gptme/gptme-contrib"
TEMPLATE_PATH = Path(__file__).parent / "prompts" / "issue-resolver.md"


def _issue(**overrides):
    base = dict(
        number=42,
        title="gptme crashes on empty prompt",
        body="Steps:\n1. Run gptme\n2. Hit enter\n\nCrash.",
        author="alice",
        labels=["bug"],
    )
    base.update(overrides)
    return resolve_issue.Issue(**base)


def test_marker_is_stable_and_versioned():
    assert resolve_issue.MARKER_COMMENT.startswith("<!--")
    assert resolve_issue.MARKER_COMMENT.endswith("v1 -->")


def test_branch_name_is_deterministic():
    assert resolve_issue.branch_for_issue(42) == "gptme-resolver/issue-42"
    assert resolve_issue.branch_for_issue(1) == "gptme-resolver/issue-1"


def test_render_prompt_substitutes_every_placeholder():
    template = TEMPLATE_PATH.read_text()
    rendered = resolve_issue.render_prompt(template, repo=REPO, issue=_issue())
    for placeholder in (
        "{repo}",
        "{issue_number}",
        "{issue_title}",
        "{issue_author}",
        "{issue_labels}",
        "{issue_body}",
    ):
        assert placeholder not in rendered, f"unfilled placeholder: {placeholder}"
    assert REPO in rendered
    assert "42" in rendered
    assert "gptme crashes on empty prompt" in rendered
    assert "@alice" in rendered


def test_render_prompt_handles_empty_body_and_no_labels():
    template = TEMPLATE_PATH.read_text()
    rendered = resolve_issue.render_prompt(
        template, repo=REPO, issue=_issue(body="", labels=[])
    )
    assert "(empty body)" in rendered
    assert "(none)" in rendered


def test_render_prompt_does_not_crash_on_format_conflicts():
    # Issue bodies often contain `{` / `}` (JSON snippets, f-string examples).
    # The renderer must not treat those as format placeholders.
    template = TEMPLATE_PATH.read_text()
    tricky = _issue(body='{"error": "unexpected {bracket}"}')
    rendered = resolve_issue.render_prompt(template, repo=REPO, issue=tricky)
    assert '"error": "unexpected {bracket}"' in rendered


def test_parse_status_changes_extracts_summary():
    out = (
        "... work ...\n"
        "RESOLVER_STATUS: changes\n"
        "RESOLVER_SUMMARY: Fixed the crash by guarding the empty-prompt branch.\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "changes"
    assert "empty-prompt" in message


def test_parse_status_no_changes_extracts_reason():
    out = (
        "RESOLVER_STATUS: no_changes\n"
        "RESOLVER_REASON: Needs product direction on retry semantics.\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "no_changes"
    assert message.startswith("Needs product direction")


def test_parse_status_missing_marker_returns_error():
    out = "model chatted about the issue but forgot the marker.\n"
    status, message = resolve_issue.parse_status(out)
    assert status == "error"
    assert "RESOLVER_STATUS" in message


def test_parse_status_is_last_line_tolerant():
    # The marker can appear anywhere — we anchor on the line, not the tail.
    out = (
        "pre-amble\n"
        "RESOLVER_STATUS: changes\n"
        "RESOLVER_SUMMARY: tightened regex\n"
        "post-amble junk\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "changes"
    assert message == "tightened regex"


def test_write_output_creates_dir(tmp_path):
    resolve_issue.write_output(
        tmp_path / "out", "status.json", json.dumps({"ok": True})
    )
    assert (tmp_path / "out" / "status.json").exists()


def test_prompt_template_mentions_both_markers():
    # The prompt contract itself has to advertise both markers or the model
    # will never emit them — guard against silent template rot.
    template = TEMPLATE_PATH.read_text()
    assert "RESOLVER_STATUS: changes" in template
    assert "RESOLVER_STATUS: no_changes" in template
    assert "RESOLVER_SUMMARY" in template
    assert "RESOLVER_REASON" in template


def test_prompt_template_requires_read_before_editing_existing_files():
    template = TEMPLATE_PATH.read_text()
    assert "Before calling `patch` or" in template
    assert "`save` on any file that already exists" in template
    assert "Do NOT write patch chunks from memory" in template


def test_status_regex_ignores_prose_mentions():
    # The model often echoes the words "RESOLVER_STATUS" in the middle of a
    # sentence while explaining what it's about to do. Only an anchored line
    # of the exact form should count.
    prose = "I'll now emit RESOLVER_STATUS: changes inline like this."
    status, _ = resolve_issue.parse_status(prose)
    assert status == "error"


def test_extract_tool_errors_returns_empty_when_none():
    out = "RESOLVER_STATUS: changes\nRESOLVER_SUMMARY: something\n"
    assert resolve_issue.extract_tool_errors(out) == []


def test_extract_tool_errors_finds_errors():
    out = (
        "some work\n"
        "System: Error during execution: Patch failed: original chunk not found in file\n"
        "more work\n"
        "System: Error during execution: another failure\n"
        "RESOLVER_STATUS: changes\n"
    )
    errors = resolve_issue.extract_tool_errors(out)
    assert len(errors) == 2
    assert "Patch failed" in errors[0]
    assert "another failure" in errors[1]


def test_extract_tool_errors_deduplicates_consecutive_identical_errors():
    out = (
        "System: Error during execution: Patch failed: original chunk not found in file\n"
        "System: Error during execution: Patch failed: original chunk not found in file\n"
    )
    errors = resolve_issue.extract_tool_errors(out)
    assert len(errors) == 1


def test_extract_tool_errors_handles_empty_string():
    assert resolve_issue.extract_tool_errors("") == []


def test_count_write_tool_calls_returns_zero_when_none():
    out = "RESOLVER_STATUS: changes\nRESOLVER_SUMMARY: something\n"
    assert resolve_issue.count_write_tool_calls(out) == 0


def test_count_write_tool_calls_counts_patch_and_save():
    out = (
        "Let me patch the file:\n"
        "```patch src/foo.py\n"
        "<<<<<<< ORIGINAL\nold\n=======\nnew\n>>>>>>> UPDATED\n"
        "```\n"
        "System: Error during execution: Patch failed: original chunk not found\n"
        "Let me try save instead:\n"
        "```save src/bar.py\nnew content\n```\n"
        "System: Error during execution: save failed\n"
    )
    assert resolve_issue.count_write_tool_calls(out) == 2


def test_count_write_tool_calls_handles_empty_string():
    assert resolve_issue.count_write_tool_calls("") == 0


def test_main_reclassifies_status_when_all_writes_errored_despite_dirty_worktree(
    monkeypatch, tmp_path
):
    """Canary-#824 regression: submodule bump + all patches failed → no bogus draft PR."""
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    out_dir = tmp_path / "out"

    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        ["git", "clone", str(bare), str(repo)], check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "master"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "resolver-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "resolver-test@example.com"],
        check=True,
    )
    (repo / "note.txt").write_text("old\n")
    subprocess.run(["git", "-C", str(repo), "add", "note.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "-u",
            "origin",
            "master",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    issue = resolve_issue.Issue(
        number=42,
        title="Fix note",
        body="Update note",
        author="alice",
        labels=["bug"],
    )
    comments: list[str] = []
    draft_prs: list[str] = []

    def fake_fetch_issue(repo_name, issue_number):
        return issue

    gptme_stdout = (
        "Let me patch the file:\n"
        "```patch note.txt\n"
        "<<<<<<< ORIGINAL\nold\n=======\nnew\n>>>>>>> UPDATED\n"
        "```\n"
        "System: Error during execution: Patch failed: original chunk not found in file\n"
        "RESOLVER_STATUS: changes\n"
        "RESOLVER_SUMMARY: Updated note.\n"
    )

    def fake_run_gptme(prompt, *, model=None, workdir):
        # Simulate incidental worktree change (e.g. submodule bump) without a
        # successful patch — the worktree is dirty but all writes errored.
        (workdir / "incidental.txt").write_text("side-effect\n")
        return (gptme_stdout, "")

    def fake_comment(repo_name, issue_number, body):
        comments.append(body)

    def fake_open_draft_pr(repo_name, issue_number, branch, message):
        draft_prs.append(message)
        return "https://github.com/gptme/gptme-contrib/pull/99"

    monkeypatch.setattr(resolve_issue, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(resolve_issue, "run_gptme", fake_run_gptme)
    monkeypatch.setattr(resolve_issue, "post_issue_comment", fake_comment)
    monkeypatch.setattr(resolve_issue, "open_draft_pr", fake_open_draft_pr)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = resolve_issue.main(
        [
            "--repo",
            "local/test",
            "--issue",
            "42",
            "--workdir",
            str(repo),
            "--output-dir",
            str(out_dir),
        ]
    )

    assert rc == 0
    # Must NOT open a draft PR when all writes failed.
    assert draft_prs == [], f"Unexpected draft PR opened: {draft_prs}"
    # Must comment with the error and tool error details.
    assert len(comments) == 1
    assert "write tool call" in comments[0]
    assert "Patch failed" in comments[0]


def test_open_draft_pr_retrigger_returns_existing_url(monkeypatch):
    # On re-trigger, `gh pr create` exits 1 ("a pull request … already exists").
    # open_draft_pr must catch CalledProcessError and return the existing PR URL
    # via `gh pr view` so the caller can still post the issue comment.
    import subprocess

    calls: list[list[str]] = []

    def fake_gh(args, *, check=True):
        calls.append(args)
        if args[0] == "pr" and args[1] == "create":
            raise subprocess.CalledProcessError(1, "gh")
        if args[0] == "pr" and args[1] == "view":
            return "https://github.com/gptme/gptme-contrib/pull/99\n"
        return ""

    monkeypatch.setattr(resolve_issue, "gh", fake_gh)
    url = resolve_issue.open_draft_pr(
        "gptme/gptme-contrib", 42, "gptme-resolver/issue-42", "fixed the crash"
    )
    assert url == "https://github.com/gptme/gptme-contrib/pull/99"
    # Verify we fell back to `gh pr view`
    assert any(a[0] == "pr" and a[1] == "view" for a in calls)


def test_run_gptme_uses_restricted_tools_and_sanitized_env(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    shim_root = tmp_path / "shims"
    shim_root.mkdir()

    class FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    class FakeTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return str(shim_root)

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

    def fake_run(*args, **kwargs):
        captured["args"] = args[0]
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return FakeResult()

    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(resolve_issue.subprocess, "run", fake_run)
    monkeypatch.setattr(
        resolve_issue.tempfile, "TemporaryDirectory", FakeTemporaryDirectory
    )

    stdout, stderr = resolve_issue.run_gptme(
        "prompt", model="claude-sonnet", workdir=tmp_path
    )

    assert stdout == "ok"
    assert stderr == ""
    assert captured["args"] == [
        "gptme",
        "--non-interactive",
        "--no-confirm",
        "--tools",
        resolve_issue.RESOLVER_TOOL_ALLOWLIST,
        "--model",
        "claude-sonnet",
        "-",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert Path(env["PATH"].split(os.pathsep)[0]) == shim_root
    assert (shim_root / "git").exists()
    assert (shim_root / "gh").exists()
    assert captured["cwd"] == tmp_path


def test_detect_repo_state_violation_on_unexpected_commit(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "resolver-test"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "resolver-test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "note.txt").write_text("old\n")
    subprocess.run(["git", "add", "note.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "initial"],
        cwd=tmp_path,
        check=True,
    )

    baseline = resolve_issue.capture_repo_state(tmp_path)

    (tmp_path / "note.txt").write_text("new\n")
    subprocess.run(["git", "add", "note.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "agent direct commit",
        ],
        cwd=tmp_path,
        check=True,
    )

    violation = resolve_issue.detect_repo_state_violation(baseline, tmp_path)
    assert violation is not None
    assert "HEAD moved" in violation
    assert "master" in violation
    assert resolve_issue.has_git_changes(tmp_path) is False


def test_push_branch_uses_explicit_github_token(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_git(args, *, check=True, cwd=None):
        del check
        assert cwd == tmp_path
        calls.append(args)
        return ""

    monkeypatch.setattr(resolve_issue, "git", fake_git)

    resolve_issue.push_branch(
        tmp_path,
        "gptme-resolver/issue-42",
        repo="gptme/gptme-contrib",
        auth_token="token-123",
    )

    assert calls == [
        [
            "push",
            "--force-with-lease",
            "https://x-access-token:token-123@github.com/gptme/gptme-contrib.git",
            "HEAD:refs/heads/gptme-resolver/issue-42",
        ]
    ]


def test_main_preserves_direct_commit_as_partial_attempt(monkeypatch, tmp_path):
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    out_dir = tmp_path / "out"

    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        ["git", "clone", str(bare), str(repo)], check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "master"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "resolver-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "resolver-test@example.com"],
        check=True,
    )
    (repo / "note.txt").write_text("old\n")
    subprocess.run(["git", "-C", str(repo), "add", "note.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "-u",
            "origin",
            "master",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    issue = resolve_issue.Issue(
        number=42,
        title="Fix note",
        body="Update note",
        author="alice",
        labels=["bug"],
    )
    comments: list[str] = []

    def fake_fetch_issue(repo_name, issue_number):
        assert repo_name == "local/test"
        assert issue_number == 42
        return issue

    def fake_run_gptme(prompt, *, model=None, workdir):
        del prompt, model
        (workdir / "note.txt").write_text("new\n")
        subprocess.run(["git", "-C", str(workdir), "add", "note.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workdir),
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-q",
                "-m",
                "agent direct commit",
            ],
            check=True,
        )
        return ("RESOLVER_STATUS: changes\nRESOLVER_SUMMARY: Updated note.\n", "")

    def fake_comment(repo_name, issue_number, body):
        assert repo_name == "local/test"
        assert issue_number == 42
        comments.append(body)

    def fail_gh(args, *, check=True):
        del check
        raise AssertionError(f"unsafe direct-commit path must not call gh: {args}")

    monkeypatch.setattr(resolve_issue, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(resolve_issue, "run_gptme", fake_run_gptme)
    monkeypatch.setattr(resolve_issue, "post_issue_comment", fake_comment)
    monkeypatch.setattr(resolve_issue, "gh", fail_gh)
    # Prevent GitHub Actions' auto-injected GITHUB_TOKEN / GH_TOKEN from
    # routing push_branch to the real GitHub instead of the local bare repo.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = resolve_issue.main(
        [
            "--repo",
            "local/test",
            "--issue",
            "42",
            "--workdir",
            str(repo),
            "--output-dir",
            str(out_dir),
        ]
    )

    assert rc == 0
    assert len(comments) == 1
    assert "mutated git state directly" in comments[0]
    assert (
        "Partial attempt preserved on branch `gptme-resolver/issue-42`." in comments[0]
    )

    refs = subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "master" in refs
    assert "gptme-resolver/issue-42" in refs

    branch_head = subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "log",
            "--oneline",
            "gptme-resolver/issue-42",
            "-1",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent direct commit" in branch_head
