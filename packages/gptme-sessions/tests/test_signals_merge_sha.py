"""Tests for server-side merge-commit SHA resolution in signals.py.

Bug: sessions that shipped via `gh pr merge --squash` never observed the
server-side merge-commit SHA, so those commits went missing from
`deliverables` and could not be reverse-indexed for revert attribution.

Fix: after extraction, `extract_signals_cc` calls `_resolve_merge_shas`,
which invokes `gh pr view <N> --repo <owner/repo> --json mergeCommit` for
each detected merge and adds the returned SHA to `git_commits` and
`deliverables`.

These tests mock `subprocess.run` at the `gptme_sessions.signals` module
scope so no real `gh` binary is ever invoked.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from gptme_sessions.signals import (
    _resolve_merge_shas,
    extract_signals_cc,
)

# 40-char hex — mimics a real git SHA.
FAKE_SHA = "a" * 40
FAKE_SHA_2 = "b" * 40


def _make_mock_run(stdout: str = "", returncode: int = 0):
    """Build a lambda usable as a subprocess.run side_effect."""

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return _run


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_merge_shas: direct unit tests
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_merge_shas_success():
    """gh returns a valid 40-char SHA; it's appended to the result list."""
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout=FAKE_SHA + "\n"),
    ) as mock_run:
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={42: "owner/repo"})
    assert shas == [FAKE_SHA]
    mock_run.assert_called_once()
    # Verify the gh invocation form.
    call_args = mock_run.call_args.args[0]
    assert call_args[:3] == ["gh", "pr", "view"]
    assert "42" in call_args
    assert "owner/repo" in call_args
    assert "mergeCommit" in call_args


def test_resolve_merge_shas_missing_context():
    """PR with no known repo → no subprocess call, no SHA returned."""
    with patch("gptme_sessions.signals.subprocess.run") as mock_run:
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={})
    assert shas == []
    mock_run.assert_not_called()


def test_resolve_merge_shas_gh_failure_nonzero_exit():
    """gh exits non-zero (auth error, PR not found, etc.) → graceful skip."""
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout="", returncode=1),
    ):
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={42: "owner/repo"})
    assert shas == []


def test_resolve_merge_shas_gh_timeout():
    """gh call times out → graceful skip, no exception raised."""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)

    with patch("gptme_sessions.signals.subprocess.run", side_effect=_timeout):
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={42: "owner/repo"})
    assert shas == []


def test_resolve_merge_shas_gh_oserror():
    """gh binary missing (OSError/FileNotFoundError) → graceful skip."""

    def _oserror(*args, **kwargs):
        raise FileNotFoundError("gh: command not found")

    with patch("gptme_sessions.signals.subprocess.run", side_effect=_oserror):
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={42: "owner/repo"})
    assert shas == []


def test_resolve_merge_shas_malformed_output():
    """gh returns garbage / short hash → not counted as a valid SHA."""
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout="not-a-sha\n"),
    ):
        shas = _resolve_merge_shas(pr_merges=["PR #42"], pr_context={42: "owner/repo"})
    assert shas == []


def test_resolve_merge_shas_empty_list():
    """No PRs merged → no subprocess calls, empty result."""
    with patch("gptme_sessions.signals.subprocess.run") as mock_run:
        shas = _resolve_merge_shas(pr_merges=[], pr_context={})
    assert shas == []
    mock_run.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Integration: extract_signals_cc end-to-end with merge-SHA resolution
# ─────────────────────────────────────────────────────────────────────────────


def _pr_create_then_merge_msgs(
    pr_num: int = 42,
    owner_repo: str = "owner/repo",
) -> list[dict]:
    """Build a CC trajectory: gh pr create succeeds, then gh pr merge --squash."""
    create_id = "bash_create_001"
    merge_id = "bash_merge_001"
    pr_url = f"https://github.com/{owner_repo}/pull/{pr_num}"
    return [
        {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": create_id,
                        "name": "Bash",
                        "input": {"command": "gh pr create --title 'fix: thing' --body 'body'"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-17T10:00:05.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": create_id,
                        "is_error": False,
                        "content": f"Creating pull request\n{pr_url}",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-17T10:01:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": merge_id,
                        "name": "Bash",
                        "input": {"command": f"gh pr merge {pr_num} --squash"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-17T10:01:10.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": merge_id,
                        "is_error": False,
                        "content": (
                            f"\u2713 Squashed and merged pull request #{pr_num} (fix: thing)"
                        ),
                    }
                ],
            },
        },
    ]


def test_extract_signals_cc_includes_resolved_merge_sha():
    """End-to-end: pr create URL → pr_context → gh pr view mock → merge SHA in deliverables."""
    msgs = _pr_create_then_merge_msgs(pr_num=42, owner_repo="ErikBjare/bob")
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout=FAKE_SHA + "\n"),
    ) as mock_run:
        sigs = extract_signals_cc(msgs)

    # pr_merges still captured as usual.
    assert sigs["pr_merges"] == ["PR #42"]
    assert sigs["prs_submitted"] == ["PR #42"]

    # Fake merge SHA now appears in git_commits AND deliverables.
    assert any(
        FAKE_SHA in c for c in sigs["git_commits"]
    ), f"expected {FAKE_SHA} in git_commits, got {sigs['git_commits']}"
    assert any(
        FAKE_SHA in d for d in sigs["deliverables"]
    ), f"expected {FAKE_SHA} in deliverables, got {sigs['deliverables']}"

    # Exactly one gh-view call — for PR #42.
    mock_run.assert_called_once()
    args = mock_run.call_args.args[0]
    assert args[:3] == ["gh", "pr", "view"]
    assert "ErikBjare/bob" in args
    assert "42" in args


def test_extract_signals_cc_uses_repo_flag_from_merge_cmd():
    """No pr create URL but `gh pr merge --repo X/Y 99` provides repo context."""
    merge_id = "bash_merge_with_repo_flag"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": merge_id,
                        "name": "Bash",
                        "input": {"command": "gh pr merge --repo gptme/gptme-contrib 99 --squash"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-17T10:00:10.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": merge_id,
                        "is_error": False,
                        "content": "\u2713 Squashed and merged pull request #99 (fix: y)",
                    }
                ],
            },
        },
    ]
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout=FAKE_SHA_2 + "\n"),
    ) as mock_run:
        sigs = extract_signals_cc(msgs)
    assert sigs["pr_merges"] == ["PR #99"]
    assert any(FAKE_SHA_2 in d for d in sigs["deliverables"])
    assert any(FAKE_SHA_2 in c for c in sigs["git_commits"])
    # Verify the repo from `--repo` was used for the lookup.
    args = mock_run.call_args.args[0]
    assert "gptme/gptme-contrib" in args


def test_extract_signals_cc_gh_failure_does_not_break_extraction():
    """When gh pr view fails, extraction still succeeds and pr_merges is intact."""
    msgs = _pr_create_then_merge_msgs(pr_num=42, owner_repo="owner/repo")
    with patch(
        "gptme_sessions.signals.subprocess.run",
        side_effect=_make_mock_run(stdout="", returncode=1),
    ):
        sigs = extract_signals_cc(msgs)
    # Still records the merge — just no resolved SHA.
    assert sigs["pr_merges"] == ["PR #42"]
    # No fake sha added.
    assert not any(FAKE_SHA in c for c in sigs["git_commits"])
    # deliverables still contains the human-readable "merge PR #42" entry.
    assert any("merge PR #42" in d for d in sigs["deliverables"])


def test_extract_signals_cc_no_pr_merges_no_gh_calls():
    """Regression: sessions with no pr merges do NOT invoke gh pr view."""
    # Minimal trajectory: just an assistant write, no gh pr merge.
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/tmp/x.py", "content": "x = 1"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-17T10:00:05.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "w1",
                        "is_error": False,
                        "content": "wrote 1 line",
                    }
                ],
            },
        },
    ]
    with patch("gptme_sessions.signals.subprocess.run") as mock_run:
        sigs = extract_signals_cc(msgs)
    mock_run.assert_not_called()
    assert sigs["pr_merges"] == []
    # No fake SHA leaked in.
    assert not any(FAKE_SHA in c for c in sigs["git_commits"])


def test_extract_signals_cc_merge_no_repo_context_skips_lookup():
    """`gh pr merge` without --repo and without a prior pr create → no lookup."""
    merge_id = "bash_merge_no_context"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-04-17T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": merge_id,
                        "name": "Bash",
                        "input": {"command": "gh pr merge 55 --squash"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-17T10:00:10.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": merge_id,
                        "is_error": False,
                        "content": "\u2713 Squashed and merged pull request #55 (fix: z)",
                    }
                ],
            },
        },
    ]
    with patch("gptme_sessions.signals.subprocess.run") as mock_run:
        sigs = extract_signals_cc(msgs)
    # Merge still recorded.
    assert sigs["pr_merges"] == ["PR #55"]
    # But no gh lookup — we have no repo for this PR.
    mock_run.assert_not_called()
