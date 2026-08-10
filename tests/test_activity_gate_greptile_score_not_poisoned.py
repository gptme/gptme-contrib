"""The Greptile state file must record Greptile's score, not our routing decision.

`check_greptile_scores` consults our own AI reviewer when Greptile says 5/5, and
routes PRs with open findings to the fix arm. The first implementation did that
by overwriting `greptile_score=3`, which the shared emit path then persisted to
`<repo>-pr-<n>-greptile.state`.

That poison is self-sticking. On the next cycle the cache-hit branch reads the
persisted 3 back into `greptile_score`, the `-ge 5` test fails, and the AI
verdict is never consulted again — so the PR cannot leave the fix arm even after
its findings are resolved, until the head changes or the TTL expires. Meanwhile
every consumer of that file (`check_own_pr_review_state`, `check_merge_ready`)
reports "Greptile 3/5" for a PR Greptile actually scored 5/5.

Routing therefore uses a separate `route_score`, and the verdict is persisted as
a 4th field so the paginated comments fetch obeys the same TTL as the score.
"""

from __future__ import annotations

import re
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "github" / "activity-gate.sh"


def _extract_function(name: str) -> str:
    src = GATE.read_text()
    start = src.index(f"{name}() {{")
    rest = src[start:]
    return rest[: rest.index("\n}\n") + len("\n}\n")]


def test_greptile_score_is_never_assigned_a_literal_score() -> None:
    """greptile_score may come from the API or the cache — never from a decision.

    Deliberately checks for a *literal* rather than allow-listing the legitimate
    assignments: the API fetch and the cache read are both fine and both change
    shape over time, whereas `greptile_score=<digit>` is only ever someone
    steering a branch by overwriting the reported score.
    """
    body = _extract_function("check_greptile_scores")
    assignments = re.findall(r"^\s*greptile_score=(.*)$", body, re.MULTILINE)
    assert assignments, "expected greptile_score to be assigned at all"
    literals = [a.strip() for a in assignments if re.fullmatch(r'"?\d+"?', a.strip())]
    assert not literals, (
        f"greptile_score assigned the literal {literals} — this is persisted to "
        "the state file and read back as Greptile's own score on the next cycle, "
        "so the PR can never leave the branch the literal routed it to"
    )


def test_routing_uses_route_score_not_greptile_score() -> None:
    body = _extract_function("check_greptile_scores")
    assert 'local route_score="$greptile_score"' in body
    assert '[ "$route_score" -lt 4 ]' in body, (
        "the item-type decision must read the routing score, or the AI verdict "
        "cannot influence routing at all"
    )


def test_every_greptile_state_write_carries_the_verdict() -> None:
    """A write that omits the verdict silently re-enables the per-cycle refetch.

    ai_review_verdict() costs a paginated comments fetch. It is cached by the 4th
    field, so any new write path that drops the field makes the gate hit the
    comments endpoint every cycle for every 5/5 PR — the API-budget regression
    this repo has a rate-limit history with.
    """
    body = _extract_function("check_greptile_scores")
    writes = re.findall(r'^\s*echo "([^"]*)" > "\$state_file"$', body, re.MULTILINE)
    assert writes, "expected greptile state writes"
    missing = [w for w in writes if "${ai_verdict}" not in w]
    assert not missing, f"state writes missing the verdict field: {missing}"


def test_verdict_is_read_back_and_reused_within_the_ttl() -> None:
    body = _extract_function("check_greptile_scores")
    assert 'last_verdict=$(echo "$last_state" | cut -d: -f4)' in body
    assert 'ai_verdict="$last_verdict"' in body, (
        "the cached verdict must actually be reused, or the 4th field is written "
        "but never read and the fetch happens every cycle anyway"
    )


def test_cooldown_compares_the_verdict_too() -> None:
    """Score no longer moves on a flip, so the verdict must be in the change test."""
    body = _extract_function("check_greptile_scores")
    assert '[ "$ai_verdict" = "$last_verdict" ]' in body, (
        "without this, a clean -> dirty flip on an unchanged head sits out the "
        "cooldown before PM hears about it"
    )
