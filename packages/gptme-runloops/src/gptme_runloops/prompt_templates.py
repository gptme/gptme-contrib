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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

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
    # NOTE(parity): the jq object in both investigate arms was originally
    # malformed — `body: (.body | split("\n")[0:5] | join(" ")}` never
    # closed the `(` opened after `body:` (a runtime jq syntax error the
    # sibling pr_update arm, lib.sh:688, never had). Fixed in bash by
    # ErikBjare/bob#1067; this template and the goldens mirror the fixed
    # form. (`\n` inside split() is the literal two-character sequence the
    # bash `\\n` renders — correct jq, kept.)
    "read_commands": (
        "# Read the PR and full Greptile review comments\n"
        "gh pr view {number} --repo {repo}\n"
        "gh pr view {number} --repo {repo} --comments\n"
        "\n"
        "# Get Greptile's review comments (the ones that need fixing)\n"
        "gh api repos/{repo}/pulls/{number}/comments \\\n"
        '  --jq \'.[] | select(.user.login | test("greptile"; "i")) | {id, path, line, body: (.body | split("\\n")[0:5] | join(" "))}\'\n'
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
    # NOTE(parity): same jq as greptile_needs_fix (see the fix note there;
    # bash fixed in ErikBjare/bob#1067), with [0:3] instead of [0:5].
    "read_commands": (
        "# Read the PR and Greptile review comments\n"
        "gh pr view {number} --repo {repo}\n"
        "gh api repos/{repo}/pulls/{number}/comments \\\n"
        '  --jq \'.[] | select(.user.login | test("greptile"; "i")) | {id, path, line, body: (.body | split("\\n")[0:3] | join(" "))}\''
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


# ============================================================================
# Item prompts (step 4 — the run-item executor, ErikBjare/bob phase-2/3)
# ============================================================================
#
# Behavior-identical port of the remaining prompt content the detached PM
# slot builds per item (rows 6-7 of the run-item behavior inventory; see
# ``knowledge/technical-designs/pm-run-item-executor-design.md`` in
# ErikBjare/bob):
#
# - ``build_item_investigate`` per-type arms
#   (``scripts/github/project-monitoring-lib.sh:670-935``) →
#   :class:`ItemPromptKind` + :func:`render_item_investigate` /
#   :func:`build_investigate`.
# - The main session-prompt skeleton, the direct-@mention deliverable
#   constraint, and the arc-continuation context block
#   (``scripts/runs/github/project-monitoring.sh:535-578``,
#   ``project-monitoring-lib.sh:96-112``) → :func:`render_main_prompt`,
#   :func:`render_mention_constraint`, :func:`render_arc_context`.
#
# Goldens: ``tests/goldens/run_item/prompts.json`` holds the captured output
# of the real bash builders (sourced/sed-extracted from ErikBjare/bob @
# f95ccae920af0058c68c86083e9007be86710dc5) — see
# ``tests/test_run_item_prompts.py`` for the capture procedure.
#
# Agent-agnostic by construction: everything Bob-specific in the bash
# (identity name, author handle, peer-agent roster, the hardcoded
# ``/home/bob/bob`` in the twitter/forum/agent-msg arms, the Gordon/Erik
# policy paragraph) is a parameter of :class:`ItemPromptParams`. With Bob's
# values the output is byte-identical to the bash; with another agent's
# values nothing Bob remains.

_GREPTILE_INVESTIGATE_KINDS: dict[str, InstructionKind] = {
    "greptile_needs_fix": InstructionKind.GREPTILE_NEEDS_FIX,
    "greptile_needs_improvement": InstructionKind.GREPTILE_NEEDS_IMPROVEMENT,
}


class ItemPromptKind(Enum):
    """The per-type investigate arms of ``build_item_investigate``.

    The two ``greptile_*`` arms are NOT members: they were ported in step 2
    as :class:`~gptme_runloops.merge_lifecycle.InstructionKind` members and
    :func:`build_investigate` dispatches those types to
    :func:`render_instruction` (same template module, same bytes).

    Item types with no arm in the bash ``case`` (notably ``merge_ready``)
    contribute nothing — mirrored by :func:`build_investigate` skipping
    unknown types.
    """

    PR_UPDATE = "pr_update"
    CI_FAILURE = "ci_failure"
    ASSIGNED_ISSUE = "assigned_issue"
    TWITTER_MENTION = "twitter_mention"
    FORUM_MENTION = "forum_mention"
    AGENT_MSG_REPLY = "agent_msg_reply"
    NOTIFICATION = "notification"
    MASTER_CI_FAILURE = "master_ci_failure"
    MERGE_CONFLICT = "merge_conflict"


@dataclass(frozen=True)
class ItemPromptParams:
    """Per-item parameters for the investigate arms and the main prompt.

    Attributes mirror the bash globals; the identity fields parameterize
    what the bash hardcodes:

    - ``agent_name`` ← the literal "Bob" ("You are Bob", "belongs to Bob",
      "for Bob-local PRs").
    - ``operator_name`` ← the literal "Erik" in the direct-mention
      deliverable constraint.
    - ``twitter_handle`` ← the literal "TimeToBuildBob" mention handle
      (empty → falls back to ``author``).
    - ``forum_handle`` ← the literal "bob" agentboard handle (empty → falls
      back to ``agent_name.lower()``).
    - ``peer_agents`` ← the literal "Gordon / Alice / Sven" domain roster.
    - ``agent_msg_policy_note`` ← the trailing Gordon/IBKR policy paragraph
      of the agent-msg arm (Bob passes it; empty renders no paragraph).
    - ``workspace`` ← both ``$WORKSPACE`` *and* the hardcoded
      ``/home/bob/bob`` in the twitter/forum/agent-msg arms (identical for
      Bob, parameterized for everyone else).
    """

    repo: str
    number: int | str
    workspace: str
    detail: str = ""
    all_numbers: tuple[str, ...] = ()
    author: str = ""
    agent_name: str = "Agent"
    operator_name: str = "the operator"
    twitter_handle: str = ""
    forum_handle: str = ""
    peer_agents: str = "other agents"
    agent_msg_policy_note: str = ""
    greptile_helper: str | None = None
    pr_address_script: str | None = None
    poll_budget_sec: int = 1800

    def to_prompt_context(self) -> PromptContext:
        """The step-2 context for the greptile investigate arms."""
        return PromptContext(
            repo=self.repo,
            number=self.number,
            workspace=self.workspace,
            greptile_helper=self.greptile_helper,
            pr_address_script=self.pr_address_script,
            poll_budget_sec=self.poll_budget_sec,
        )

    def _tokens(self) -> dict[str, str]:
        ctx = self.to_prompt_context()
        return {
            "repo": self.repo,
            "number": str(self.number),
            "workspace": self.workspace,
            "detail": self.detail,
            "author": self.author,
            "agent_name": self.agent_name,
            "operator_name": self.operator_name,
            "twitter_handle": self.twitter_handle or self.author,
            "forum_handle": self.forum_handle or self.agent_name.lower(),
            "peer_agents": self.peer_agents,
            "greptile_helper": ctx.resolved_greptile_helper,
            "pr_address_script": ctx.resolved_pr_address_script,
        }


# --- Investigate arm templates (lib.sh:670-935, non-greptile arms) ---

_PR_UPDATE_ARM = """
### PR Review & Comments
```bash
# Read PR details + all comments (NEVER truncate with | head)
gh pr view {number} --repo {repo}
gh pr view {number} --repo {repo} --comments

# Review comments (compact with jq)
gh api repos/{repo}/pulls/{number}/reviews \\
  --jq '.[] | {user: .user.login, state: .state, body: .body}'

# Inline review comments (CRITICAL — often missed!)
gh api repos/{repo}/pulls/{number}/comments \\
  --jq '.[] | {id, path, user: .user.login, body: (.body | split("\\n")[0:3] | join(" "))}'

# CI status
gh pr checks {number} --repo {repo}

# Merge conflict status
gh pr view {number} --repo {repo} --json mergeable,mergeStateStatus
```

**Important**: Check ALL comments — human AND bot (Greptile, etc.). Respond to every
human comment. If Greptile has unresolved findings, address them, push fixes, then
run `bash {pr_address_script} --repo {repo} {number}`
for {agent_name}-local PRs, or `bash {greptile_helper} trigger {repo} {number}`
and exit for cross-repo PRs. If it exits 2 or 3, stop there. The next monitoring cycle will pick up any remaining work.
Never ignore a human comment in favor of bot review work.
"""

_CI_FAILURE_ARM = """
### CI Failure Investigation
```bash
# Check which CI checks are failing
gh pr checks {number} --repo {repo}

# Get details on failures
gh pr checks {number} --repo {repo} --json name,state,link \\
  --jq '.[] | select(.state == "FAILURE")'

# For each failing check, read the logs:
# gh run view RUN_ID --repo {repo} --log-failed | tail -40
```

Also check master branch CI health:
```bash
gh run list --repo {repo} --branch master --limit 3 --json name,conclusion,createdAt,url \\
  --jq '.[] | select(.conclusion == "failure") | {name, conclusion, createdAt, url}'
```
"""

_ASSIGNED_ISSUE_ARM = """
### Issue Details
```bash
gh issue view {number} --repo {repo}
gh issue view {number} --repo {repo} --comments
```

**Close the loop requirement**:
1. Decide whether this belongs to {agent_name} or another agent domain ({peer_agents}).
2. If it belongs to another agent: create or update the task on that agent's VM/workspace, then reply on the issue confirming the handoff.
3. If it belongs to {agent_name} and won't be fully finished in this run: create or update a local task file using the idempotent helper (safe to call multiple times — no duplicates):
   ```bash
   python3 {workspace}/scripts/tasks/promote_from_github.py {repo} {number} --priority medium
   # Returns "EXISTS: PATH" or "CREATED: PATH" — never creates a duplicate
   ```
4. After promotion, edit the created/found task file to add a concrete `next_action:` field and a `tracking_issue:` entry pointing at the live GitHub thread.
5. Reply on the issue with what changed (work done, task created/updated, or handoff), not just a vague acknowledgment.

For any multi-paragraph reply, avoid inline shell escapes like `"para1\\n\\npara2"`.
Pipe the body into:
```bash
cat <<'EOF' | {workspace}/scripts/github/comment-from-stdin.sh {repo} {number} --anti-spam
Paragraph one.

Paragraph two.
EOF
```
If you genuinely need the older short cooldown for rapid monitoring follow-ups, opt in explicitly:
```bash
cat <<'EOF' | {workspace}/scripts/github/comment-from-stdin.sh {repo} {number} --anti-spam --anti-spam-seconds 600
Paragraph one.

Paragraph two.
EOF
```
"""

_TWITTER_MENTION_ARM = """
### Twitter Mention (Trusted-User Task Request)
A trusted Twitter user has mentioned @{twitter_handle} with what looks like a task request.

Tweet details: {detail}

**How to handle:**
1. Read the mention carefully — what is the user asking for?
2. Determine if this is actionable (code change, research, answer, etc.)
3. If actionable: do the work, then reply to the tweet with results
4. If not actionable: reply explaining why or asking for clarification

To post the tweet reply:
```bash
cat <<'EOF' | {workspace}/scripts/runs/twitter/post-from-stdin.sh --reply-to TWEET_ID
Your reply text here.

Second paragraph if needed.
EOF
```

To mark the mention as dispatched (so it won't re-trigger):
```bash
cd {workspace} && uv run python3 scripts/runs/twitter/twitter-dispatch.py --mark-dispatched --scan-replies
```
"""

_FORUM_MENTION_ARM = """
### Agentboard Forum Mention
Another agent mentioned @{forum_handle} in the shared git-native forum.

Mention details: {detail}

**How to handle:**
1. Read the referenced post/comment thread in full
2. Determine whether the mention requests action, input, or just awareness
3. If action is needed: do the work or reply in-thread with the outcome
4. If no action is needed: reply briefly so the thread is closed-loop

To inspect the thread:
```bash
cd {workspace}
uv run python3 scripts/runs/forum/forum-dispatch.py --dry-run --agent {forum_handle}
# Then read the post/comment directly via agentboard or the forum files
AGENTBOARD_FORUM_DIR={workspace}/gptme-superuser/forum .venv/bin/agentboard post read PROJECT/SLUG
```

To reply in-thread:
```bash
cd {workspace}
AGENTBOARD_FORUM_DIR={workspace}/gptme-superuser/forum .venv/bin/agentboard comment add PROJECT/SLUG 'Reply text here'
```

To mark the mention as dispatched (so it won't re-trigger):
```bash
cd {workspace} && uv run python3 scripts/runs/forum/forum-dispatch.py --mark-dispatched MENTION_ID
```
"""

# NOTE(parity): the bash arm ends with the Gordon/IBKR policy paragraph —
# learned Bob-side policy, injected here via ``agent_msg_policy_note``
# (rendered as {policy_block}; empty note → no paragraph).
_AGENT_MSG_REPLY_ARM = """
### Inter-Agent Message Reply (agent-msg)
An inter-agent message is read but unreplied. Message details: {detail}

**CLAIM before handling to prevent duplicate replies:**
```bash
cd {workspace}
# Extract message filename from detail above
uv run coordination work-claim "pm-agent-msg-$$" "agent-msg:reply:MSG_FILE" --ttl 30
# If denied: skip (another session is handling it)
```

**Read and reply:**
```bash
cd {workspace}
python3 scripts/agent-msg.py read MSG_FILE
python3 scripts/agent-msg.py reply MSG_FILE "Your reply text"
uv run coordination work-complete "pm-agent-msg-$$" "agent-msg:reply:MSG_FILE"
```
{policy_block}"""

_NOTIFICATION_ARM = """
### Notifications
```bash
gh api notifications \\
  --jq '.[] | select(.reason == "review_requested" or .reason == "mention" or .reason == "assign") | {subject: .subject.title, type: .subject.type, reason: .reason, url: .subject.url, repo: .repository.full_name}'
```
"""

# NOTE(parity): ``{run_cmds}`` carries one trailing newline per grouped run
# (built exactly like the bash ``_run_cmds`` loop), and the skeleton adds its
# own newline after the token — producing the blank line between the last
# ``gh run view`` and the "# Check recent runs" comment, as the bash does.
_MASTER_CI_FAILURE_ARM = """
### Master Branch CI Failure
```bash
# Check all failing run logs (grouped from {all_numbers_joined} runs)
{run_cmds}
# Check recent runs on master to see if this is a flaky test or real regression
gh run list --repo {repo} --branch master --limit 5 \\
  --json name,conclusion,createdAt,headSha \\
  --jq '.[] | {name, conclusion, createdAt, sha: .headSha[:8]}'
```
"""

_MERGE_CONFLICT_ARM = """
### Merge Conflict Resolution
```bash
# Check the PR and its conflict status
gh pr view {number} --repo {repo}
gh pr view {number} --repo {repo} --json mergeable,mergeStateStatus,headRefName

# Check what files conflict
# (will need to clone/worktree and attempt rebase to see actual conflicts)
```

To resolve: create a worktree, rebase onto master, resolve conflicts, force-push.
"""

_ITEM_ARMS: dict[ItemPromptKind, str] = {
    ItemPromptKind.PR_UPDATE: _PR_UPDATE_ARM,
    ItemPromptKind.CI_FAILURE: _CI_FAILURE_ARM,
    ItemPromptKind.ASSIGNED_ISSUE: _ASSIGNED_ISSUE_ARM,
    ItemPromptKind.TWITTER_MENTION: _TWITTER_MENTION_ARM,
    ItemPromptKind.FORUM_MENTION: _FORUM_MENTION_ARM,
    ItemPromptKind.AGENT_MSG_REPLY: _AGENT_MSG_REPLY_ARM,
    ItemPromptKind.NOTIFICATION: _NOTIFICATION_ARM,
    ItemPromptKind.MASTER_CI_FAILURE: _MASTER_CI_FAILURE_ARM,
    ItemPromptKind.MERGE_CONFLICT: _MERGE_CONFLICT_ARM,
}


def render_item_investigate(kind: ItemPromptKind, params: ItemPromptParams) -> str:
    """Render one investigate arm; byte-identical to the bash ``case`` arm.

    Output includes the arm's leading and trailing newline (the bash appends
    ``INVESTIGATE+="\\n...\\n"``).
    """
    template = _ITEM_ARMS[kind]
    tokens = params._tokens()
    if kind is ItemPromptKind.MASTER_CI_FAILURE:
        # lib.sh:855-870 — one `gh run view` line (with trailing newline) per
        # grouped run number; the header joins the numbers with ", ".
        numbers = [str(n) for n in params.all_numbers] or [str(params.number)]
        tokens["run_cmds"] = "".join(
            f"gh run view {n} --repo {params.repo} --log-failed | tail -60\n"
            for n in numbers
        )
        tokens["all_numbers_joined"] = ", ".join(numbers)
    if kind is ItemPromptKind.AGENT_MSG_REPLY:
        note = params.agent_msg_policy_note
        tokens["policy_block"] = f"\n{note}\n" if note else "\n"
    return _substitute(template, tokens)


def build_investigate(types: Sequence[str], params: ItemPromptParams) -> str:
    """Concatenate investigate arms for every type in *types*, in order.

    Behavior-identical to ``build_item_investigate`` (lib.sh:670-935): types
    are processed in the given order (the grouped item's ``types`` array is
    jq-``unique``, i.e. sorted); types with no arm contribute nothing
    (``merge_ready`` has no arm — a merge_ready-only item yields ``""``);
    the ``greptile_needs_fix`` / ``greptile_needs_improvement`` types render
    via the step-2 :func:`render_instruction` templates.
    """
    sections: list[str] = []
    for t in types:
        if t in _GREPTILE_INVESTIGATE_KINDS:
            sections.append(
                render_instruction(
                    _GREPTILE_INVESTIGATE_KINDS[t], params.to_prompt_context()
                )
            )
            continue
        try:
            kind = ItemPromptKind(t)
        except ValueError:
            continue
        sections.append(render_item_investigate(kind, params))
    return "".join(sections)


# --- Direct-@mention deliverable constraint (project-monitoring.sh:535-542) ---

_MENTION_CONSTRAINT = """
## Required: Produce a Deliverable (Direct @Mention)

This item is a **direct @mention from {operator_name}**. Before completing or exiting, you MUST do one of:
1. **Produce the explicitly requested deliverable** (open a tracking issue, post a reply, submit a PR, etc.), OR
2. **Reply to the thread** explaining what you did or why you cannot fulfil the request right now.

A silent NOOP is not acceptable for a direct {operator_name} mention. If you can only do part of the ask, do the simple parts (e.g. open an issue) and note the rest in your reply."""


def render_mention_constraint(params: ItemPromptParams) -> str:
    """The direct-mention NOOP-guard block (starts with a newline, like the bash)."""
    return _substitute(_MENTION_CONSTRAINT, params._tokens())


# --- Arc continuation context (project-monitoring-lib.sh:96-112) ---

_ARC_CONTEXT = """
## Arc Continuation Context

This item has an existing multi-session arc ({arc_sessions} prior session(s)).
This monitoring reaction is an **arc continuation**, not a fresh start.

Arc hint for next step: {arc_hint}

This worker will append its own continuation record automatically after it finishes.
If you materially change the plan, refresh the arc hint before you exit:
```bash
python3 {workspace}/scripts/tasks/arc_manager.py write \\
    '{arc_id}' \\
    --upstream-id '{upstream_id}' \\
    --next-step-hint 'What the next session should do'
```
"""


def render_arc_context(
    params: ItemPromptParams,
    *,
    arc_id: str,
    arc_hint: str,
    arc_sessions: int | str,
) -> str:
    """The arc-continuation block (leading and trailing newline, like the bash).

    ``upstream_id`` is derived as ``github:{repo}#{number}`` exactly like
    ``build_arc_context`` does.
    """
    tokens = params._tokens()
    tokens.update(
        {
            "arc_id": arc_id,
            "arc_hint": arc_hint,
            "arc_sessions": str(arc_sessions),
            "upstream_id": f"github:{params.repo}#{params.number}",
        }
    )
    return _substitute(_ARC_CONTEXT, tokens)


# --- Pre-held claim block (NEW content — no bash counterpart) ---
#
# Reserved for the step-7/8 dispatcher-held-claim mode (phase-2 doc,
# interaction risk #1): when the dispatcher already holds the
# ``github:REPO#NUM`` claim, the prompt MUST name it or the session
# self-blinds on its own claim (the 2026-07-04 calm-window failure class).
# Rendered ONLY in ``--claim-mode preheld``; acquire/none modes render ""
# so step-4/5 output stays byte-identical to the bash.

_PREHELD_CLAIM_BLOCK = """
## Coordination Claim (pre-held)

The coordination claim `{claim_key}` for this work item is ALREADY HELD on your behalf by the dispatcher.
Do NOT try to re-claim it — a "denied" result for this exact key means the claim is YOURS; proceed with the work.
Claim keys for OTHER work items are still authoritative: a denied claim on a different key means skip that work."""


def render_preheld_claim_block(claim_key: str) -> str:
    """The pre-held-claim prompt block (starts with a newline, like the other optional blocks)."""
    return _substitute(_PREHELD_CLAIM_BLOCK, {"claim_key": claim_key})


# --- Main session-prompt skeleton (project-monitoring.sh:545-578) ---

_MAIN_PROMPT = """You are {agent_name}, running a focused project monitoring session. Your identity files have been injected as system context.

## Your Task

Investigate and act on this work item:

- **Event(s)**: {item_type}
- **Repo**: {repo}
- **Number**: #{number}
- **Title**: {title}
- **Detail**: {detail}

Your GitHub author name is: {author}
{greptile_block}
{arc_context}
{mention_constraint}{preheld_block}

## Step 1: Investigate (3 min)

Get full context for this item. Read ALL sources — never truncate output.
{investigate}

## Step 2: Classify & Execute

{monitoring_rules}

## Time Budget

You have {time_desc} available for this item.
- Treat the limit as a stall guard, not a rush order.
- Keep naturally sequential work together when it fits: investigate -> fix -> verify -> reply.
- Do not skip verification or compress analysis just to finish early.
- If the item needs no action after investigation, just exit. No journal. No commit."""


def render_main_prompt(
    params: ItemPromptParams,
    *,
    item_type: str,
    title: str,
    investigate: str,
    monitoring_rules: str,
    time_desc: str,
    greptile_fix_instructions: str = "",
    arc_context: str = "",
    mention_constraint: str = "",
    preheld_block: str = "",
) -> str:
    """Render the full per-item session prompt (project-monitoring.sh:545-578).

    Byte-identical to the bash ``PROMPT=`` assignment for empty
    ``preheld_block``:

    - ``greptile_fix_instructions`` is the ``$(...)``-assigned form (no
      trailing newline — pass ``render_instruction(...).rstrip("\\n")``);
      non-empty values are prefixed with a newline exactly like the bash
      ``${GREPTILE_FIX_INSTRUCTIONS:+\\n$GREPTILE_FIX_INSTRUCTIONS}``.
    - ``arc_context`` / ``mention_constraint`` are inserted verbatim (their
      templates carry their own leading newlines; empty means absent).
    - ``preheld_block`` is the step-7/8 extension point; empty (the step-4/5
      default) renders the exact bash skeleton.
    """
    tokens = params._tokens()
    tokens.update(
        {
            "item_type": item_type,
            "title": title,
            "investigate": investigate,
            "monitoring_rules": monitoring_rules,
            "time_desc": time_desc,
            "greptile_block": (
                f"\n{greptile_fix_instructions}" if greptile_fix_instructions else ""
            ),
            "arc_context": arc_context,
            "mention_constraint": mention_constraint,
            "preheld_block": preheld_block,
        }
    )
    return _substitute(_MAIN_PROMPT, tokens)
