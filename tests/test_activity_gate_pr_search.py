"""PR-data fetch prefers a raw GraphQL search over `gh pr list --author`.

`gh pr list --author` takes the search path and gh re-issues a SearchType
capability probe on every invocation (2 requests / 2 points, regardless of
projection). The raw search query is 1 request / 1 point. The fallback contract
is strict: fall back to `gh pr list` only on a hard failure or a non-search
payload — never on a valid empty result (a repo with no authored PRs is the
common case and must stay at 1 point).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

TEST_REPO = "testorg/testrepo"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
count_dir = Path(os.environ["GH_COUNT_DIR"])
search_mode = os.environ.get("TEST_SEARCH_MODE", "valid")


def bump(name: str) -> None:
    f = count_dir / name
    f.write_text(str(int(f.read_text()) + 1 if f.exists() else 1))


if not argv:
    sys.exit(2)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    bump("pr_list")
    if "--limit" in argv:
        bump("pr_list_limit")
    print(json.dumps([{
        "number": 5,
        "title": "feat: from fallback",
        "updatedAt": "2026-01-01T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": [],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "b" * 40,
        "isDraft": False,
    }]))
    sys.exit(0)

if argv[0] == "api" and any("PullRequestSearch" in a for a in argv):
    bump("search")
    if search_mode == "error":
        sys.exit(1)
    if search_mode == "nonsense":
        # Non-search payload: a fake `gh api` answering an unknown endpoint.
        print("[]")
        sys.exit(0)
    if search_mode == "empty":
        nodes = []
    elif search_mode == "broken_cursor":
        # Page 1 claims hasNextPage but provides a null endCursor — the
        # caller cannot continue pagination and must not treat the partial
        # page as success.
        node = {
            "number": 5,
            "title": "feat: from page1",
            "updatedAt": "2026-01-01T00:00:00Z",
            "comments": {"nodes": []},
            "latestReviews": {"nodes": []},
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "headRefOid": "a" * 40,
            "isDraft": False,
            "statusCheckRollup": {"contexts": {"nodes": []}},
        }
        print(json.dumps({"data": {"search": {
            "nodes": [node],
            "pageInfo": {"hasNextPage": True, "endCursor": None},
        }}}))
        sys.exit(0)
    elif search_mode == "paged":
        # Two pages: the fake keys off whether the caller sent an endCursor.
        paged = any(a.startswith("endCursor=") for a in argv)
        node = {
            "number": 7 if paged else 5,
            "title": "feat: from page2" if paged else "feat: from page1",
            "updatedAt": "2026-01-01T00:00:00Z",
            "comments": {"nodes": []},
            "latestReviews": {"nodes": []},
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "headRefOid": ("b" if paged else "a") * 40,
            "isDraft": False,
            "statusCheckRollup": {"contexts": {"nodes": []}},
        }
        print(json.dumps({"data": {"search": {
            "nodes": [node],
            "pageInfo": {"hasNextPage": not paged, "endCursor": "c1"},
        }}}))
        sys.exit(0)
    else:
        nodes = [{
            "number": 5,
            "title": "feat: from search",
            "updatedAt": "2026-01-01T00:00:00Z",
            "comments": {"nodes": [{
                "author": {"login": "ErikBjare"},
                "authorAssociation": "OWNER",
                "body": "hi",
                "createdAt": "2026-01-01T00:00:00Z",
                "url": "https://example.invalid/1",
            }]},
            "latestReviews": {"nodes": []},
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "headRefOid": "a" * 40,
            "isDraft": False,
            "statusCheckRollup": {"contexts": {"nodes": [
                {"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]}},
        }]
    print(json.dumps({"data": {"search": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}))
    sys.exit(0)

if argv[0] == "api":
    print("{}")
    sys.exit(0)

if argv[0] in ("issue", "run", "repo"):
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(tmp: Path, state_dir: Path, mode: str) -> tuple[dict[str, int], Path]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    count_dir = tmp / "counts"
    count_dir.mkdir()

    env = os.environ.copy()
    env["GH_COUNT_DIR"] = str(count_dir)
    env["TEST_SEARCH_MODE"] = mode
    env["PATH"] = f"{tmp}:{env['PATH']}"

    subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--org",
            "testorg",
            "--repo",
            TEST_REPO,
            "--state-dir",
            str(state_dir),
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    counts = {
        name: int((count_dir / name).read_text())
        for name in ("search", "pr_list", "pr_list_limit")
        if (count_dir / name).exists()
    }
    return counts, state_dir / "gh-cache" / "pr-v2-testorg-testrepo.json"


def test_valid_search_payload_uses_raw_path_and_skips_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        counts, cache_file = _run_gate(tmp, state_dir, "valid")

        assert counts.get("search") == 1, counts
        assert counts.get("pr_list", 0) == 0, "fallback must not run on a valid payload"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert cached[0]["title"] == "feat: from search"
        # gh `--json` shape: comments/latestReviews are arrays and
        # statusCheckRollup is a flat context list, not `{contexts: {nodes}}`.
        assert cached[0]["comments"][0]["author"]["login"] == "ErikBjare"
        assert cached[0]["latestReviews"] == []
        assert cached[0]["statusCheckRollup"][0]["conclusion"] == "SUCCESS"


def test_valid_empty_search_result_does_not_fall_back() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        counts, cache_file = _run_gate(tmp, state_dir, "empty")

        assert counts.get("search") == 1, counts
        assert counts.get("pr_list", 0) == 0, "a valid empty result is not a failure"
        assert cache_file.exists()
        assert json.loads(cache_file.read_text()) == []


def test_non_search_payload_falls_back_to_pr_list() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        counts, cache_file = _run_gate(tmp, state_dir, "nonsense")

        assert counts.get("pr_list", 0) >= 1, "non-search payload must fall back"
        assert cache_file.exists()
        assert json.loads(cache_file.read_text())[0]["title"] == "feat: from fallback"


def test_search_follows_pagination_and_merges_pages() -> None:
    """A repo with more than one page of authored PRs is not truncated at 30."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        counts, cache_file = _run_gate(tmp, state_dir, "paged")

        assert counts.get("search") == 2, counts
        assert counts.get("pr_list", 0) == 0, "pagination is not a failure"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert [pr["title"] for pr in cached] == [
            "feat: from page1",
            "feat: from page2",
        ]


def test_has_next_page_with_null_cursor_falls_back() -> None:
    """hasNextPage=true with a null endCursor must not truncate silently."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        counts, cache_file = _run_gate(tmp, state_dir, "broken_cursor")

        assert counts.get("search") == 1, counts
        assert counts.get("pr_list", 0) >= 1, "unpageable result must fall back"
        assert (
            counts.get("pr_list_limit", 0) >= 1
        ), "fallback must request the full limit, not gh's 30-item default"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert [pr["title"] for pr in cached] == ["feat: from fallback"]
