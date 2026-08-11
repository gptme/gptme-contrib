"""Tests for check_greptile_scores behavior when Greptile is dark.

When Greptile has not reviewed a PR (billing outage, initial indexing, etc.),
the Greptile score is absent. `check_greptile_scores` must consult our own
AI reviewer and emit greptile_needs_improvement when it finds open findings,
rather than skipping the PR silently.

The state file records an empty Greptile-score field so later cycles that do
receive a real Greptile score are not confused by a poisoned cache.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"
GATE = SCRIPT

TEST_REPO = "testorg/testrepo"
TEST_PR = 42
TEST_HEAD_SHA = "deadbeef1234"
BOT = "TimeToBuildBob"


# Fake gh that serves comments for the issues API and applies --jq via
# the real jq binary.  All other endpoints return empty results.
FAKE_GH = r'''#!/usr/bin/env python3
"""Fake gh CLI for greptile-dark tests."""
from pathlib import Path
import json
import os
import subprocess as sp
import sys

fixture = json.loads(Path(os.environ["GH_FIXTURE"]).read_text())
argv = sys.argv[1:]

if not argv:
    sys.exit(2)

if argv[0] == "pr" and argv[1:2] == ["list"]:
    print(json.dumps(fixture["prs"]))
    sys.exit(0)

if argv[0] in ("repo", "issue", "run") and argv[1:2] == ["list"]:
    print("[]")
    sys.exit(0)

if argv[0] != "api":
    sys.exit(0)

endpoint = ""
jq_expr = ""
i = 1
while i < len(argv):
    a = argv[i]
    if a == "--jq":
        jq_expr = argv[i + 1]; i += 2; continue
    if a in ("-q", "-H", "-f", "-F", "-X"):
        i += 2; continue
    if a in ("--paginate", "--slurp"):
        i += 1; continue
    if a.startswith("-"):
        i += 1; continue
    endpoint = a; i += 1

if endpoint == "notifications":
    sys.exit(0)

# issues/{n}/comments — serve the fixture comments list and apply jq.
if "/issues/" in endpoint and endpoint.endswith("/comments"):
    data = json.dumps(fixture.get("comments", []))
    if jq_expr:
        r = sp.run(["jq", "-r", jq_expr], input=data, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
    else:
        print(data)
    sys.exit(0)

# Everything else returns empty.
if jq_expr:
    sys.exit(0)
print("[]")
sys.exit(0)
'''


def _extract_function(name: str) -> str:
    src = GATE.read_text()
    start = src.index(f"{name}() {{")
    rest = src[start:]
    return rest[: rest.index("\n}\n") + len("\n}\n")]


def _pr(head_sha: str = TEST_HEAD_SHA) -> dict:
    return {
        "number": TEST_PR,
        "title": f"Test PR #{TEST_PR}",
        "updatedAt": "2026-08-11T10:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "headRefOid": head_sha,
        "isDraft": False,
    }


def _bob_ai_review_comment(sha: str, score: int = 4) -> dict:
    """A comment carrying Bob's ai-review marker (no Greptile signature)."""
    return {
        "id": 111,
        "user": {"login": BOT},
        "body": f'<!-- bob-ai-review {{"sha": "{sha}", "score": {score}}} -->',
        "created_at": "2026-08-11T09:00:00Z",
    }


def _run_gate(
    tmp: Path, fixture: dict, *, state_dir: Path
) -> subprocess.CompletedProcess[str]:
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))

    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["GH_FIXTURE"] = str(fixture_path)
    env["PATH"] = f"{tmp}:{env['PATH']}"

    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            BOT,
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
        timeout=120,
    )


def _greptile_items(result: subprocess.CompletedProcess[str]) -> list[dict]:
    items = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") in ("greptile_needs_improvement", "greptile_needs_fix"):
            items.append(obj)
    return items


def _state_file(state_dir: Path) -> Path:
    repo_safe = TEST_REPO.replace("/", "-")
    return state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"


# ── Static analysis ──────────────────────────────────────────────────────────


def test_ai_review_verdict_called_outside_ge5_branch() -> None:
    """ai_review_verdict must be called even when greptile_score is absent.

    The regression: the call was gated behind `if [ "$greptile_score" -ge 5 ]`,
    making it unreachable whenever Greptile is dark.
    """
    body = _extract_function("check_greptile_scores")
    # Find the first occurrence of ai_review_verdict call.
    verdict_pos = body.index("ai_review_verdict ")
    # Find the start of the greptile_score -ge 5 block.
    ge5_pos = body.index('[ "$greptile_score" -ge 5 ]')
    assert verdict_pos < ge5_pos, (
        "ai_review_verdict is only called inside the -ge 5 branch — it is "
        "unreachable when Greptile is dark. The call must appear earlier, in "
        "the no-score branch."
    )


def test_no_score_state_write_uses_dark_score_field() -> None:
    """State writes in the no-score branch must use ${dark_score} as the first field.

    dark_score is empty when there is no previous real score (genuine dark
    case) and equals last_score when the same SHA had a real score before
    (transient API failure: P1 fix).  Writing a bare `:${fetched_at}` would
    always lose a previously-cached real score on transient failures.
    """
    body = _extract_function("check_greptile_scores")
    writes = re.findall(r'^\s*echo "([^"]*)" > "\$state_file"$', body, re.MULTILINE)
    # The no-score branch writes start with "${dark_score}:${fetched_at}".
    dark_writes = [w for w in writes if w.startswith("${dark_score}:${fetched_at}")]
    assert dark_writes, (
        "expected at least one state write in the no-score branch starting with "
        "'${dark_score}:${fetched_at}' (P1 fix: preserve or empty Greptile-score "
        "field); found writes: " + str(writes)
    )
    for w in dark_writes:
        assert (
            "${ai_verdict}" in w
        ), f"no-score branch write missing the verdict field: {w!r}"


def test_dark_branch_calls_ai_review_verdict_unconditionally() -> None:
    """In the dark branch, ai_review_verdict must be called unconditionally (P2 fix).

    The old cache used last_timestamp (old state epoch), so a stale dirty verdict
    could persist for up to fetch_cache_ttl even after findings are resolved.
    The fix: remove the cache check — always call ai_review_verdict.
    """
    body = _extract_function("check_greptile_scores")
    # Find the dark branch (after the empty-score check).
    dark_start = body.index('if [ -z "$greptile_score" ]')
    dark_body = body[dark_start:]
    verdict_call_pos = dark_body.index("ai_review_verdict ")
    # There must be no last_timestamp cache condition before the call.
    pre_call = dark_body[:verdict_call_pos]
    assert "last_timestamp" not in pre_call, (
        "The dark branch caches ai_review_verdict based on last_timestamp — "
        "stale dirty verdicts can persist for up to fetch_cache_ttl after "
        "findings are resolved (P2). Remove the cache check: always call "
        "ai_review_verdict unconditionally in the dark branch."
    )


# ── Subprocess (behavioral) ──────────────────────────────────────────────────


def test_no_greptile_score_dirty_ai_review_emits_needs_improvement() -> None:
    """Greptile dark + dirty AI verdict → greptile_needs_improvement emitted.

    The fix: when there is no Greptile comment (Greptile dark), fall through
    to our own AI reviewer and emit if it finds open findings.
    """
    # ai_review_verdict does prefix matching: marker sha is a prefix of head sha.
    short_sha = TEST_HEAD_SHA[:10]  # "deadbeef12"
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(short_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed state file with an old timestamp so cooldown is expired and
        # this is not treated as first-discovery (which seeds but does not emit).
        state_file = _state_file(state_dir)
        state_file.write_text(f":{0}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        assert len(items) == 1, (
            f"expected one greptile item, got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert items[0]["type"] == "greptile_needs_improvement", items[0]
        assert items[0]["number"] == TEST_PR
        assert "Greptile dark" in (
            items[0].get("detail") or ""
        ), f"detail must mention 'Greptile dark': {items[0]}"


def test_no_greptile_score_first_discovery_seeds_state_no_emit() -> None:
    """First time a PR is seen with no Greptile score — seed state, no emit.

    Mirrors the normal first-discovery behavior: report on the next cycle,
    not on the very first encounter.
    """
    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(short_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        # State file does NOT exist — this is first discovery.

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        assert (
            items == []
        ), f"first discovery must not emit; got {items}\nstdout: {result.stdout}"

        # State file must be seeded with an empty first field.
        state_file = _state_file(state_dir)
        assert state_file.exists(), "state file must be seeded on first discovery"
        fields = state_file.read_text().strip().split(":")
        assert fields[0] == "", (
            f"first field (Greptile score) must be empty when Greptile is dark; "
            f"got: {state_file.read_text()!r}"
        )
        assert fields[3] == "dirty", (
            f"fourth field (AI verdict) must be 'dirty'; "
            f"got: {state_file.read_text()!r}"
        )


def test_no_greptile_score_clean_ai_review_does_not_emit() -> None:
    """Greptile dark + clean AI verdict → no emit.

    A PR with no Greptile score and a clean AI review is not actionable.
    """
    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        # Score 5 → ai_review_verdict returns "clean".
        "comments": [_bob_ai_review_comment(short_sha, score=5)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        assert items == [], (
            f"clean AI verdict with no Greptile score must not emit; "
            f"got {items}\nstdout: {result.stdout}"
        )


def test_dark_branch_preserves_last_score_on_same_sha() -> None:
    """Dark branch preserves last_score when API returns empty and SHA matches (P1 fix).

    Scenario: previous cycle wrote a real Greptile score (4) but the TTL
    expired and the API now returns empty (transient failure).  The state
    file must keep the '4' in the first field so check_merge_ready does not
    treat this PR as having no Greptile review and emit merge_ready.
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        # AI review is clean (score=5) — no emit expected.
        # No Greptile comment → dark branch entered.
        "comments": [_bob_ai_review_comment(short_sha, score=5)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed with a real Greptile score (4) for the same SHA, but TTL expired
        # so the score cache misses and we re-fetch (getting empty → dark branch).
        state_file = _state_file(state_dir)
        old_ts = int(time.time()) - 7200  # 2 hours ago, past the 3600-s TTL
        state_file.write_text(f"4:{old_ts}:{TEST_HEAD_SHA}:clean")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        # State file must preserve the real score '4', not overwrite with empty.
        written = state_file.read_text().strip()
        fields = written.split(":")
        assert fields[0] == "4", (
            f"P1 fix: state file first field must preserve previous Greptile "
            f"score '4' when API returns empty and SHA is unchanged; "
            f"got: {written!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def _greptile_score_comment(score: int) -> dict:
    """A comment from Greptile containing a score line."""
    return {
        "id": 888,
        "user": {"login": "greptile-review[bot]"},
        "body": f"Code review complete.\n\nScore: {score}/5\n\nSome findings.",
        "created_at": "2026-08-11T13:00:00Z",
    }


def test_empty_last_score_reraises_real_greptile_score() -> None:
    """Empty last_score must NOT be used as a cache hit — always re-fetch.

    Regression guard for the P1 finding in the AI review:
    the code at line 1057 checks `[ -n "$last_score" ]`, so a state file
    written by the no-score arm (empty first field) forces a re-fetch on
    the next cycle. When Greptile has now posted a real score (e.g. 4/5),
    that score must be picked up immediately — not suppressed until the TTL
    expires.

    Scenario:
    - Previous cycle: Greptile dark → state file written as `:ts:sha:dirty`
    - Current cycle: Greptile posts 4/5, timestamp still within fetch_cache_ttl
    - Expected: score "4" is fetched and a greptile item is emitted
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        "comments": [
            # Greptile has now posted a 4/5 score
            _greptile_score_comment(score=4),
            # Bob's AI review marker is still present (but Greptile score takes precedence)
            _bob_ai_review_comment(short_sha, score=4),
        ],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Simulate the state left by a Greptile-dark cycle:
        # empty first field, recent timestamp (well within fetch_cache_ttl=3600s),
        # same head SHA. Without the `[ -n "$last_score" ]` guard this would be
        # treated as a cache hit and greptile_score would stay "".
        state_file = _state_file(state_dir)
        recent_ts = int(time.time()) - 60  # 60 s ago, within the 3600-s TTL
        state_file.write_text(f":{recent_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        # The real Greptile score (4) must be picked up and a greptile item emitted.
        # (check_own_pr_review_state may also emit an item for this PR — allow both.)
        score_items = [
            i for i in items if "Greptile score: 4/5" in (i.get("detail") or "")
        ]
        assert len(score_items) >= 1, (
            f"expected a greptile item with real score 4/5 after re-fetch; got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # State file first field must now be the real score, not empty.
        fields = state_file.read_text().strip().split(":")
        assert fields[0] == "4", (
            f"state file first field must be real Greptile score '4' after re-fetch; "
            f"got: {state_file.read_text()!r}"
        )
