"""Tests for check_greptile_scores behavior when Greptile is dark.

When Greptile has not reviewed a PR (billing outage, initial indexing, etc.),
the Greptile score is absent. `check_greptile_scores` must consult our own
AI reviewer and emit greptile_needs_fix when it finds open findings (dirty verdict),
rather than skipping the PR silently. Using greptile_needs_fix (not improvement)
matches the non-dark path, which always routes a dirty AI verdict to route_score=3
(< 4 → fix).

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
import re
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

# Bare repo endpoint (repos/OWNER/REPO) — return push=true for bot_can_merge.
# This allows check_merge_ready to proceed to emit_item rather than posting a
# maintainer-waiting comment and continuing, which would mask whether the
# Greptile score gate is actually blocking the emit.
if re.match(r'^repos/[^/]+/[^/]+$', endpoint):
    data = json.dumps({"permissions": {"push": True, "maintain": True, "admin": True}})
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


def _bob_ai_review_comment(
    sha: str, score: int = 4, *, n_history: int | None = None
) -> dict:
    """A comment carrying Bob's ai-review marker (no Greptile signature).

    ``n_history`` is the length of the marker's ``history`` array (includes the
    current round, matching the live reviewer). Default ``None`` keeps the
    original compact marker so existing tests stay byte-identical.
    """
    if n_history:
        history = [
            {"sha": f"{sha[:6]}{i:02d}", "score": score, "findings": 1}
            for i in range(n_history)
        ]
        marker = json.dumps(
            {"sha": sha, "score": score, "history": history},
            separators=(",", ":"),
        )
        body = f"<!-- bob-ai-review {marker} -->"
    else:
        body = f'<!-- bob-ai-review {{"sha": "{sha}", "score": {score}}} -->'
    return {
        "id": 111,
        "user": {"login": BOT},
        "body": body,
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
        if obj.get("type") in (
            "greptile_needs_improvement",
            "greptile_needs_fix",
            "reviewer_needs_improvement",
            "reviewer_needs_fix",
        ):
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
    """State writes in the no-score branch must use an empty first field and ${dark_score} in field 5.

    P3 fix: dark_score must NOT go in field 1. Putting a non-empty score in
    field 1 causes the score-cache condition ([ -n "$last_score" ]) to trigger
    on the next cycle, so the dark branch is skipped and ai_review_verdict is
    never called fresh — a stale dirty verdict persists for up to fetch_cache_ttl.
    Fix: field 1 = empty (cache always misses for dark PRs), field 5 = dark_score
    (so check_merge_ready can still block sub-5 merges during outages).
    """
    body = _extract_function("check_greptile_scores")
    writes = re.findall(r'^\s*echo "([^"]*)" > "\$state_file"$', body, re.MULTILINE)
    # The no-score branch writes start with ":${fetched_at}" (empty first field).
    dark_writes = [w for w in writes if w.startswith(":${fetched_at}")]
    assert dark_writes, (
        "expected at least one state write in the no-score branch starting with "
        "':${fetched_at}' (P3 fix: empty first field prevents score-cache hit; "
        "dark_score goes in field 5); found writes: " + str(writes)
    )
    for w in dark_writes:
        assert (
            "${ai_verdict}" in w
        ), f"no-score branch write missing the verdict field: {w!r}"
        assert (
            "${dark_score}" in w
        ), f"no-score branch write missing dark_score (expected in field 5): {w!r}"


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


def test_no_greptile_score_dirty_ai_review_emits_needs_fix() -> None:
    """Greptile dark + dirty AI verdict → greptile_needs_fix emitted.

    The fix: when there is no Greptile comment (Greptile dark), fall through
    to our own AI reviewer and emit greptile_needs_fix if it finds open findings.
    Always fix, not improvement — matching the non-dark path which sets route_score=3
    (< 4 → fix) whenever the AI verdict is dirty.
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
        assert items[0]["type"] in ("greptile_needs_fix", "reviewer_needs_fix"), (
            f"dirty AI on Greptile-dark PR must emit needs_fix (not improvement); "
            f"got: {items[0]}"
        )
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
    """Dark branch preserves last_score in field 5 when API returns empty and SHA matches (P1/P3 fix).

    Scenario: previous cycle wrote a real Greptile score (4) but the TTL
    expired and the API now returns empty (transient failure). The state file
    must keep '4' in field 5 (NOT field 1) so:
    - check_merge_ready does not treat this PR as having no Greptile review
    - the score-cache cannot trigger on the next cycle (field 1 stays empty)
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

        # P3: field 1 must be empty (not "4") — empty field 1 ensures the score-cache
        # never triggers on subsequent dark cycles, keeping ai_review_verdict fresh.
        # P1: the real score "4" must still appear in field 5 for check_merge_ready.
        written = state_file.read_text().strip()
        fields = written.split(":")
        assert fields[0] == "", (
            f"P3 fix: state file first field must be empty in dark state "
            f"(score cache must not hit on next cycle); "
            f"got: {written!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert len(fields) >= 5 and fields[4] == "4", (
            f"P1/P3 fix: state file field 5 must preserve previous Greptile "
            f"score '4' when API returns empty and SHA is unchanged; "
            f"got: {written!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_dark_branch_preserved_score_does_not_cache_hit() -> None:
    """Dark state with non-empty field 5 must NOT cause a score-cache hit next cycle.

    Regression guard for P1 finding fp 20761efa5189: when the dark branch writes
    dark_score in field 1 (old format), the next cycle's score-cache condition
    ([ -n "$last_score" ] && SHA matches && timestamp fresh) fires → dark branch
    is skipped → ai_review_verdict is never called → stale dirty verdict persists.

    With the P3 fix: field 1 is always empty in dark states. The score-cache
    condition cannot fire ([ -n "" ] is false) → dark branch always re-runs →
    ai_review_verdict is called fresh on every cycle.

    Scenario:
    - State file pre-seeded as P3 dark format: ":ts:sha:dirty:4" (recent ts, within TTL)
    - Greptile still dark; AI review findings are now RESOLVED (score=5 → "clean")
    - Expected: no greptile item emitted (ai_review_verdict called fresh, returns clean)
    - If the bug were present: score-cache hits on dark_score=4, non-dark path emits
      greptile_needs_improvement (not greptile_needs_fix) from stale dirty verdict.
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        # No Greptile score comment (Greptile still dark).
        # AI review findings resolved — score=5 → ai_review_verdict returns "clean".
        "comments": [_bob_ai_review_comment(short_sha, score=5)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed with P3 dark state format: empty field 1, preserved score=4 in
        # field 5, recent timestamp (well within fetch_cache_ttl=3600s).
        state_file = _state_file(state_dir)
        recent_ts = int(time.time()) - 60  # 60 s ago, within the 3600-s TTL
        state_file.write_text(f":{recent_ts}:{TEST_HEAD_SHA}:dirty:4")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        # Fresh ai_review_verdict returns "clean" → no item emitted.
        # If score-cache had triggered, we would have taken the non-dark path
        # with score=4 and served the stale "dirty" verdict → greptile_needs_improvement.
        items = _greptile_items(result)
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert dark_items == [], (
            f"P3 fix: dark branch must NOT emit when ai_review_verdict returns 'clean' "
            f"(score-cache must not hit on dark state, fresh verdict must be used); "
            f"got: {dark_items}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Verify no stale-verdict item from check_greptile_scores's score-cache path.
        # Items from that path have "Greptile score:" in their detail string.
        # Note: check_own_pr_review_state now correctly emits an item for the
        # preserved score=4 read from field 5 — those have "own-PR review:" in
        # detail and are NOT a cache-hit regression.
        cache_path_items = [
            i
            for i in items
            if i.get("type") in ("greptile_needs_improvement", "greptile_needs_fix")
            and "Greptile score:" in (i.get("detail") or "")
        ]
        assert cache_path_items == [], (
            f"P3 fix: score-cache must not produce items for dark states "
            f"(detail would contain 'Greptile score:'); "
            f"got: {cache_path_items}\nstdout: {result.stdout}\nstderr: {result.stderr}"
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


def test_dark_branch_sub4_dark_score_emits_needs_fix() -> None:
    """Dark branch with preserved dark_score < 4 must emit greptile_needs_fix, not improvement.

    Regression guard for the P1 finding on 872a1d1a: when Greptile is dark but a
    previous cycle recorded a real score < 4 (e.g. 3), the dark branch should
    route to greptile_needs_fix, not greptile_needs_improvement, to match the
    severity of the cached finding.

    Scenario:
    - Previous cycle: Greptile posted score 3/5 → state written as "3:ts:sha:dirty"
    - Current cycle: Greptile score fetch returns empty (API hiccup or dark)
    - Expected: greptile_needs_fix emitted (not greptile_needs_improvement)
    """
    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        # AI review is dirty (score=4, which maps to "dirty"); no Greptile comment.
        "comments": [_bob_ai_review_comment(short_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed with a real sub-4 Greptile score (3) for the same SHA, but
        # old timestamp so the cache TTL is expired and we re-fetch (getting
        # empty → dark branch).  dark_score should be preserved as "3".
        import time

        state_file = _state_file(state_dir)
        old_ts = int(time.time()) - 7200  # 2 hours ago, past the 3600-s TTL
        state_file.write_text(f"3:{old_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        # The dark branch emits one item with "Greptile dark" in the detail.
        # check_own_pr_review_state may also emit a second item (it reads the
        # preserved dark_score=3 from the state file) — that's expected and correct.
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert len(dark_items) == 1, (
            f"expected one dark-branch greptile item; got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert dark_items[0]["type"] in ("greptile_needs_fix", "reviewer_needs_fix"), (
            f"dark_score=3 (<4) must route to needs_fix, "
            f"not needs_improvement; got: {dark_items[0]}"
        )
        # Verify no item wrongly uses the improvement lane for a sub-4 score.
        improvement_items = [
            i
            for i in items
            if i.get("type")
            in ("greptile_needs_improvement", "reviewer_needs_improvement")
        ]
        assert improvement_items == [], (
            f"no item should use needs_improvement when dark_score=3 (<4); "
            f"got: {improvement_items}"
        )


def test_dark_branch_high_dark_score_dirty_ai_emits_needs_fix() -> None:
    """Dark branch with preserved dark_score >= 4 and dirty AI must emit greptile_needs_fix.

    Regression guard for the P1 finding on 615671b2: when Greptile is dark and the
    preserved cached score is high (e.g. 5/5), a dirty AI verdict must still route to
    greptile_needs_fix — matching the non-dark path which always sets route_score=3
    (< 4 → fix) whenever the AI verdict is dirty. The old code used
    dark_item_type="greptile_needs_improvement" as the default and only overrode to
    greptile_needs_fix when dark_score < 4, causing the wrong item type for
    high-score PRs with unresolved AI findings.

    Scenario:
    - Previous cycle: Greptile posted 5/5 → state written as "5:ts:sha:dirty"
    - Current cycle: Greptile dark (score fetch returns empty), AI verdict = dirty
    - Expected: greptile_needs_fix emitted (not greptile_needs_improvement)
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        # AI review is dirty (score=4, which ai_review_verdict maps to "dirty");
        # no Greptile score comment present.
        "comments": [_bob_ai_review_comment(short_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed with a high Greptile score (5) for the same SHA but old
        # timestamp so the cache TTL is expired → re-fetch returns empty → dark branch.
        state_file = _state_file(state_dir)
        old_ts = int(time.time()) - 7200  # 2 hours ago, past the 3600-s TTL
        state_file.write_text(f"5:{old_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert len(dark_items) == 1, (
            f"expected one dark-branch greptile item; got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert dark_items[0]["type"] in ("greptile_needs_fix", "reviewer_needs_fix"), (
            f"dirty AI with dark_score=5 (>=4) must still route to needs_fix "
            f"(not needs_improvement); got: {dark_items[0]}"
        )
        improvement_items = [
            i
            for i in items
            if i.get("type")
            in ("greptile_needs_improvement", "reviewer_needs_improvement")
        ]
        assert (
            improvement_items == []
        ), f"no improvement item expected when AI verdict is dirty; got: {improvement_items}"


def test_round_capped_p2_score_4_is_clean() -> None:
    """Score 4 with history length > 5 (round cap already applied) is clean.

    gptme/gptme#3646: Greptile 5/5, our reviewer 4/5 with one round-capped P2,
    all inline threads resolved, closed-loop already posted. ``ai_review_verdict``
    returned dirty because score < 5, so the gate re-emitted reviewer_needs_fix
    every cooldown hour — 23 PM sessions on the same head.

    History length includes the current round; > 5 matches apply_round_cap
    (review_round_count = len(history)+1 > 5 once the capped review is posted).
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(short_sha, score=4, n_history=6)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        state_file = _state_file(state_dir)
        old_ts = int(time.time()) - 7200
        # Prior cycle saw this as dirty; the new verdict must flip to clean
        # and not emit.
        state_file.write_text(f":{old_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        assert items == [], (
            f"round-capped P2-only (score 4, history=6) must not emit; got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        fields = state_file.read_text().strip().split(":")
        assert fields[3] == "clean", (
            f"verdict field must be clean after round-capped score 4; "
            f"got: {state_file.read_text()!r}"
        )


def test_round_capped_p1_score_3_stays_dirty() -> None:
    """P0/P1 (score <= 3) still dispatch after the round cap.

    The cap only downgrades P2+. A P1 that survives 6 reviews is still work.
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(short_sha, score=3, n_history=6)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        state_file = _state_file(state_dir)
        old_ts = int(time.time()) - 7200
        state_file.write_text(f":{old_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        assert any(
            i.get("type") in ("greptile_needs_fix", "reviewer_needs_fix") for i in items
        ), (
            f"score 3 (P1) after 6 rounds must still emit needs_fix; got {items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_dark_branch_pending_ai_verdict_does_not_emit() -> None:
    """Dark branch with 'pending' AI verdict (stale review SHA) must NOT emit.

    Regression guard for P2 `06b3d169b8f6`: if a regression caused 'pending'
    to be treated as 'dirty', the gate would emit greptile_needs_fix for PRs
    whose AI review is for an older push — flooding the dispatcher.

    'pending' is returned by ai_review_verdict when the reviewed SHA is not a
    prefix of the current head SHA (i.e., the review is stale after a new push).
    """
    # Use a SHA that is NOT a prefix of TEST_HEAD_SHA so ai_review_verdict
    # falls into the `*) echo "pending"` branch.
    stale_sha = "00000000"
    assert not TEST_HEAD_SHA.startswith(stale_sha)
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(stale_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert dark_items == [], (
            f"dark branch must NOT emit for 'pending' AI verdict (stale review SHA); "
            f"got: {dark_items}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_dark_branch_none_ai_verdict_does_not_emit() -> None:
    """Dark branch with 'none' AI verdict (no review marker) must NOT emit.

    Regression guard for P2 `06b3d169b8f6`: if a regression caused 'none'
    to be treated as 'dirty', the gate would emit greptile_needs_fix for PRs
    that have never been reviewed by our AI reviewer.

    'none' is returned by ai_review_verdict when no bob-ai-review marker
    comment exists on the PR.
    """
    fixture = {
        "prs": [_pr()],
        # No bob-ai-review marker comment → ai_review_verdict returns "none".
        "comments": [],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert dark_items == [], (
            f"dark branch must NOT emit for 'none' AI verdict (no review marker); "
            f"got: {dark_items}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_dark_branch_cooldown_dirty_not_emitted() -> None:
    """Dark branch must respect cooldown: dirty AI on same head within cooldown → no emit.

    Regression guard for the P2 finding on 615671b2: the cooldown check at
    lines 1109-1113 was untested. A regression removing or miscomputing the
    cooldown would cause the gate to re-emit greptile_needs_fix every 2-minute
    sweep cycle, flooding the dispatcher.

    Scenario:
    - Previous cycle: dirty AI verdict, state written with current timestamp and same SHA
    - Current cycle: same head SHA, same dirty verdict, but well within cooldown_seconds
    - Expected: no greptile item emitted (cooldown still active)
    """
    import time

    short_sha = TEST_HEAD_SHA[:10]
    fixture = {
        "prs": [_pr()],
        "comments": [_bob_ai_review_comment(short_sha, score=4)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed state with a very recent timestamp (30 s ago) for the same
        # SHA and the same dirty verdict — well within the default cooldown window.
        state_file = _state_file(state_dir)
        recent_ts = int(time.time()) - 30  # 30 s ago, well within default cooldown
        # dark_score empty: Greptile was already dark on the previous cycle too.
        state_file.write_text(f":{recent_ts}:{TEST_HEAD_SHA}:dirty")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        items = _greptile_items(result)
        dark_items = [i for i in items if "Greptile dark" in (i.get("detail") or "")]
        assert dark_items == [], (
            f"dark branch must NOT emit while within cooldown (same sha, same dirty verdict, "
            f"recent timestamp); got: {dark_items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_ai_review_verdict_call_has_error_handling() -> None:
    """Dark branch must tolerate ai_review_verdict failure (|| ai_verdict=... guard).

    Regression guard for P1 fp `7b8c5490114c`: the dark branch calls
    ai_review_verdict, which runs a gh api command. Under set -euo pipefail
    a non-zero exit (rate limit, network) propagates and aborts the entire gate,
    so the dark branch needs error handling.

    Static check: the call site must have a || fallback immediately after
    ai_review_verdict so API failures are treated as 'none' (no emit) rather
    than crashing the gate.
    """
    src = GATE.read_text()
    func_start = src.index("check_greptile_scores() {")
    # Find the dark-branch block (no-score branch)
    dark_branch_start = src.index(
        'if [ -z "$greptile_score" ] || [ "$greptile_score" = "null" ]; then',
        func_start,
    )
    dark_branch_end = src.index("\n        fi\n", dark_branch_start)
    dark_body = src[dark_branch_start:dark_branch_end]

    # The call must be followed by a || fallback on the same or next line.
    assert (
        "ai_review_verdict" in dark_body
    ), "ai_review_verdict not found in dark branch of check_greptile_scores"
    # Require either "|| ai_verdict=" or "|| true" immediately after the call.
    assert "|| ai_verdict=" in dark_body or "|| true" in dark_body, (
        "Dark branch must have error handling (|| ai_verdict=... or || true) after "
        "ai_review_verdict call so a failing gh api call does not abort the gate under "
        "set -euo pipefail. Found dark branch body:\n" + dark_body
    )


def test_check_merge_ready_field5_fallback_before_emit() -> None:
    """check_merge_ready must read preserved score from field 5 before emitting merge_ready.

    Regression guard for P2 fp `fe919616dcae`: the dark-state format leaves
    field 1 empty and stores the preserved Greptile score in field 5. If
    check_merge_ready did not have the fallback to read field 5, a dark-state
    PR with a preserved sub-5 score and a clean AI verdict would emit merge_ready
    despite having a failing Greptile score, silently bypassing the merge floor.

    Static check: the fallback (`cut -d: -f5`) must appear in check_merge_ready
    before the `emit_item "merge_ready"` call.
    """
    src = GATE.read_text()
    func_start = src.index("check_merge_ready() {")
    func_end = src.index("\n}\n", func_start)
    func_body = src[func_start:func_end]

    assert "cut -d: -f5" in func_body, (
        "check_merge_ready must read preserved score from field 5 of the dark-state "
        "file (dark-state format: ':fetched_at:sha:verdict:preserved_score'). "
        "The fallback 'cut -d: -f5' was not found in the function body."
    )
    assert (
        'emit_item "merge_ready"' in func_body
    ), "emit_item merge_ready not found in check_merge_ready (test precondition)"

    fallback_pos = func_body.index("cut -d: -f5")
    emit_pos = func_body.index('emit_item "merge_ready"')
    assert fallback_pos < emit_pos, (
        f"field-5 fallback (pos {fallback_pos}) must appear before emit_item merge_ready "
        f"(pos {emit_pos}) in check_merge_ready — the score check fires before the emit"
    )


def test_check_merge_ready_dark_state_preserved_score_blocks_merge_ready() -> None:
    """Dark-state preserved sub-5 score in field 5 must block merge_ready emission.

    Regression guard for P2 fp `a7e77115fd15`: the static test
    test_check_merge_ready_field5_fallback_before_emit checks code ordering but
    cannot catch a regression that removes the field-5 read while preserving ordering
    (e.g. reading field 1 unconditionally). Without the fallback, a dark-state file
    ':ts:sha:clean:3' would leave greptile_score="" (field 1 empty, field 5 unread),
    the merge floor check would pass ("" is not < 5), and a PR that genuinely scored
    3/5 before the Greptile outage would emit merge_ready.

    Scenario:
    - PR is CLEAN/MERGEABLE (CI passed, no conflicts)
    - Greptile state file is dark-state format: ':ts:sha:clean:3' (preserved score=3)
    - FAKE_GH returns push=true for the repo permissions endpoint so bot_can_merge
      returns true — making the test sensitive to the greptile score gate (if the
      gate were absent, the emit would proceed and the assertion would fail)
    - Expected: no merge_ready item emitted (preserved score 3 < 5 blocks it)
    """
    import time

    pr_clean = dict(_pr(), mergeStateStatus="CLEAN")
    fixture = {
        "prs": [pr_clean],
        "comments": [],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Seed dark-state greptile file with preserved score 3 in field 5.
        repo_safe = TEST_REPO.replace("/", "-")
        greptile_state_file = state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"
        recent_ts = int(time.time()) - 60  # within TTL — won't be re-fetched
        greptile_state_file.write_text(f":{recent_ts}:{TEST_HEAD_SHA}:clean:3")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        merge_ready_items = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "merge_ready":
                merge_ready_items.append(obj)

        assert merge_ready_items == [], (
            f"dark-state preserved score 3 must block merge_ready; "
            f"got: {merge_ready_items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_check_own_pr_review_state_field5_fallback_before_emit() -> None:
    """check_own_pr_review_state must read preserved score from field 5 before emitting.

    Regression guard for P1 fp `d9bed4563cc8`: the dark-state format leaves
    field 1 empty and stores the preserved Greptile score in field 5. If
    check_own_pr_review_state does not have the fallback to read field 5, a dark-state
    PR with a preserved sub-5 score is silently skipped — the empty field 1 hits the
    'no Greptile review on file yet' guard and the PR never gets dispatched for a fix.

    Static check: the fallback (`cut -d: -f5`) must appear in check_own_pr_review_state
    before the `emit_item` call.
    """
    src = GATE.read_text()
    func_start = src.index("check_own_pr_review_state() {")
    func_end = src.index("\n}\n", func_start)
    func_body = src[func_start:func_end]

    assert "cut -d: -f5" in func_body, (
        "check_own_pr_review_state must read preserved score from field 5 of the dark-state "
        "file (dark-state format: ':fetched_at:sha:verdict:preserved_score'). "
        "The fallback 'cut -d: -f5' was not found in the function body."
    )
    assert (
        "emit_item" in func_body
    ), "emit_item not found in check_own_pr_review_state (test precondition)"

    fallback_pos = func_body.index("cut -d: -f5")
    emit_pos = func_body.index("emit_item")
    assert fallback_pos < emit_pos, (
        f"field-5 fallback (pos {fallback_pos}) must appear before emit_item "
        f"(pos {emit_pos}) in check_own_pr_review_state — score must be read before emit"
    )


def test_check_own_pr_review_state_dark_state_preserved_score_emits_needs_fix() -> None:
    """Dark-state preserved sub-4 score in field 5 must trigger greptile_needs_fix emission.

    Regression guard for P1 fp `d9bed4563cc8`: when Greptile is dark and the state
    file uses the dark format (':ts:sha:clean:3'), check_own_pr_review_state was
    reading only field 1 (empty), hitting the 'no Greptile review' skip guard, and
    silently dropping the PR. The fix reads field 5 as a fallback, matching
    check_merge_ready.

    Scenario:
    - PR is BLOCKED/MERGEABLE (normal open PR state)
    - AI verdict is clean (check_greptile_scores emits nothing, only own-pr-review acts)
    - Greptile state file is dark-state: ':ts:sha:clean:3' (preserved score=3, field 5)
    - Expected: greptile_needs_fix emitted (score 3 < 4)
    """
    import time

    pr = dict(_pr(), mergeStateStatus="BLOCKED")
    fixture = {
        "prs": [pr],
        # score=5 → ai_review_verdict returns "clean" → check_greptile_scores emits
        # nothing (dark branch, clean verdict) → check_own_pr_review_state must
        # exercise its own field-5 fallback to emit greptile_needs_fix.
        # score=4 (the old value) caused ai_review_verdict to return "dirty", which
        # made check_greptile_scores emit greptile_needs_fix and then suppress
        # check_own_pr_review_state via double-dispatch guard — the fallback was
        # never reached and the test was passing for the wrong reason.
        "comments": [_bob_ai_review_comment(TEST_HEAD_SHA, score=5)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Seed dark-state greptile file with preserved score 3 in field 5.
        repo_safe = TEST_REPO.replace("/", "-")
        greptile_state_file = state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"
        recent_ts = int(time.time()) - 60
        greptile_state_file.write_text(f":{recent_ts}:{TEST_HEAD_SHA}:clean:3")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        greptile_fix_items = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") in ("greptile_needs_fix", "reviewer_needs_fix"):
                greptile_fix_items.append(obj)

        assert greptile_fix_items, (
            f"dark-state preserved score 3 must trigger greptile_needs_fix from "
            f"check_own_pr_review_state; got nothing.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_dark_state_dirty_verdict_no_double_dispatch() -> None:
    """Dark state with dirty AI verdict must NOT emit greptile_needs_improvement.

    Regression guard for P1 fp `eb4e7594dfb4`: when Greptile is dark and the
    preserved dark_score in field 5 is 4, check_greptile_scores emits
    greptile_needs_fix (AI verdict dirty), then check_own_pr_review_state runs,
    reads field 5 = 4, and was also emitting greptile_needs_improvement — a
    double-dispatch that spawned both a fix session and an improvement session.

    The fix: in check_own_pr_review_state, when field 1 is empty (dark state),
    read field 4 (AI verdict); if it is "dirty", skip — check_greptile_scores
    already handled it.

    Scenario:
    - Greptile dark (no score comment)
    - AI review comment at score 3 → "dirty" verdict
    - State file pre-seeded as dark format: ':ts:sha:dirty:4' (preserved score=4)
    - Expected: exactly ONE greptile_needs_fix item total (from check_greptile_scores)
    - Bug behaviour: also emits greptile_needs_improvement from check_own_pr_review_state
    """
    import time

    pr = dict(_pr(), mergeStateStatus="BLOCKED")
    fixture = {
        "prs": [pr],
        # Score 3 → ai_review_verdict returns "dirty"
        "comments": [_bob_ai_review_comment(TEST_HEAD_SHA, score=3)],
    }

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-seed greptile state with dirty verdict and preserved score=4 in field 5.
        repo_safe = TEST_REPO.replace("/", "-")
        greptile_state_file = state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"
        # Use old-enough timestamp so cooldown (3600s) is NOT in effect — we want
        # check_greptile_scores to emit this cycle.
        old_ts = int(time.time()) - 7200
        greptile_state_file.write_text(f":{old_ts}:{TEST_HEAD_SHA}:dirty:4")

        result = _run_gate(tmp, fixture, state_dir=state_dir)
        assert result.returncode in (0, 1), result.stderr

        all_items = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type", "").startswith(("greptile_", "reviewer_")):
                all_items.append(obj)

        improvement_items = [
            i
            for i in all_items
            if i.get("type")
            in ("greptile_needs_improvement", "reviewer_needs_improvement")
        ]
        assert improvement_items == [], (
            f"P1 fix: check_own_pr_review_state must not emit needs_improvement "
            f"when dark-state AI verdict is dirty (double-dispatch prevention); "
            f"got: {improvement_items}\nall greptile items: {all_items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        fix_items = [
            i
            for i in all_items
            if i.get("type") in ("greptile_needs_fix", "reviewer_needs_fix")
        ]
        assert fix_items, (
            f"P1 fix: needs_fix must still be emitted by check_greptile_scores "
            f"when AI verdict is dirty (dark state); "
            f"got no fix items\nall greptile items: {all_items}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
