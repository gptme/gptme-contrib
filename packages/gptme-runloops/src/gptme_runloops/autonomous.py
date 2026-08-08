"""Autonomous run loop implementation."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.executor import Executor
from gptme_runloops.utils.prompt import generate_base_prompt, get_agent_name


def is_capable_backend(backend: str, model: str = "") -> bool:
    """Return True when this backend+model combo suits self-review / cleanup tasks.

    Mirrors the bash ``_is_capable_backend()`` in autonomous-run.sh.
    """
    if backend == "claude-code":
        return True
    if model.startswith("glm-5"):
        return True
    return False


def self_review_hours_since_last(state_path: Path) -> float:
    """Return hours since the last self-review, or 999.0 if the state is absent/corrupt."""
    try:
        with open(state_path) as f:
            ts = json.load(f).get("timestamp", "")
        dt = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 3600
    except Exception:
        return 999.0


def self_review_cooldown_active(state_path: Path, cooldown_hours: float = 6.0) -> bool:
    """Return True when the self-review cooldown is still active (last review was too recent).

    Mirrors the inline bash time-check in ``_apply_cascade_routing()``.
    Returns False when the state file is absent (no prior review → no cooldown).
    """
    return self_review_hours_since_last(state_path) < cooldown_hours


def parse_cascade_selector_output(data: dict) -> str:
    """Parse cascade-selector JSON output into space-separated shell fields.

    Extracts 7 space-separated tokens for bash ``read -r`` consumption:
    ``scope selected_id category execution_category all_blocked selector_mode intent_json``

    This is the shared implementation used by Bob's production
    ``scripts/runs/autonomous/autonomous-run.sh`` caller. That caller lives in
    the agent workspace (not this reusable package repository) and invokes the
    ``parse-cascade-json`` module command below. Keep the shell integration
    contract covered by ``test_bob_runtime_invokes_parse_cascade_cli``.
    """
    tier = data.get("tier", 0)
    blocked = len(data.get("blocked_tasks", []))
    all_blocked = "true" if (tier == 3 and blocked > 0) else "false"

    sel: dict = data.get("selected", {})
    effective = sel
    execution_surface = sel.get("execution_surface")
    if (
        sel.get("selection_mode") == "synthetic_calibration"
        and isinstance(execution_surface, dict)
        and execution_surface.get("category")
    ):
        effective = execution_surface

    reason_str = sel.get("reason", "")
    reasons = sel.get("reasons") or ([reason_str] if reason_str else [])
    intent: dict = {"reasons": reasons, "tier": tier}

    selection_mode = sel.get("selection_mode", "")
    if selection_mode:
        intent["selection_mode"] = selection_mode

    execution_category = effective.get("category", "")
    if selection_mode == "synthetic_calibration" and execution_category:
        intent["execution_category"] = execution_category
        execution_label = effective.get("label", "")
        if execution_label:
            intent["execution_label"] = execution_label
        execution_id = effective.get("id", "")
        if execution_id:
            intent["execution_id"] = execution_id

    task_state = sel.get("state", "")
    # Tier 0 = assigned GitHub issue: real demand-side lane with id/label but no
    # task-file 'state'. Record task_id so factory-ingest-health counts it as demand.
    # Do NOT set task_state for Tier 0 — an empty task_state keeps the claim gate
    # from inventing a bogus cascade:task:<issue-id> claim (test_only_task_backed_selections).
    is_assigned_issue = tier == 0 and bool(sel.get("id"))
    if task_state or is_assigned_issue:
        task_id = sel.get("id", "")
        if task_id:
            intent["task_id"] = task_id
        task_label = sel.get("label", "")
        if task_label:
            intent["task_label"] = task_label
        if task_state:
            intent["task_state"] = task_state

    task_state_flow = sel.get("state_flow", "")
    if task_state_flow:
        intent["task_state_flow"] = task_state_flow

    task_next_action = sel.get("next_action", "")
    if task_next_action:
        intent["task_next_action"] = task_next_action

    task_entry_actions = sel.get("entry_actions") or []
    if task_entry_actions:
        intent["task_entry_actions"] = task_entry_actions

    upstream_coordination_id = sel.get("upstream_coordination_id", "")
    if upstream_coordination_id:
        intent["upstream_coordination_id"] = upstream_coordination_id

    suggested_claim = sel.get("suggested_claim", "")
    if suggested_claim:
        intent["suggested_claim"] = suggested_claim

    routing_hint = sel.get("routing_hint", "")
    if routing_hint:
        intent["routing_hint"] = routing_hint

    scope = data.get("recommended_scope", "standard")
    selected_id = sel.get("id", "")
    category = sel.get("category", "")
    execution_category_out = execution_category or category
    selector_mode_out = data.get("selector_mode", "") or "-"
    intent_json = json.dumps(intent, separators=(",", ":"))

    return f"{scope} {selected_id} {category} {execution_category_out} {all_blocked} {selector_mode_out} {intent_json}"


def parse_cascade_intent(data: dict) -> dict[str, str]:
    """Extract routing fields from a CASCADE intent JSON object.

    Replaces 4 separate inline ``python3 -c`` blocks in autonomous-run.sh that
    each extract a single field from ``$CASCADE_INTENT``.  Centralising them
    makes the extraction testable and keeps the bash script below its ratchet cap.

    Returned keys:

    - ``task_id`` — task slug for ``CASCADE_SELECTED_ID`` back-fill (fanout path)
    - ``execution_category`` — category override carried by fanout workers
    - ``upstream_coordination_id`` — cross-repo claim key embedded in the intent
    - ``task_state`` — current task state for context / claim-gate injection

    Every value is a plain string; missing or falsy values are normalised to ``""``.
    """
    return {
        "task_id": str(data.get("task_id", "") or ""),
        "execution_category": str(data.get("execution_category", "") or ""),
        "upstream_coordination_id": str(data.get("upstream_coordination_id", "") or ""),
        "task_state": str(data.get("task_state", "") or ""),
    }


class AutonomousRun(BaseRunLoop):
    """Autonomous operation run loop.

    Implements the full autonomous workflow:
    - Three-step process (loose ends, selection, execution)
    - Work queue management
    - Preventive checks
    - Session validation
    """

    def __init__(
        self,
        workspace: Path,
        model: str | None = None,
        tool_format: str | None = None,
        executor: Executor | None = None,
    ):
        """Initialize autonomous run.

        Args:
            workspace: Path to workspace directory
            model: Model override (e.g. "openai-subscription/gpt-5.3-codex")
            tool_format: Tool format override (markdown/xml/tool)
            executor: Backend executor (default: GptmeExecutor)
        """
        super().__init__(
            workspace=workspace,
            run_type="autonomous",
            timeout=3000,  # 50 minutes
            lock_wait=False,  # Don't wait for lock
            model=model,
            tool_format=tool_format,
            executor=executor,
        )

    def generate_prompt(self) -> str:
        """Generate prompt for autonomous run.

        Returns:
            Full autonomous prompt
        """
        # Read prompt template from workspace
        template_file = self.workspace / "scripts/runs/autonomous/autonomous-prompt.txt"

        if template_file.exists():
            # Use existing template
            return template_file.read_text()

        # Get agent name from workspace config
        agent_name = get_agent_name(self.workspace)

        # Fallback: generate basic prompt
        return generate_base_prompt(
            run_type="autonomous",
            agent_name=agent_name,
            additional_sections="""
## Required Workflow

**Step 1**: Quick Loose Ends Check (2-5 min max)
- Check git status, critical notifications only
- Fix only immediate blockers

**Step 2**: Task Selection via CASCADE (5-10 min max)
1. **PRIMARY**: Read state/queue-manual.md "Planned Next" section
2. **SECONDARY**: Check notifications for direct assignments
3. **TERTIARY**: Check workspace tasks if PRIMARY/SECONDARY blocked

**Step 3**: EXECUTION (20-30 min - the main focus!)
- Make substantial progress on selected task
- Verify your work

Begin your autonomous work session now.
""",
        )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "is-capable-backend":
        backend = sys.argv[2] if len(sys.argv) > 2 else ""
        model = sys.argv[3] if len(sys.argv) > 3 else ""
        sys.exit(0 if is_capable_backend(backend, model) else 1)
    elif cmd == "self-review-cooldown":
        state = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/dev/null")
        hours = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
        # exit 0 = cooldown active (dispatcher should skip self-review)
        sys.exit(0 if self_review_cooldown_active(state, hours) else 1)
    elif cmd == "self-review-hours":
        state = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/dev/null")
        print(f"{self_review_hours_since_last(state):.1f}")
    elif cmd == "parse-cascade-json":
        # Read cascade-selector JSON from stdin, emit space-separated shell fields.
        # Usage: echo "$CASCADE_JSON" | python3 -m gptme_runloops.autonomous parse-cascade-json
        # Logic: parse_cascade_selector_output (tested)
        data = json.load(sys.stdin)
        print(parse_cascade_selector_output(data))
    elif cmd == "parse-cascade-intent":
        # Read CASCADE_INTENT JSON from stdin, emit 4 space-separated shell fields.
        # Usage: echo "$CASCADE_INTENT" | python3 -m gptme_runloops.autonomous parse-cascade-intent
        # Output: task_id execution_category upstream_coordination_id task_state
        # Logic: parse_cascade_intent (tested)
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        result = parse_cascade_intent(data)
        print(
            f"{result['task_id']} {result['execution_category']}"
            f" {result['upstream_coordination_id']} {result['task_state']}"
        )
    else:
        cmds = [
            "is-capable-backend BACKEND [MODEL]",
            "self-review-cooldown STATE_PATH [COOLDOWN_HOURS]",
            "self-review-hours STATE_PATH",
            "parse-cascade-json  (reads JSON from stdin)",
            "parse-cascade-intent  (reads JSON from stdin)",
        ]
        print(
            f"Usage: python3 -m gptme_runloops.autonomous [{' | '.join(c.split()[0] for c in cmds)}]",
            file=sys.stderr,
        )
        for c in cmds:
            print(f"  {c}", file=sys.stderr)
        sys.exit(2)
