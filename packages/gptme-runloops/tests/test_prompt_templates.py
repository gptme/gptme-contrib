"""Golden tests for the Greptile prompt templates.

The goldens under ``tests/goldens/prompt_templates/`` are the *captured
output of the bash builders* in ErikBjare/bob
``scripts/github/project-monitoring-lib.sh`` @
ca7aa17a2899dbe676fba7074fc5c4dd61d25fe4 (lines 425-470, 474-517, 886-912,
913-932), rendered for two parameter sets. :func:`render_instruction` must
match them byte-for-byte — this is what lets the later brain-side switchover
PR prove "same prompts out".

Regenerate the goldens from a checkout of the brain repo with::

    source scripts/github/project-monitoring-lib.sh
    export item_repo=gptme/gptme-contrib item_number=1234 WORKSPACE=/home/bob/bob
    _build_local_greptile_fix_instructions > local_greptile_fix.bob.txt
    _build_cross_repo_greptile_refresh_instructions > cross_repo_greptile_refresh.bob.txt
    item_types="greptile_needs_fix"; build_item_investigate
    printf '%s' "$INVESTIGATE" > greptile_needs_fix.bob.txt
    item_types="greptile_needs_improvement"; build_item_investigate
    printf '%s' "$INVESTIGATE" > greptile_needs_improvement.bob.txt
    # ...and again with the second parameter set (see CONTEXTS below).

Newline conventions locked in here (see the module docstring):

- fix-instruction kinds render the heredoc's stdout **with** its trailing
  newline (the bash ``$(...)`` call site strips it);
- investigate kinds render the ``INVESTIGATE+=`` string **with** its leading
  and trailing newline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_runloops.merge_lifecycle import InstructionKind
from gptme_runloops.prompt_templates import PromptContext, render_instruction

GOLDEN_DIR = Path(__file__).parent / "goldens" / "prompt_templates"

# Two parameter sets: the brain's own values (proving parity with what the
# bash renders in production) and a non-Bob agent (proving the Bob-specific
# bits are really parameters — Alice consumes this package too).
CONTEXTS: dict[str, PromptContext] = {
    "bob": PromptContext(
        repo="gptme/gptme-contrib", number=1234, workspace="/home/bob/bob"
    ),
    "alice": PromptContext(
        repo="ErikBjare/alice", number=7, workspace="/srv/agents/alice/workspace"
    ),
}

ALL_KINDS = tuple(InstructionKind)


# --- Golden parity ---


@pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.value)
@pytest.mark.parametrize("ctx_name", sorted(CONTEXTS))
def test_render_matches_bash_golden(kind: InstructionKind, ctx_name: str) -> None:
    golden = (GOLDEN_DIR / f"{kind.value}.{ctx_name}.txt").read_text()
    assert render_instruction(kind, CONTEXTS[ctx_name]) == golden


def test_every_kind_has_a_template() -> None:
    ctx = CONTEXTS["bob"]
    for kind in InstructionKind:
        assert render_instruction(kind, ctx)


def test_every_kind_and_context_has_a_golden() -> None:
    expected = {
        f"{kind.value}.{ctx}.txt" for kind in InstructionKind for ctx in CONTEXTS
    }
    assert {p.name for p in GOLDEN_DIR.glob("*.txt")} == expected


# --- Newline conventions ---


@pytest.mark.parametrize(
    "kind",
    [InstructionKind.LOCAL_GREPTILE_FIX, InstructionKind.CROSS_REPO_GREPTILE_REFRESH],
    ids=lambda k: k.value,
)
def test_fix_instruction_kinds_render_heredoc_stdout(kind: InstructionKind) -> None:
    out = render_instruction(kind, CONTEXTS["bob"])
    assert out.startswith("### Also: Address Greptile Review Findings")
    assert out.endswith(".\n") and not out.endswith("\n\n")


@pytest.mark.parametrize(
    "kind",
    [InstructionKind.GREPTILE_NEEDS_FIX, InstructionKind.GREPTILE_NEEDS_IMPROVEMENT],
    ids=lambda k: k.value,
)
def test_investigate_kinds_render_append_string(kind: InstructionKind) -> None:
    out = render_instruction(kind, CONTEXTS["bob"])
    assert out.startswith("\n### Greptile Score ")
    assert out.endswith(".\n") and not out.endswith("\n\n")


# --- Parameterization (nothing Bob-specific baked in) ---


@pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.value)
def test_no_bob_paths_leak_into_other_agents_render(kind: InstructionKind) -> None:
    out = render_instruction(kind, CONTEXTS["alice"])
    assert "/home/bob" not in out
    assert "gptme/gptme-contrib" not in out


def test_helper_paths_derive_from_workspace() -> None:
    ctx = PromptContext(repo="o/r", number=1, workspace="/ws")
    assert ctx.resolved_greptile_helper == "/ws/scripts/github/greptile-helper.sh"
    assert (
        ctx.resolved_pr_address_script
        == "/ws/scripts/github/pr-address-wait-and-merge.sh"
    )


def test_explicit_helper_paths_override_workspace_derivation() -> None:
    ctx = PromptContext(
        repo="o/r",
        number=1,
        workspace="/ws",
        greptile_helper="/opt/tools/greptile.sh",
        pr_address_script="/opt/tools/pr-wait.sh",
    )
    cross = render_instruction(InstructionKind.CROSS_REPO_GREPTILE_REFRESH, ctx)
    assert "bash /opt/tools/greptile.sh trigger o/r 1" in cross
    assert "bash /opt/tools/pr-wait.sh --repo o/r 1" in cross
    assert "/ws/scripts" not in cross


def test_poll_budget_is_a_parameter_with_bash_default() -> None:
    default = render_instruction(
        InstructionKind.LOCAL_GREPTILE_FIX, PromptContext("o/r", 1, "/ws")
    )
    assert "POLL_BUDGET_SEC=1800 bash" in default
    custom = render_instruction(
        InstructionKind.LOCAL_GREPTILE_FIX,
        PromptContext("o/r", 1, "/ws", poll_budget_sec=600),
    )
    assert "POLL_BUDGET_SEC=600 bash" in custom


def test_number_accepts_str_and_int() -> None:
    as_int = render_instruction(
        InstructionKind.GREPTILE_NEEDS_FIX, PromptContext("o/r", 42, "/ws")
    )
    as_str = render_instruction(
        InstructionKind.GREPTILE_NEEDS_FIX, PromptContext("o/r", "42", "/ws")
    )
    assert as_int == as_str


# --- Preserved quirks (NOTE(parity) anchors) ---


@pytest.mark.parametrize(
    "kind",
    [InstructionKind.GREPTILE_NEEDS_FIX, InstructionKind.GREPTILE_NEEDS_IMPROVEMENT],
    ids=lambda k: k.value,
)
def test_investigate_jq_malformation_is_preserved(kind: InstructionKind) -> None:
    # NOTE(parity): the bash investigate arms ship a jq object whose
    # `body: (...` group is never closed — a runtime jq syntax error the
    # sibling pr_update arm does not have. The port preserves it; this test
    # pins the quirk so an accidental "fix" fails loudly (fixing it is a
    # switchover-time decision).
    out = render_instruction(kind, CONTEXTS["bob"])
    jq_line = next(
        line for line in out.splitlines() if "split(" in line and "join(" in line
    )
    assert jq_line.count("(") == jq_line.count(")") + 1


def test_step2_qualifier_drift_is_preserved() -> None:
    # NOTE(parity): "usually just P2 nits" (local) vs "usually P2 nits"
    # (cross-repo) — one-word drift between the two heredocs, preserved.
    ctx = CONTEXTS["bob"]
    local = render_instruction(InstructionKind.LOCAL_GREPTILE_FIX, ctx)
    cross = render_instruction(InstructionKind.CROSS_REPO_GREPTILE_REFRESH, ctx)
    assert "(usually just P2 nits, a SUBSET)" in local
    assert "(usually P2 nits, a SUBSET)" in cross


def test_local_fix_never_names_the_greptile_helper() -> None:
    # NOTE(parity): local PRs re-trigger via pr-address-wait-and-merge.sh
    # (internal trigger); only the cross-repo variant instructs an explicit
    # greptile-helper trigger. Intentional lane difference.
    ctx = CONTEXTS["bob"]
    local = render_instruction(InstructionKind.LOCAL_GREPTILE_FIX, ctx)
    cross = render_instruction(InstructionKind.CROSS_REPO_GREPTILE_REFRESH, ctx)
    assert "greptile-helper.sh" not in local
    assert f"greptile-helper.sh trigger {ctx.repo} {ctx.number}" in cross
