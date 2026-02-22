"""Autonomous run loop implementation."""

from pathlib import Path

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.prompt import generate_base_prompt, get_agent_name


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
    ):
        """Initialize autonomous run.

        Args:
            workspace: Path to workspace directory
            model: Model override (e.g. "openai-subscription/gpt-5.3-codex")
            tool_format: Tool format override (markdown/xml/tool)
        """
        super().__init__(
            workspace=workspace,
            run_type="autonomous",
            timeout=3000,  # 50 minutes
            lock_wait=False,  # Don't wait for lock
            model=model,
            tool_format=tool_format,
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
