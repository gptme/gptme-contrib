"""Autonomous run loop implementation."""

import os
from pathlib import Path

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.prompt import generate_base_prompt


class AutonomousRun(BaseRunLoop):
    """Autonomous operation run loop.

    Implements the full autonomous workflow:
    - Three-step process (loose ends, selection, execution)
    - Work queue management
    - Preventive checks
    - Session validation
    """

    def __init__(self, workspace: Path):
        """Initialize autonomous run.

        Args:
            workspace: Path to workspace directory
        """
        super().__init__(
            workspace=workspace,
            run_type="autonomous",
            timeout=3000,  # 50 minutes
            lock_wait=False,  # Don't wait for lock
        )

    def _get_friction_section(self) -> str:
        """Get friction analysis section if triggered.

        Returns:
            Friction analysis guidance if BOB_FRICTION_TRIGGER is set,
            empty string otherwise.
        """
        if os.environ.get("BOB_FRICTION_TRIGGER") != "true":
            return ""

        return """
## 🔴 FRICTION ANALYSIS TRIGGERED

**Session threshold reached** - Run friction analysis before normal work.

**Required Action (Step 0)**:
```shell
# Run friction analysis on recent sessions
uv run python -m metaproductivity.friction --last-n-sessions 20 --with-alerts --format summary
```

**Then**:
1. Review the friction report output
2. If actionable patterns found → create GitHub issue or address immediately
3. Record friction analysis in session counter:
   ```shell
   uv run python -m metaproductivity.session_counter --record-friction
   ```
4. Continue with normal workflow (Steps 1-3)

**Purpose**: Identify and address autonomous operation blockers before they compound.
"""

    def generate_prompt(self) -> str:
        """Generate prompt for autonomous run.

        Returns:
            Full autonomous prompt
        """
        # Read prompt template from workspace
        template_file = self.workspace / "scripts/runs/autonomous/autonomous-prompt.txt"

        if template_file.exists():
            # Use existing template, append friction section if triggered
            template = template_file.read_text()
            friction_section = self._get_friction_section()
            if friction_section:
                # Insert friction section before the main workflow
                return template + "\n" + friction_section
            return template

        # Get friction section if triggered
        friction_section = self._get_friction_section()

        # Fallback: generate basic prompt
        return generate_base_prompt(
            run_type="autonomous",
            additional_sections=f"""{friction_section}
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
