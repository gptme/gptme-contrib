"""Greptile fix/improve/re-trigger prompt templates (parameterized).

Behavior-identical consolidation of the four Greptile prompt-building
heredocs from ErikBjare/bob ``scripts/github/project-monitoring-lib.sh``
(step 2 of the Phase-2 execution-consolidation migration; see
``knowledge/technical-designs/reactive-dispatch-phase2-3-execution-consolidation.md``
in that repo). Step 1 (contrib#1261, :mod:`gptme_runloops.merge_lifecycle`)
ported the *decisions* and left :class:`~gptme_runloops.merge_lifecycle.InstructionKind`
as the seam; this module renders the instruction *bodies* the decisions name.
The bash remains the runtime hotpath until the brain-side switchover PR; the
golden tests in ``tests/test_prompt_templates.py`` are what that switchover
will diff against.

Bash source heredocs ported (ErikBjare/bob @
ca7aa17a2899dbe676fba7074fc5c4dd61d25fe4):

- ``project-monitoring-lib.sh:425-470`` — ``_build_local_greptile_fix_instructions``
  → :attr:`InstructionKind.LOCAL_GREPTILE_FIX`
- ``project-monitoring-lib.sh:474-517`` — ``_build_cross_repo_greptile_refresh_instructions``
  → :attr:`InstructionKind.CROSS_REPO_GREPTILE_REFRESH`
- ``project-monitoring-lib.sh:886-912`` — ``build_item_investigate`` arm
  ``greptile_needs_fix`` → :attr:`InstructionKind.GREPTILE_NEEDS_FIX`
- ``project-monitoring-lib.sh:913-932`` — ``build_item_investigate`` arm
  ``greptile_needs_improvement`` → :attr:`InstructionKind.GREPTILE_NEEDS_IMPROVEMENT`

The four heredocs are ~90% pairwise-duplicate. They collapse into two shared
skeletons with per-variant parameter tables:

- **Fix-instruction skeleton** (the two ``_build_*`` functions): title suffix,
  intro paragraph, STEP-1 comment, STEP-2 qualifier, follow-up block, warning
  block, and the do-not-loop block vary; the STEP-1/STEP-2 ``gh api`` command
  blocks are shared verbatim.
- **Investigate skeleton** (the two ``build_item_investigate`` arms): title,
  read-commands block, assessment paragraph, and warning block vary; the
  greptile-helper re-trigger command block is shared verbatim.

Rendering parity (exact bytes, locked by golden tests):

- ``LOCAL_GREPTILE_FIX`` / ``CROSS_REPO_GREPTILE_REFRESH`` render the exact
  stdout of the bash function, **including the trailing newline** the heredoc
  emits. The bash call site assigns via ``$(...)`` which strips it; callers
  that need the assigned-variable form should ``.rstrip("\\n")``.
- ``GREPTILE_NEEDS_FIX`` / ``GREPTILE_NEEDS_IMPROVEMENT`` render the exact
  string the bash appends to ``INVESTIGATE``, **including the leading and
  trailing newline**.

Agent-agnostic by construction: workspace paths, helper-script locations, and
repo/PR identifiers are all :class:`PromptContext` parameters — nothing
Bob-specific is baked into the templates (Alice consumes this package too).

Where the variants differ in ways that look like drift rather than intent,
the difference is preserved and marked with a ``# NOTE(parity):`` comment.
Behavior changes come later, with the brain-side switchover.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from gptme_runloops.merge_lifecycle import InstructionKind

# --- Context ---


@dataclass(frozen=True)
class PromptContext:
    """Per-render parameters extracted from the bash globals.

    Attributes:
        repo: ``owner/name`` of the PR's repository (bash ``$item_repo``).
        number: PR number (bash ``$item_number``).
        workspace: absolute path to the agent's workspace repo (bash
            ``$WORKSPACE``); used only to derive the helper-script paths
            below when they are not given explicitly.
        greptile_helper: path to ``greptile-helper.sh`` (the anti-spam
            trigger wrapper). Default: ``<workspace>/scripts/github/greptile-helper.sh``.
        pr_address_script: path to ``pr-address-wait-and-merge.sh`` (the
            address→wait→merge poller). Default:
            ``<workspace>/scripts/github/pr-address-wait-and-merge.sh``.
        poll_budget_sec: ``POLL_BUDGET_SEC`` value baked into the
            pr-address-wait-and-merge invocation lines (bash hardcodes 1800).
    """

    repo: str
    number: int | str
    workspace: str
    greptile_helper: str | None = None
    pr_address_script: str | None = None
    poll_budget_sec: int = 1800

    @property
    def resolved_greptile_helper(self) -> str:
        if self.greptile_helper is not None:
            return self.greptile_helper
        return f"{self.workspace}/scripts/github/greptile-helper.sh"

    @property
    def resolved_pr_address_script(self) -> str:
        if self.pr_address_script is not None:
            return self.pr_address_script
        return f"{self.workspace}/scripts/github/pr-address-wait-and-merge.sh"


# --- Token substitution ---
#
# str.format() would choke on the literal jq braces in the command blocks
# (``{id, path, line, body}``), so substitution replaces only the exact
# ``{name}`` tokens present in the mapping and leaves everything else alone.

_TOKEN_RE = re.compile(r"\{([a-z0-9_]+)\}")


def _substitute(template: str, mapping: Mapping[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return mapping[key] if key in mapping else match.group(0)

    return _TOKEN_RE.sub(repl, template)


# --- Skeleton A: session-injected fix instructions (lib.sh:425-517) ---
#
# Shared shape of _build_local_greptile_fix_instructions and
# _build_cross_repo_greptile_refresh_instructions. The STEP-1/STEP-2 gh
# command blocks are byte-identical between the two bash functions; the
# prose around them varies per variant.

_FIX_SKELETON = """\
### Also: Address Greptile Review Findings{title_suffix}

{intro}

**STEP 1 — Read the score-DRIVING findings in the summary comment (do this FIRST):**
```bash
{step1_comment}
gh api repos/{repo}/issues/{number}/comments --paginate \\
  --jq '.[] | select(.user.login | test("greptile"; "i"))
            | select(.body | test("Confidence Score"; "i")) | .body'
```

**STEP 2 — Read the inline review threads (usually {step2_qualifier}P2 nits, a SUBSET):**
```bash
gh api repos/{repo}/pulls/{number}/comments \\
  --jq '.[] | select(.user.login | test("greptile"; "i")) | {id, path, line, body}'
```

**Fix the substantive errors from the summary body FIRST**, then the inline
findings (P1/P2/P3). {followup}

{warning}

{no_loop}
"""

# NOTE(parity): the local variant's intro never names the PR (no
# repo#number interpolation) while the cross-repo intro does — preserved
# from lib.sh:429 vs lib.sh:478.
_LOCAL_FIX_SECTIONS = {
    "title_suffix": "",
    "intro": (
        "This PR has a Greptile review below the score floor (or unresolved findings).\n"
        "The confidence score is driven by the **summary-comment body**, not the inline\n"
        "P2 nits. Fixing only inline threads leaves the score unchanged (fake remediation)."
    ),
    # NOTE(parity): the local STEP-1 comment is the 3-line explanatory form
    # ("— THESE are what move the score. Read them:"); the cross-repo one
    # (below) is a 2-line condensed rewrite of the same content. Looks like
    # one heredoc was updated and the other independently shortened —
    # preserved verbatim (lib.sh:435-437 vs lib.sh:484-485).
    "step1_comment": (
        "# The Greptile SUMMARY comment is an ISSUE comment (not an inline thread). It\n"
        '# contains "Confidence Score", the confirmed-errors prose, and the per-file\n'
        '# "Important Files Changed" notes — THESE are what move the score. Read them:'
    ),
    # NOTE(parity): "usually just P2 nits" here vs "usually P2 nits" in the
    # cross-repo variant (lib.sh:443 vs lib.sh:491) — one-word drift,
    # preserved.
    "step2_qualifier": "just ",
    "followup": (
        "A fix that only touches inline nits forces an extra\n"
        "round-trip and the score does not move.\n"
        "\n"
        "**After addressing the findings:**\n"
        "1. Push fixes and reply to each comment thread individually with the fix SHA\n"
        "2. Run `POLL_BUDGET_SEC={poll_budget_sec} bash {pr_address_script} --repo {repo} {number}`\n"
        "   - Exit 0: merged — done\n"
        "   - Exit 2: Greptile re-reviewed and found new issues — read the new unresolved threads, fix them, push, reply, then re-run the command once more\n"
        "   - Exit 3: poll budget exhausted without Greptile re-review completing — stop; next monitoring cycle will continue"
    ),
    # NOTE(parity): the local variant never mentions the greptile-helper —
    # re-triggering for local PRs flows through pr-address-wait-and-merge.sh
    # (which triggers internally); the cross-repo variant instructs an
    # explicit helper trigger. Intentional lane difference, not drift.
    "warning": (
        "⚠️ **If you cannot fix the findings, do NOT re-trigger Greptile and do NOT call pr-address-wait-and-merge.sh.**\n"
        "NEVER post raw `@greptileai review` comments — the pr-address-wait-and-merge.sh script triggers Greptile internally.\n"
        "Only call it AFTER you have actually pushed fix commits."
    ),
    "no_loop": (
        "**Do NOT loop to chase 5/5.** A confidence score is a holistic judgment, not a\n"
        "resolved-thread counter — some PRs will not reach 5/5 even after every stated\n"
        "finding is genuinely addressed. Address the summary-body findings, re-review\n"
        "**once**, and if the score still does not clear the floor, **stop and leave it\n"
        "for human review** (the self-merge gate correctly blocks below-floor merges)."
    ),
}

_CROSS_REPO_REFRESH_SECTIONS = {
    "title_suffix": " (cross-repo)",
    "intro": (
        "This PR ({repo}#{number}) has a Greptile review that is stale or below\n"
        "the score floor. The confidence score is driven by the **summary-comment body**,\n"
        "not the inline P2 nits — fixing only inline threads leaves the score unchanged."
    ),
    "step1_comment": (
        '# The Greptile SUMMARY comment is an ISSUE comment containing "Confidence Score",\n'
        '# the confirmed-errors prose, and the per-file "Important Files Changed" notes.'
    ),
    "step2_qualifier": "",
    "followup": (
        "After fixing AND pushing commits, re-trigger Greptile:\n"
        "```bash\n"
        "# ONLY call this AFTER pushing fix commits — NEVER trigger without actual fixes\n"
        "bash {greptile_helper} trigger {repo} {number}\n"
        "```\n"
        "Then run `POLL_BUDGET_SEC={poll_budget_sec} bash {pr_address_script} --repo {repo} {number}` to wait for the re-review.\n"
        "- Exit 0: merged — done\n"
        "- Exit 2: new findings — address them, push, reply, re-run once more\n"
        "- Exit 3: poll budget exhausted — stop; next monitoring cycle will continue"
    ),
    "warning": (
        "⚠️ **If you cannot fix the issues this session, do NOT re-trigger Greptile.**\n"
        "**NEVER post raw `@greptileai review` directly — ALWAYS use the helper script (has spam guards).**\n"
        "Multiple sessions may see the same state; the helper's flock+age guards prevent concurrent spam."
    ),
    # NOTE(parity): the exit-code bullets and the do-not-loop paragraph are
    # independently-worded rewrites of the local variant's (e.g. "Greptile
    # re-reviewed and found new issues" vs "new findings"; indented vs
    # flush-left bullets) — preserved verbatim per variant.
    "no_loop": (
        "**Do NOT loop to chase 5/5.** Address the summary-body findings, re-review\n"
        "**once**, and if the score still does not clear the floor after the findings are\n"
        "genuinely addressed, **stop and leave it for human review** (the self-merge gate\n"
        "correctly blocks below-floor merges; don't auto-merge below floor)."
    ),
}


# --- Skeleton B: investigate-section blocks (lib.sh:886-932) ---
#
# Shared shape of the greptile_needs_fix / greptile_needs_improvement arms
# of build_item_investigate. These are string appends (not heredocs) that
# start and end with a newline; the greptile-helper re-trigger block is
# byte-identical between the two arms.

_INVESTIGATE_SKELETON = """
### {title}
```bash
{read_commands}
```

{assessment}
```bash
bash {greptile_helper} trigger {repo} {number}
```

{warnings}
"""

_NEEDS_FIX_SECTIONS = {
    "title": "Greptile Score Fix Needed (score < 4/5)",
    # NOTE(parity): the jq object in both investigate arms is malformed —
    # `body: (.body | split("\n")[0:5] | join(" ")}` never closes the `(`
    # opened after `body:`, so jq fails at runtime with a syntax error.
    # The sibling pr_update arm (lib.sh:688) has the closing paren, which
    # is the drift tell. Preserved byte-for-byte; fixing the jq is a
    # switchover-time decision. (`\n` inside split() is the literal
    # two-character sequence the bash `\\n` renders — correct jq, kept.)
    "read_commands": (
        "# Read the PR and full Greptile review comments\n"
        "gh pr view {number} --repo {repo}\n"
        "gh pr view {number} --repo {repo} --comments\n"
        "\n"
        "# Get Greptile's review comments (the ones that need fixing)\n"
        "gh api repos/{repo}/pulls/{number}/comments \\\n"
        '  --jq \'.[] | select(.user.login | test("greptile"; "i")) | {id, path, line, body: (.body | split("\\n")[0:5] | join(" ")}\'\n'
        "\n"
        "# Get Greptile's summary review comment (contains score like 3/5 or 4/5)\n"
        "gh api repos/{repo}/issues/{number}/comments \\\n"
        '  --jq \'.[] | select(.user.login | test("greptile"; "i")) | {id, body} | select(.body | test("/5"))\' | tail -5'
    ),
    "assessment": (
        "This PR has a low Greptile score and likely has real code issues.\n"
        "**Action**: Read the Greptile findings, fix the issues in the PR's repo, push commits, then re-trigger:"
    ),
    # NOTE(parity): this arm says "leave it for the next cycle" while the
    # LOCAL_GREPTILE_FIX variant says "leave it for human review" — the
    # investigate lane retries via monitoring cycles, the self-merge lane
    # escalates. Different by design; listed as a switchover-time review
    # point because the divergence is easy to mistake for drift.
    "warnings": (
        "⚠️ **Only re-trigger if you ACTUALLY pushed fixes addressing the Greptile findings.**\n"
        "If you cannot fix the issues in this session, do NOT re-trigger — leave it for the next cycle.\n"
        "NEVER post raw `@greptileai review` directly — ALWAYS use the helper script above (it has spam guards)."
    ),
}

_NEEDS_IMPROVEMENT_SECTIONS = {
    "title": "Greptile Score Improvement (score = 4/5)",
    # NOTE(parity): same malformed jq as greptile_needs_fix (missing `)`),
    # with [0:3] instead of [0:5]. Preserved.
    "read_commands": (
        "# Read the PR and Greptile review comments\n"
        "gh pr view {number} --repo {repo}\n"
        "gh api repos/{repo}/pulls/{number}/comments \\\n"
        '  --jq \'.[] | select(.user.login | test("greptile"; "i")) | {id, path, line, body: (.body | split("\\n")[0:3] | join(" ")}\''
    ),
    "assessment": (
        "This PR scored 4/5 — minor issues from Greptile. Address them if quick; **leave it untouched if trivial** (do NOT re-trigger just because you looked at it).\n"
        "Re-trigger ONLY if you actually pushed fixes:"
    ),
    # NOTE(parity): greptile_needs_fix ends its NEVER-post-raw line with
    # "(it has spam guards)."; this arm ends it with a bare period
    # (lib.sh:911 vs lib.sh:930) — drift, preserved.
    "warnings": (
        "⚠️ **Never re-trigger without having pushed at least one fix commit.**\n"
        "NEVER post raw `@greptileai review` directly — ALWAYS use the helper script above.\n"
        "If you skip fixing (issues are truly trivial or out of scope), leave the PR alone and do NOT re-trigger."
    ),
}


# --- Variant table ---

_VARIANTS: dict[InstructionKind, tuple[str, Mapping[str, str]]] = {
    InstructionKind.LOCAL_GREPTILE_FIX: (_FIX_SKELETON, _LOCAL_FIX_SECTIONS),
    InstructionKind.CROSS_REPO_GREPTILE_REFRESH: (
        _FIX_SKELETON,
        _CROSS_REPO_REFRESH_SECTIONS,
    ),
    InstructionKind.GREPTILE_NEEDS_FIX: (_INVESTIGATE_SKELETON, _NEEDS_FIX_SECTIONS),
    InstructionKind.GREPTILE_NEEDS_IMPROVEMENT: (
        _INVESTIGATE_SKELETON,
        _NEEDS_IMPROVEMENT_SECTIONS,
    ),
}


def render_instruction(kind: InstructionKind, ctx: PromptContext) -> str:
    """Render the instruction body for ``kind`` with ``ctx`` parameters.

    Output is byte-identical to the corresponding bash builder (see the
    module docstring for the newline conventions each family preserves).
    """
    try:
        skeleton, sections = _VARIANTS[kind]
    except KeyError:  # pragma: no cover - defensive; enum is closed
        raise ValueError(f"no template registered for {kind!r}") from None
    params = {
        "repo": ctx.repo,
        "number": str(ctx.number),
        "greptile_helper": ctx.resolved_greptile_helper,
        "pr_address_script": ctx.resolved_pr_address_script,
        "poll_budget_sec": str(ctx.poll_budget_sec),
    }
    # Sections may themselves carry {repo}/{number}/... tokens — resolve
    # them first, then fill the skeleton with sections + params in one pass.
    resolved_sections = {k: _substitute(v, params) for k, v in sections.items()}
    return _substitute(skeleton, {**resolved_sections, **params})
