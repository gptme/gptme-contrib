"""Candidate selection is portable: a host supplies GitHub, identity is explicit."""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gptme_review.sweep import (
    GhCliHost,
    SweepConfig,
    classify_all_failed_run,
    had_findings,
    hunks_from_patch,
    is_benign_file,
    is_likely_duplicate,
    marker_findings_non_blocking,
    marker_payload,
    marker_state,
    needs_consensus_upgrade,
    pending_review_request,
    select_candidates,
    self_merge_eligible_from_marker,
    sweep_identity,
)

NOW = datetime(2026, 8, 7, 22, 0, 0, tzinfo=UTC)
IDENTITY = sweep_identity("reviewer-bot", "agent-ai-review")
OTHER = sweep_identity("peer-agent", "peer-ai-review")


class FakeHost:
    def __init__(
        self,
        prs: list[dict] | None,
        comments: dict[int, list[dict] | None] | None = None,
        *,
        outage: bool = False,
        mergeability: dict | None = None,
        files: dict[int, list[str]] | None = None,
        patches: dict[int, str] | None = None,
        review_only: bool = False,
    ) -> None:
        self.prs = prs
        self.comments = comments or {}
        self.outage = outage
        self.mergeability = mergeability or {}
        self.files = files or {}
        self.patches = patches or {}
        self.review_only = review_only
        self.reactions: list[tuple[str, int, str]] = []

    def list_open_prs(self, repo: str) -> list[dict] | None:
        return self.prs

    def pr_comments(self, repo: str, number: int) -> list[dict] | None:
        comments = self.comments.get(number, [])
        return list(comments) if comments is not None else None

    def pr_mergeability(self, repo: str, number: int) -> dict:
        return dict(self.mergeability)

    def openrouter_unavailable(self) -> bool:
        return self.outage

    def add_reaction(self, repo: str, comment_id: int, content: str) -> bool:
        self.reactions.append((repo, comment_id, content))
        return True

    def fetch_pr_files(self, repo: str, number: int) -> list[str]:
        return list(self.files.get(number, []))

    def fetch_pr_patch(self, repo: str, number: int) -> str:
        return self.patches.get(number, "")

    def is_review_only_blocker(self, mergeability: Mapping[str, object]) -> bool:
        return self.review_only


def _pr(
    number: int,
    head: str,
    *,
    draft: bool = False,
    age_secs: int = 3600,
    author: str = "reviewer-bot",
) -> dict:
    return {
        "number": number,
        "headRefOid": head,
        "isDraft": draft,
        "updatedAt": (NOW - timedelta(seconds=age_secs)).isoformat(),
        "title": f"pr {number}",
        "author": {"login": author},
    }


def _marker_comment(
    sha: str,
    *,
    prefix: str = "agent-ai-review",
    extra: dict | None = None,
    comment_id: int = 1,
) -> dict:
    payload = {"sha": sha, **(extra or {})}
    body = f"<!-- {prefix} {json.dumps(payload)} -->"
    return {
        "id": comment_id,
        "body": body,
        "updated_at": "2026-08-07T21:00:00+00:00",
        "user": {"login": "reviewer-bot"},
        "author_association": "MEMBER",
    }


def _config(*, authors: tuple[str, ...] | None = ("reviewer-bot",)) -> SweepConfig:
    return SweepConfig(
        identity=IDENTITY,
        authors=authors,
        consensus_upgrade_repos=frozenset({"o/r"}),
    )


def _numbers(out: list[dict] | None) -> list[int]:
    assert out is not None
    return [p["number"] for p in out]


def test_sweep_module_has_no_brain_or_agent_literals() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "gptme_review" / "sweep.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    banned = (
        "TimeToBuildBob",
        "TimeToLearnAlice",
        "ErikBjare/bob",
        "ErikBjare/alice",
        "bob-ai-review",
    )
    text = ast.dump(tree)
    for token in banned:
        assert token not in text
    assert "AI_REVIEW_REPO_ROOT" not in source.read_text(encoding="utf-8")


def test_malformed_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="login"):
        sweep_identity("not a login", "agent-ai-review")
    with pytest.raises(ValueError, match="marker_prefix"):
        sweep_identity("reviewer-bot", "Not_a_slug")


def test_unreviewed_pr_is_a_candidate() -> None:
    host = FakeHost([_pr(1, "abc123def456")])
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [1]


def test_failed_comment_read_skips_only_that_pr_without_reactions() -> None:
    host = FakeHost([_pr(1, "abc123"), _pr(2, "def456")], {1: None, 2: []})
    found = select_candidates("o/r", _config(), host, NOW, dry_run=False)
    assert _numbers(found) == [2]
    assert found is not None and found[0]["never_attempted"] is True
    assert host.reactions == []


def test_pr_reviewed_at_current_head_is_skipped() -> None:
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {1: [_marker_comment("abc123")]},
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_short_marker_sha_still_matches_full_head() -> None:
    host = FakeHost(
        [_pr(1, "abc123def456789")],
        {1: [_marker_comment("abc123")]},
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_pr_pushed_since_its_review_is_a_candidate() -> None:
    host = FakeHost(
        [_pr(1, "fff123def456")],
        {1: [_marker_comment("abc123")]},
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [1]


def test_foreign_marker_is_not_this_reviewers() -> None:
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {1: [_marker_comment("abc123", prefix="peer-ai-review")]},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1]
    assert found is not None and found[0]["never_attempted"] is True


def test_trusted_review_command_forces_fresh_review() -> None:
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "maintainer"},
        "author_association": "MEMBER",
    }
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {1: [_marker_comment("abc123"), request]},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1]
    assert found is not None and found[0]["requested_by"] == "maintainer"


def test_untrusted_review_command_does_not_spend_review_budget() -> None:
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "drive-by"},
        "author_association": "NONE",
    }
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {1: [_marker_comment("abc123"), request]},
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_draft_prs_are_skipped() -> None:
    host = FakeHost([_pr(1, "abc123def456", draft=True)])
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_very_recent_push_is_left_to_settle() -> None:
    host = FakeHost([_pr(1, "abc123def456", age_secs=30)])
    assert (
        _numbers(
            select_candidates(
                "o/r",
                _config(),
                host,
                NOW,
                dry_run=True,
            )
        )
        == []
    )


def test_recent_push_is_reviewed_once_past_the_window() -> None:
    host = FakeHost([_pr(1, "abc123def456", age_secs=181)])
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [1]


def test_unlistable_repo_yields_none() -> None:
    host = FakeHost(None)
    assert select_candidates("o/r", _config(), host, NOW, dry_run=True) is None


def test_external_contributor_pr_is_not_reviewed_unprompted() -> None:
    host = FakeHost([_pr(1, "abc123def456", author="outsider")])
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_author_filter_can_be_disabled_explicitly() -> None:
    host = FakeHost([_pr(1, "abc123def456", author="outsider")])
    found = select_candidates("o/r", _config(authors=None), host, NOW, dry_run=True)
    assert _numbers(found) == [1]


def test_trusted_request_opts_in_external_contributor() -> None:
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "maintainer"},
        "author_association": "OWNER",
    }
    host = FakeHost(
        [_pr(1, "abc123def456", author="outsider")],
        {1: [request]},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1]


def test_outage_single_pass_is_not_upgraded_while_outage_lasts() -> None:
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123",
                    extra={"engine": "llm", "outage_single_pass": True, "score": 5},
                )
            ]
        },
        outage=True,
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == []


def test_clean_first_pass_in_upgrade_repo_is_a_consensus_candidate() -> None:
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123", extra={"engine": "llm", "score": 5, "findings": []}
                )
            ]
        },
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1]
    assert found is not None and found[0].get("consensus_upgrade") is True


def test_dry_run_does_not_write_reactions() -> None:
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "maintainer"},
        "author_association": "MEMBER",
    }
    host = FakeHost([_pr(1, "abc123def456")], {1: [request]})
    select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert host.reactions == []
    select_candidates("o/r", _config(), host, NOW, dry_run=False)
    assert host.reactions == [("o/r", 9, "eyes")]


def test_subscription_defer_exhaustion_skips_same_head() -> None:
    host = FakeHost([_pr(1, "abc123def456")])
    found = select_candidates(
        "o/r",
        _config(),
        host,
        NOW,
        dry_run=True,
        subscription_defers={"o/r#1": {"head": "abc123def456", "count": 3}},
    )
    assert _numbers(found) == []


def test_fix_iteration_is_flagged_when_head_moved_past_findings() -> None:
    host = FakeHost(
        [_pr(1, "fff123def456")],
        {1: [_marker_comment("abc123", extra={"score": 3, "findings": [{"fp": "x"}]})]},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert found is not None and found[0]["is_fix_iteration"] is True


def test_needs_consensus_upgrade_on_clean_first_pass() -> None:
    assert needs_consensus_upgrade({"sha": "abc", "score": 5, "findings": []}, "abcdef")


def test_needs_consensus_upgrade_false_when_requested_two() -> None:
    marker = {
        "sha": "abc",
        "score": 5,
        "findings": [],
        "consensus": {"requested": 2, "answered": 2},
    }
    assert needs_consensus_upgrade(marker, "abcdef") is False


def test_marker_helpers_are_identity_scoped() -> None:
    comments = [_marker_comment("abc123")]
    parsed, comment = marker_payload(comments, IDENTITY)
    assert parsed["sha"] == "abc123"
    assert comment is not None
    assert marker_payload(comments, OTHER) == ({}, None)
    sha, _served = marker_state(comments, IDENTITY)
    assert sha == "abc123"
    assert marker_state(comments, OTHER) == (None, None)


@pytest.mark.parametrize("prefix", ["agent-ai-review", "agent-ai-review-publishing"])
@pytest.mark.parametrize(
    "user", [{"login": "outsider"}, {"login": "peer-agent"}, {}, None]
)
def test_forged_marker_cannot_suppress_review(prefix: str, user: object) -> None:
    forged = _marker_comment(
        "abc123",
        prefix=prefix,
        extra={
            "score": 5,
            "findings": [],
            "consensus": {"requested": 2, "answered": 2},
        },
    )
    forged["user"] = user
    # Even a maintainer cannot impersonate the configured reviewer.
    assert marker_payload([forged], IDENTITY) == ({}, None)
    assert marker_state([forged], IDENTITY) == (None, None)
    legitimate = _marker_comment("old123", comment_id=2)
    assert marker_payload([forged, legitimate], IDENTITY)[0]["sha"] == "old123"
    assert marker_state([forged, legitimate], IDENTITY)[0] == "old123"
    host = FakeHost([_pr(1, "abc123def456")], {1: [forged]})
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1]
    assert found is not None and found[0]["never_attempted"] is True


def test_marker_author_login_is_case_insensitive() -> None:
    comment = _marker_comment("abc123")
    comment["user"] = {"login": "Reviewer-Bot"}
    assert marker_payload([comment], IDENTITY)[0]["sha"] == "abc123"
    assert marker_state([comment], IDENTITY)[0] == "abc123"


def test_pending_request_ignores_other_reviewers_trigger() -> None:
    comments = [
        {
            "id": 9,
            "body": "@peer-agent review",
            "created_at": "2026-08-07T21:30:00+00:00",
            "user": {"login": "maintainer"},
            "author_association": "MEMBER",
        }
    ]
    assert pending_review_request(comments, None, IDENTITY) is None
    assert pending_review_request(comments, None, OTHER) is not None


def test_p2_only_marker_is_non_blocking() -> None:
    marker = {
        "findings": [{"fp": "a", "severity": "P2"}],
        "dispositions": {},
    }
    assert marker_findings_non_blocking(marker) is True
    assert had_findings({"score": 4}) is True
    assert had_findings({"score": 5, "history": [{"findings": 0}]}) is False


def test_self_merge_preflight_requires_answered_consensus() -> None:
    marker = {
        "findings": [{"fp": "a", "severity": "P2"}],
        "consensus": {"requested": 2, "answered": 2, "min_agreement": 2},
    }
    assert self_merge_eligible_from_marker(marker) is True
    assert (
        self_merge_eligible_from_marker({"findings": [{"fp": "a", "severity": "P2"}]})
        is False
    )


def test_classify_all_failed_run_shapes() -> None:
    assert (
        classify_all_failed_run(
            attempted=1, failures=1, rate_limit_failures=0, too_large_failures=0
        )
        == "singleton"
    )
    assert (
        classify_all_failed_run(
            attempted=3, failures=3, rate_limit_failures=3, too_large_failures=0
        )
        == "rate_limited"
    )
    assert (
        classify_all_failed_run(
            attempted=3, failures=3, rate_limit_failures=0, too_large_failures=0
        )
        == "systemic"
    )
    assert (
        classify_all_failed_run(
            attempted=3, failures=1, rate_limit_failures=0, too_large_failures=0
        )
        is None
    )


def test_is_benign_file_and_hunks() -> None:
    assert is_benign_file("uv.lock")
    assert is_benign_file("gptme-contrib")
    assert not is_benign_file("src/foo.py")
    patch = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -10,2 +10,3 @@
 keep
+added
"""
    assert hunks_from_patch(patch) == {"src/foo.py": {10}}


def test_single_hunk_overlap_between_small_prs_is_duplicate() -> None:
    files = {"src/foo.py"}
    hunks = {"src/foo.py": {10}}
    assert is_likely_duplicate(files, hunks, files, hunks) is True


def test_lockfile_only_overlap_is_not_a_duplicate() -> None:
    files = {"uv.lock"}
    hunks = {"uv.lock": {1}}
    assert is_likely_duplicate(files, hunks, files, hunks) is False


def test_duplicate_pair_is_tagged() -> None:
    host = FakeHost(
        [_pr(1, "aaa111"), _pr(2, "bbb222")],
        files={1: ["src/foo.py"], 2: ["src/foo.py"]},
        patches={
            1: "+++ b/src/foo.py\n@@ -10,1 +10,2 @@\n",
            2: "+++ b/src/foo.py\n@@ -10,1 +10,2 @@\n",
        },
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert found is not None
    by_n = {p["number"]: p for p in found}
    assert by_n[2]["likely_duplicate_of"] == 1
    assert "likely_duplicate_of" not in by_n[1]


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "Binary files a/assets/logo.png and b/assets/logo.png differ\n",
    ],
    ids=["patch-fetch-failed", "binary-only"],
)
def test_no_hunk_evidence_does_not_tag_duplicate(patch: str) -> None:
    host = FakeHost(
        [_pr(1, "aaa111"), _pr(2, "bbb222")],
        files={1: ["assets/logo.png"], 2: ["assets/logo.png"]},
        patches={1: patch, 2: patch},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1, 2]
    assert found is not None
    assert all("likely_duplicate_of" not in entry for entry in found)


def test_cli_help_requires_identity() -> None:
    from gptme_review.sweep import main

    with pytest.raises(SystemExit):
        main([])


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid-json"])
def test_gh_host_comment_fetch_failure_is_not_empty(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == [
            "gh",
            "api",
            "repos/o/r/issues/1/comments",
            "--paginate",
            "--slurp",
        ]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 60)
        if failure == "http":
            return subprocess.CompletedProcess(args, 1, "", "HTTP 503")
        return subprocess.CompletedProcess(args, 0, "invalid json", "")

    monkeypatch.setattr("gptme_review.sweep.subprocess.run", run)
    assert GhCliHost().pr_comments("o/r", 1) is None


@pytest.mark.parametrize("pages", [[], [[]], [[{"id": 1}], [{"id": 2}]]])
def test_gh_host_comment_pages_preserve_empty_and_populated_results(
    monkeypatch: pytest.MonkeyPatch, pages: list
) -> None:
    monkeypatch.setattr("gptme_review.sweep._gh_json", lambda *args, **kwargs: pages)
    assert GhCliHost().pr_comments("o/r", 1) == [
        item for page in pages for item in page
    ]


@pytest.mark.parametrize(
    ("failed_repos", "expected_rc"),
    [(frozenset({"o/a", "o/b"}), 1), (frozenset({"o/a"}), 0), (frozenset(), 0)],
)
def test_cli_listing_outage_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_repos: frozenset[str],
    expected_rc: int,
) -> None:
    from gptme_review.sweep import main

    monkeypatch.setattr(
        GhCliHost,
        "list_open_prs",
        lambda self, repo: None if repo in failed_repos else [],
    )
    rc = main(
        [
            "--login",
            "reviewer-bot",
            "--marker-namespace",
            "agent-ai-review",
            "--repos",
            "o/a",
            "o/b",
            "--dry-run",
        ]
    )
    assert rc == expected_rc
    captured = capsys.readouterr()
    assert ("LISTING FAILED" in captured.err) == bool(failed_repos)
    if expected_rc:
        assert "no dispatchable PRs" not in captured.out
    else:
        assert "no dispatchable PRs" in captured.out


def test_default_host_is_review_only_blocker_delegates() -> None:
    host = GhCliHost()
    assert host.is_review_only_blocker({}) is False


def test_patch_timeout_does_not_abort_candidate_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = GhCliHost()
    monkeypatch.setattr(
        host, "list_open_prs", lambda repo: [_pr(1, "aaa"), _pr(2, "bbb")]
    )
    monkeypatch.setattr(host, "pr_comments", lambda repo, number: [])
    monkeypatch.setattr(host, "fetch_pr_files", lambda repo, number: ["src/foo.py"])

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, 45)

    monkeypatch.setattr("gptme_review.sweep.subprocess.run", run)
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1, 2]
    assert found is not None
    assert all("likely_duplicate_of" not in entry for entry in found)


def test_author_allowlist_matches_github_login_case_insensitively() -> None:
    host = FakeHost(
        [_pr(1, "aaa", author="Reviewer-Bot"), _pr(2, "bbb", author="outsider")]
    )
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [1]
    assert _numbers(
        select_candidates(
            "o/r", _config(authors=("REVIEWER-BOT",)), host, NOW, dry_run=True
        )
    ) == [1]


def test_gh_host_lists_candidates_beyond_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = [_pr(n, f"head{n}", draft=True) for n in range(1, 101)]
    last = [_pr(101, "lasthead")]

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["pr", "list"]:
            # Model the existing bounded gh-list request.
            limit = int(args[args.index("--limit") + 1])
            payload: object = (first + last)[:limit]
        else:
            assert args[1:3] == ["api", "graphql"]
            assert "--paginate" in args and "--slurp" in args
            assert any("endCursor" in arg and "pageInfo" in arg for arg in args)
            payload = [
                {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}
                for nodes in (first, last)
            ]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("gptme_review.sweep.subprocess.run", run)
    host = GhCliHost()
    monkeypatch.setattr(host, "pr_comments", lambda repo, number: [])
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [
        101
    ]


@pytest.mark.parametrize(
    "pages",
    [
        None,
        [],
        [{"data": {"repository": None}}],
        [{"data": {"repository": {"pullRequests": {"nodes": None}}}}],
        [{"data": {"repository": {"pullRequests": {"nodes": [None]}}}}],
        [
            {"data": {"repository": {"pullRequests": {"nodes": [_pr(1, "aaa")]}}}},
            {"errors": [{"message": "unavailable"}]},
        ],
    ],
)
def test_gh_host_failed_or_partial_listing_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, pages: object
) -> None:
    monkeypatch.setattr("gptme_review.sweep._gh_json", lambda *args, **kwargs: pages)
    assert GhCliHost().list_open_prs("o/r") is None


def test_gh_host_successful_empty_listing_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[dict] = [{"data": {"repository": {"pullRequests": {"nodes": []}}}}]
    monkeypatch.setattr("gptme_review.sweep._gh_json", lambda *args, **kwargs: pages)
    assert GhCliHost().list_open_prs("o/r") == []


@pytest.mark.parametrize("engine", ["llm", "agent", "subscription", "static"])
@pytest.mark.parametrize("upgrade_repo", [False, True])
def test_clean_same_head_review_only_upgrades_when_policy_allows(
    engine: str, upgrade_repo: bool
) -> None:
    config = SweepConfig(
        identity=IDENTITY,
        authors=None,
        consensus_upgrade_repos=frozenset({"o/r"}) if upgrade_repo else frozenset(),
    )
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123", extra={"engine": engine, "score": 5, "findings": []}
                )
            ]
        },
    )
    found = select_candidates("o/r", config, host, NOW, dry_run=True)
    expected = [1] if upgrade_repo and engine != "static" else []
    assert _numbers(found) == expected


def test_non_blocking_findings_still_allow_same_head_upgrade() -> None:
    config = SweepConfig(
        identity=IDENTITY, authors=None, consensus_upgrade_repos=frozenset()
    )
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123",
                    extra={
                        "engine": "llm",
                        "score": 4,
                        "findings": [{"fp": "x", "severity": "P2"}],
                    },
                )
            ]
        },
    )
    found = select_candidates("o/r", config, host, NOW, dry_run=True)
    assert _numbers(found) == [1]
    assert found is not None and found[0]["same_head_upgrade"] is True


def test_duplicate_overlap_beyond_first_file_page_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = GhCliHost()
    monkeypatch.setattr(
        host, "list_open_prs", lambda repo: [_pr(1, "aaa"), _pr(2, "bbb")]
    )
    monkeypatch.setattr(host, "pr_comments", lambda repo, number: [])
    monkeypatch.setattr(
        host,
        "fetch_pr_patch",
        lambda repo, number: "+++ b/src/shared.py\n@@ -10,1 +10,2 @@\n",
    )

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        number = int(args[2].split("/")[4])
        first = [{"filename": f"src/pr{number}_{i}.py"} for i in range(100)]
        last = [{"filename": "src/shared.py"}]
        payload = [first, last] if "--paginate" in args and "--slurp" in args else first
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("gptme_review.sweep.subprocess.run", run)
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1, 2]
    assert found is not None and found[1].get("likely_duplicate_of") == 1


@pytest.mark.parametrize(
    "flags", [["--dry-run", "--live-reactions"], ["--live-reactions", "--dry-run"]]
)
def test_cli_rejects_conflicting_reaction_modes_before_github_access(
    monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    from gptme_review.sweep import main

    calls: list[str] = []

    def list_prs(self: GhCliHost, repo: str) -> list[dict]:
        calls.append(repo)
        return []

    monkeypatch.setattr(GhCliHost, "list_open_prs", list_prs)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--login",
                "reviewer-bot",
                "--marker-namespace",
                "agent-ai-review",
                "--repos",
                "o/r",
                *flags,
            ]
        )
    assert exc.value.code == 2
    assert calls == []


@pytest.mark.parametrize(
    ("flags", "expected_dry_run"),
    [([], True), (["--dry-run"], True), (["--live-reactions"], False)],
)
def test_cli_reaction_mode_is_explicit(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected_dry_run: bool
) -> None:
    from gptme_review.sweep import main

    seen: list[bool] = []

    def select(*args: object, dry_run: bool) -> list[dict]:
        seen.append(dry_run)
        return []

    monkeypatch.setattr("gptme_review.sweep.select_candidates", select)
    assert (
        main(
            [
                "--login",
                "reviewer-bot",
                "--marker-namespace",
                "agent-ai-review",
                "--repos",
                "o/r",
                *flags,
            ]
        )
        == 0
    )
    assert seen == [expected_dry_run]


@pytest.mark.parametrize("upgrade_repo", [False, True])
def test_static_findings_do_not_authorize_same_head_upgrade(upgrade_repo: bool) -> None:
    config = SweepConfig(
        identity=IDENTITY,
        authors=None,
        consensus_upgrade_repos=frozenset({"o/r"}) if upgrade_repo else frozenset(),
    )
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123",
                    extra={
                        "engine": "static",
                        "score": 4,
                        "findings": [{"fp": "x", "severity": "P2"}],
                    },
                )
            ]
        },
    )
    assert select_candidates("o/r", config, host, NOW, dry_run=True) == []


@pytest.mark.parametrize("reverse_input", [False, True])
def test_newest_updated_marker_is_authoritative(reverse_input: bool) -> None:
    old = _marker_comment(
        "old123", extra={"score": 3, "findings": [{"fp": "x", "severity": "P1"}]}
    )
    new = _marker_comment(
        "abc123",
        comment_id=2,
        extra={
            "score": 5,
            "findings": [],
            "engine": "llm",
            "consensus": {"requested": 2, "answered": 2},
        },
    )
    new["updated_at"] = "2026-08-07T21:30:00+00:00"
    comments = [new, old] if reverse_input else [old, new]
    assert marker_state(comments, IDENTITY)[0] == "abc123"
    assert marker_payload(comments, IDENTITY)[0]["sha"] == "abc123"
    host = FakeHost([_pr(1, "abc123def456")], {1: comments})
    assert select_candidates("o/r", _config(), host, NOW, dry_run=True) == []


@pytest.mark.parametrize("prefix", ["agent-ai-review", "agent-ai-review-publishing"])
def test_nested_marker_json_is_not_truncated(prefix: str) -> None:
    comment = _marker_comment(
        "abc123",
        prefix=prefix,
        extra={
            "findings": [{"fp": "x", "severity": "P2"}],
            "consensus": {"requested": 2, "answered": 2},
        },
    )
    assert marker_state([comment], IDENTITY)[0] == "abc123"
    if prefix == "agent-ai-review":
        assert marker_payload([comment], IDENTITY)[0]["consensus"]["answered"] == 2


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "NONE"])
def test_quoted_marker_does_not_hide_trusted_review_request(association: str) -> None:
    request = _marker_comment("abc123", comment_id=9)
    request.update(
        body=request["body"] + "\n@reviewer-bot review",
        user={"login": "maintainer"},
        author_association=association,
        created_at="2026-08-07T21:30:00+00:00",
    )
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {1: [_marker_comment("abc123"), request]},
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == ([] if association == "NONE" else [1])
    # The reviewer's own published summary is state, not a new request.
    request["user"] = {"login": "Reviewer-Bot"}
    assert pending_review_request([request], None, IDENTITY) is None


def test_reaction_timeout_does_not_abort_live_candidate_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = GhCliHost()
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "maintainer"},
        "author_association": "MEMBER",
    }
    monkeypatch.setattr(
        host, "list_open_prs", lambda repo: [_pr(1, "new"), _pr(2, "bbb")]
    )
    monkeypatch.setattr(
        host,
        "pr_comments",
        lambda repo, number: [_marker_comment("old", extra={"score": 3}), request]
        if number == 1
        else [],
    )
    monkeypatch.setattr(host, "fetch_pr_files", lambda repo, number: [])
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 45)

    monkeypatch.setattr("gptme_review.sweep.subprocess.run", run)
    found = select_candidates("o/r", _config(), host, NOW, dry_run=False)
    assert _numbers(found) == [1, 2]
    assert len(calls) == 2  # Both fix-iteration and explicit-request reactions failed.
    assert host.add_reaction("o/r", 9, "eyes") is False


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
        "yarn.lock",
        "Gemfile.lock",
        "composer.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
    ],
)
def test_common_lockfile_overlap_is_not_duplicate_evidence(path: str) -> None:
    for name in (path, "web/" + path):
        assert is_benign_file(name)
        assert not is_likely_duplicate({name}, {name: {10}}, {name}, {name: {10}})


@pytest.mark.parametrize(
    "path", ["package.json", "pnpm.yaml", "cargo.json", "src/lock.py"]
)
def test_package_manifests_remain_non_benign(path: str) -> None:
    assert not is_benign_file(path)


@pytest.mark.parametrize("naive_field", ["updatedAt", "created_at", "served_through"])
def test_naive_host_timestamps_use_utc_without_aborting_selection(
    naive_field: str,
) -> None:
    pr = _pr(1, "abc123def456")
    marker = _marker_comment(
        "abc123", extra={"served_through": "2026-08-07T21:00:00+00:00"}
    )
    request = {
        "id": 9,
        "body": "@reviewer-bot review",
        "created_at": "2026-08-07T21:30:00+00:00",
        "user": {"login": "maintainer"},
        "author_association": "MEMBER",
    }
    if naive_field == "updatedAt":
        pr["updatedAt"] = "2026-08-07T21:00:00"
        comments: list[dict] = []
    else:
        if naive_field == "created_at":
            request["created_at"] = "2026-08-07T21:30:00"
        else:
            marker = _marker_comment(
                "abc123", extra={"served_through": "2026-08-07T21:00:00"}
            )
        comments = [marker, request]
    host = FakeHost([pr], {1: comments})
    assert _numbers(select_candidates("o/r", _config(), host, NOW, dry_run=True)) == [1]


@pytest.mark.parametrize("review_only", [False, True])
def test_upgrade_repo_p2_review_waits_until_review_is_last_blocker(
    review_only: bool,
) -> None:
    host = FakeHost(
        [_pr(1, "abc123def456")],
        {
            1: [
                _marker_comment(
                    "abc123",
                    extra={
                        "engine": "llm",
                        "score": 4,
                        "findings": [{"fp": "x", "severity": "P2"}],
                    },
                )
            ]
        },
        review_only=review_only,
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == ([1] if review_only else [])
    if review_only:
        assert found is not None and found[0]["consensus_upgrade"] is True


def test_deleted_file_hunks_do_not_create_false_duplicate_overlap() -> None:
    patches = {
        number: (
            "diff --git a/src/shared.py b/src/shared.py\n"
            "--- a/src/shared.py\n+++ b/src/shared.py\n"
            f"@@ -{start},1 +{start},1 @@\n-old\n+new\n"
            f"diff --git a/src/deleted{number}.py b/src/deleted{number}.py\n"
            f"deleted file mode 100644\n--- a/src/deleted{number}.py\n"
            "+++ /dev/null\n@@ -1,5 +0,0 @@\n-deleted\n"
        )
        for number, start in ((1, 10), (2, 30))
    }
    assert hunks_from_patch(patches[1]) == {"src/shared.py": {10}}
    assert hunks_from_patch(patches[2]) == {"src/shared.py": {30}}
    host = FakeHost(
        [_pr(1, "aaa"), _pr(2, "bbb")],
        files={
            1: ["src/shared.py", "src/deleted1.py"],
            2: ["src/shared.py", "src/deleted2.py"],
        },
        patches=patches,
    )
    found = select_candidates("o/r", _config(), host, NOW, dry_run=True)
    assert _numbers(found) == [1, 2]
    assert found is not None
    assert all("likely_duplicate_of" not in entry for entry in found)


@pytest.mark.parametrize("reverse_input", [False, True])
@pytest.mark.parametrize(
    "old_updated", ["2026-08-07T22:00:00+02:00", "2026-08-07T20:00:00"]
)
def test_marker_authority_compares_timestamp_instants(
    reverse_input: bool, old_updated: str
) -> None:
    old = _marker_comment("abc123", extra={"engine": "llm", "score": 5, "findings": []})
    old["updated_at"] = old_updated
    new = _marker_comment(
        "abc123",
        comment_id=2,
        extra={
            "engine": "llm",
            "score": 5,
            "findings": [],
            "consensus": {"requested": 2, "answered": 2},
            "served_through": "2026-08-07T21:30:00Z",
        },
    )
    new["updated_at"] = "2026-08-07T21:30:00Z"
    comments = [new, old] if reverse_input else [old, new]
    assert marker_payload(comments, IDENTITY)[1] == new
    assert marker_state(comments, IDENTITY) == ("abc123", "2026-08-07T21:30:00Z")
    host = FakeHost([_pr(1, "abc123def456")], {1: comments})
    assert select_candidates("o/r", _config(), host, NOW, dry_run=True) == []
