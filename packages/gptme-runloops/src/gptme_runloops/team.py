"""Autonomous team run loop implementation.

Implements the "coordinator-only" pattern inspired by Claude Code Agent Teams.
The top-level agent runs with restricted tools (gptodo + save) and must
delegate all work to subagents.

This forces:
- Context hygiene: each subagent has fresh context
- Task decomposition: coordinator must break down work
- Parallel execution: multiple subagents can work simultaneously
"""

from pathlib import Path

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.execution import ExecutionResult, execute_gptme
from gptme_runloops.utils.prompt import get_agent_name


TEAM_PROMPT_TEMPLATE = """\
You are {agent_name}, running in **autonomous-team coordinator mode**.

**Current Time**: {{timestamp}}

## Your Role: COORDINATOR

You are a **coordinator agent** with RESTRICTED tools. You CANNOT directly:
- Run shell commands
- Edit code files
- Execute tests

You CAN:
- **Delegate work** to subagents via `delegate()` function
- **Save files** (journal entries, documentation, work queue updates)
- **Check progress** of spawned agents
- **Synthesize results** from completed agents

## Required Workflow

### Phase 1: Assess Work (3-5 min)
- Review the work queue and notifications
- Identify tasks that can be parallelized
- Plan which subtasks to delegate

### Phase 2: Delegate (5-10 min)
- Spawn focused subagents for each subtask
- Write clear, specific prompts with:
  - Exact file paths to modify
  - Success criteria
  - Context needed (issue numbers, PR links)
- Use `background=True` for parallel work
- Use `background=False` for sequential dependencies

### Phase 3: Monitor & Synthesize (10-15 min)
- Check agent progress with `check_agent()`
- Wait for critical agents to complete
- Synthesize results and update work queue
- Write comprehensive session journal

### Phase 4: Complete (3-5 min)
- Save journal entry with all agent outcomes
- Update work queue with next priorities
- Commit and push changes

## Delegation Best Practices

1. **Be specific**: "Fix test_auth.py line 42 mock" not "fix tests"
2. **Include context**: Issue numbers, PR links, error messages
3. **Set timeouts**: Short tasks 300s, medium 600s, long 1200s
4. **Verify results**: Always check_agent() before declaring success

Begin coordinating work now.
"""


class TeamRun(BaseRunLoop):
    """Autonomous team operation run loop.

    Runs the coordinator agent with restricted tools (gptodo + save),
    forcing all work to be delegated to subagents.
    """

    # Tools available to the coordinator
    COORDINATOR_TOOLS = "gptodo,save,append,read,todoread,todowrite,complete"

    def __init__(self, workspace: Path, tools: str | None = None):
        """Initialize team run.

        Args:
            workspace: Path to workspace directory
            tools: Override coordinator tools (default: gptodo,save,append,...)
        """
        super().__init__(
            workspace=workspace,
            run_type="team",
            timeout=3000,  # 50 minutes
            lock_wait=False,
        )
        self.tools = tools or self.COORDINATOR_TOOLS

    def generate_prompt(self) -> str:
        """Generate prompt for team coordinator run."""
        agent_name = get_agent_name(self.workspace)

        # Check for custom template
        template_file = self.workspace / "scripts/runs/team/team-prompt.txt"
        if template_file.exists():
            return template_file.read_text()

        # Use built-in template
        return TEAM_PROMPT_TEMPLATE.format(agent_name=agent_name)

    def execute(self, prompt: str) -> ExecutionResult:
        """Execute gptme with restricted tools for coordinator mode."""
        self.logger.info(
            f"Starting team coordinator (timeout: {self.timeout}s, "
            f"tools: {self.tools})"
        )

        result = execute_gptme(
            prompt=prompt,
            workspace=self.workspace,
            timeout=self.timeout,
            non_interactive=True,
            run_type=self.run_type,
            tools=self.tools,
        )

        if result.timed_out:
            self.logger.warning(f"Coordinator timed out after {self.timeout}s")
        elif result.success:
            self.logger.info("Team coordination completed successfully")
        else:
            self.logger.error(f"Coordinator failed with exit code {result.exit_code}")

        return result
