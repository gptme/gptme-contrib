"""Golden tests for the run-item prompt templates (step 4, rows 6-7).

The goldens in ``tests/goldens/run_item/prompts.json`` (one JSON object,
name → exact string) are the *captured output of the real bash builders*
in ErikBjare/bob @ f95ccae920af0058c68c86083e9007be86710dc5:

- ``investigate.<type>.bob`` — ``build_item_investigate`` arms
  (``scripts/github/project-monitoring-lib.sh:670-935``), captured by
  sourcing the lib with the CONTEXT globals below and dumping
  ``$INVESTIGATE`` per type.
- ``arc_context.bob`` / ``mention_constraint.bob`` — the ``ARC_CONTEXT``
  (lib.sh:96-112) and ``_DIRECT_MENTION_CONSTRAINT``
  (project-monitoring.sh:535-542) assignments, sed-extracted from the real
  source and eval'd with the CONTEXT globals.
- ``main_prompt.*.bob`` — the inline ``PROMPT=`` assignment
  (project-monitoring.sh:545-578), sed-extracted and eval'd for three cases
  (plain pr_update; greptile-fix + arc + direct-mention; merge_ready with no
  investigate arm).

The captures are stored JSON-encoded (same convention as
``goldens/worker_records/*.json``) because several legitimately end
WITHOUT a trailing newline — plain .txt storage would be corrupted by the
end-of-file-fixer pre-commit hook.

Regenerate from a brain-repo checkout with the capture script recorded in
the step-4 PR description (source lib.sh; set item_repo/item_number/
item_detail/item_all_numbers/WORKSPACE/AUTHOR; ``build_item_investigate``;
``sed -n '545,578p' project-monitoring.sh`` + ``eval`` for the skeleton).

The Bob parameter set proves byte parity with production; the Alice set
proves the Bob-specific bits (identity name, author handle, hardcoded
/home/bob/bob, peer roster, Gordon/Erik policy paragraph) are really
parameters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gptme_runloops.merge_lifecycle import InstructionKind
from gptme_runloops.prompt_templates import (
    ItemPromptKind,
    ItemPromptParams,
    build_investigate,
    render_arc_context,
    render_instruction,
    render_item_investigate,
    render_main_prompt,
    render_mention_constraint,
    render_preheld_claim_block,
)

GOLDEN_FILE = Path(__file__).parent / "goldens" / "run_item" / "prompts.json"
_GOLDENS: dict[str, str] = json.loads(GOLDEN_FILE.read_text())


def golden(name: str) -> str:
    """The exact bash-rendered bytes captured for *name*."""
    return _GOLDENS[name]


# Bob's identity values — what the brain config (step-5 follow-up) carries.
BOB_IDENTITY = dict(
    workspace="/home/bob/bob",
    author="TimeToBuildBob",
    agent_name="Bob",
    operator_name="Erik",
    twitter_handle="TimeToBuildBob",
    forum_handle="bob",
    peer_agents="Gordon / Alice / Sven",
    agent_msg_policy_note=(
        "**Gordon IBKR equity/risk/graduation messages**: these are Erik-gated.\n"
        'Reply: "Noted. This is Erik-gated — logged for his review. '
        'No autonomous action taken."\n'
        "(memory: gordon-equity-messages-are-erik-gated-not-bob-action)"
    ),
)

ALICE_IDENTITY = dict(
    workspace="/srv/agents/alice/workspace",
    author="AliceAgent",
    agent_name="Alice",
    operator_name="Erik",
    twitter_handle="AliceBuilds",
    forum_handle="alice",
    peer_agents="Gordon / Sven",
    agent_msg_policy_note="",
)

BOB_PARAMS = ItemPromptParams(
    repo="gptme/gptme-contrib",
    number=1234,
    detail="CI failed on master; review comment",
    all_numbers=("1234",),
    **BOB_IDENTITY,
)

MONITORING_RULES = "## Rules\n\n- Rule one.\n- Rule two (multi-line rules placeholder)."

SIMPLE_ARMS = [
    "pr_update",
    "ci_failure",
    "assigned_issue",
    "twitter_mention",
    "forum_mention",
    "agent_msg_reply",
    "notification",
    "merge_conflict",
]


# --- Golden parity (Bob values ≡ bash bytes) ---


@pytest.mark.parametrize("type_name", SIMPLE_ARMS)
def test_investigate_arm_matches_bash_golden(type_name: str) -> None:
    assert render_item_investigate(ItemPromptKind(type_name), BOB_PARAMS) == golden(
        f"investigate.{type_name}.bob"
    )


def test_master_ci_failure_grouped_runs_match_bash_golden() -> None:
    params = ItemPromptParams(
        repo="gptme/gptme-contrib",
        number="16999888001",
        detail="CI failed on master; review comment",
        all_numbers=("16999888001", "16999888002"),
        **BOB_IDENTITY,
    )
    assert render_item_investigate(ItemPromptKind.MASTER_CI_FAILURE, params) == golden(
        "investigate.master_ci_failure.bob"
    )


def test_combined_types_concatenate_in_order() -> None:
    assert build_investigate(["ci_failure", "pr_update"], BOB_PARAMS) == golden(
        "investigate.combined.ci_failure+pr_update.bob"
    )


def test_greptile_types_delegate_to_step2_templates() -> None:
    assert build_investigate(["greptile_needs_fix"], BOB_PARAMS) == render_instruction(
        InstructionKind.GREPTILE_NEEDS_FIX, BOB_PARAMS.to_prompt_context()
    )


def test_unknown_types_contribute_nothing() -> None:
    assert build_investigate(["merge_ready"], BOB_PARAMS) == ""
    assert build_investigate(["some_future_type"], BOB_PARAMS) == ""


def test_arc_context_matches_bash_golden() -> None:
    rendered = render_arc_context(
        BOB_PARAMS,
        arc_id="arc-2026-07-01-gptme-contrib-1234",
        arc_hint="Wait for Greptile re-review, then merge",
        arc_sessions=3,
    )
    assert rendered == golden("arc_context.bob")


def test_mention_constraint_matches_bash_golden() -> None:
    assert render_mention_constraint(BOB_PARAMS) == golden("mention_constraint.bob")


def test_main_prompt_plain_matches_bash_golden() -> None:
    rendered = render_main_prompt(
        BOB_PARAMS,
        item_type="pr_update",
        title="fix(runloops): example PR title",
        investigate=build_investigate(["pr_update"], BOB_PARAMS),
        monitoring_rules=MONITORING_RULES,
        time_desc="~10 minutes",
    )
    assert rendered == golden("main_prompt.plain.bob")


def test_main_prompt_full_matches_bash_golden() -> None:
    """Greptile fix instructions + arc context + direct-mention constraint."""
    fix = render_instruction(
        InstructionKind.LOCAL_GREPTILE_FIX, BOB_PARAMS.to_prompt_context()
    ).rstrip("\n")
    arc = render_arc_context(
        BOB_PARAMS,
        arc_id="arc-2026-07-01-gptme-contrib-1234",
        arc_hint="Wait for Greptile re-review, then merge",
        arc_sessions=3,
    )
    rendered = render_main_prompt(
        BOB_PARAMS,
        item_type="pr_update",
        title="fix(runloops): example PR title",
        investigate=build_investigate(["pr_update"], BOB_PARAMS),
        monitoring_rules=MONITORING_RULES,
        time_desc="~35 minutes",
        greptile_fix_instructions=fix,
        arc_context=arc,
        mention_constraint=render_mention_constraint(BOB_PARAMS),
    )
    assert rendered == golden("main_prompt.full.bob")


def test_main_prompt_merge_ready_empty_investigate_matches_bash_golden() -> None:
    rendered = render_main_prompt(
        BOB_PARAMS,
        item_type="merge_ready",
        title="fix(runloops): example PR title",
        investigate=build_investigate(["merge_ready"], BOB_PARAMS),
        monitoring_rules=MONITORING_RULES,
        time_desc="~10 minutes",
    )
    assert rendered == golden("main_prompt.merge_ready_empty.bob")


# --- Agent-agnosticism (Alice values leave no Bob behind) ---


def test_no_bob_remains_with_alice_values() -> None:
    params = ItemPromptParams(
        repo="ErikBjare/alice",
        number=7,
        detail="review comment",
        all_numbers=("7",),
        **ALICE_IDENTITY,
    )
    all_types = SIMPLE_ARMS + ["master_ci_failure"]
    rendered = render_main_prompt(
        params,
        item_type="pr_update",
        title="a title",
        investigate=build_investigate(all_types, params),
        monitoring_rules="rules",
        time_desc="~10 minutes",
        arc_context=render_arc_context(
            params, arc_id="a", arc_hint="h", arc_sessions=1
        ),
        mention_constraint=render_mention_constraint(params),
    )
    for bob_marker in ("/home/bob", "Bob", "TimeToBuildBob", "Gordon IBKR", "@bob"):
        assert bob_marker not in rendered
    assert "/srv/agents/alice/workspace" in rendered
    assert "You are Alice," in rendered
    assert "@AliceBuilds" in rendered
    assert "@alice" in rendered


def test_agent_msg_policy_note_absent_when_empty() -> None:
    params = ItemPromptParams(
        repo="ErikBjare/alice", number=7, detail="d", **ALICE_IDENTITY
    )
    rendered = render_item_investigate(ItemPromptKind.AGENT_MSG_REPLY, params)
    assert "Erik-gated" not in rendered
    # Empty note leaves the arm's trailing newline structure, no paragraph.
    assert rendered.endswith("```\n\n")


def test_twitter_forum_handles_fall_back() -> None:
    params = ItemPromptParams(
        repo="o/r", number=1, workspace="/w", author="SomeAuthor", agent_name="Zed"
    )
    tw = render_item_investigate(ItemPromptKind.TWITTER_MENTION, params)
    assert "@SomeAuthor" in tw
    fm = render_item_investigate(ItemPromptKind.FORUM_MENTION, params)
    assert "@zed" in fm
    assert "--agent zed" in fm


# --- Pre-held claim block (new content, step-7/8 reservation) ---


def test_preheld_block_names_the_held_key() -> None:
    block = render_preheld_claim_block("github:gptme/gptme#42")
    assert block.startswith("\n## Coordination Claim (pre-held)")
    assert "`github:gptme/gptme#42`" in block
    assert "ALREADY HELD" in block


def test_preheld_block_appends_after_optional_blocks() -> None:
    base = render_main_prompt(
        BOB_PARAMS,
        item_type="pr_update",
        title="t",
        investigate="",
        monitoring_rules="rules",
        time_desc="~10 minutes",
    )
    with_preheld = render_main_prompt(
        BOB_PARAMS,
        item_type="pr_update",
        title="t",
        investigate="",
        monitoring_rules="rules",
        time_desc="~10 minutes",
        preheld_block=render_preheld_claim_block("github:a/b#1"),
    )
    assert base != with_preheld
    assert "## Coordination Claim (pre-held)" in with_preheld
    # Empty preheld block ⇒ byte-identical bash skeleton (the parity contract).
    assert with_preheld.replace(render_preheld_claim_block("github:a/b#1"), "") == base


def test_every_expected_golden_exists() -> None:
    expected = {f"investigate.{t}.bob" for t in SIMPLE_ARMS}
    expected |= {
        "investigate.master_ci_failure.bob",
        "investigate.combined.ci_failure+pr_update.bob",
        "arc_context.bob",
        "mention_constraint.bob",
        "main_prompt.plain.bob",
        "main_prompt.full.bob",
        "main_prompt.merge_ready_empty.bob",
    }
    present = set(_GOLDENS)
    assert expected <= present, f"missing goldens: {expected - present}"
