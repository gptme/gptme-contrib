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
    else:
        cmds = [
            "is-capable-backend BACKEND [MODEL]",
            "self-review-cooldown STATE_PATH [COOLDOWN_HOURS]",
            "self-review-hours STATE_PATH",
        ]
        print(
            f"Usage: python3 -m gptme_runloops.autonomous [{' | '.join(c.split()[0] for c in cmds)}]",
            file=sys.stderr,
        )
        for c in cmds:
            print(f"  {c}", file=sys.stderr)
        sys.exit(2)
