"""
Ralph Loop plugin for gptme - iterative execution with context reset.

The Ralph Loop pattern (named after Ralph Wiggum) implements:
1. Give agent a spec + implementation plan
2. Agent works through plan step by step
3. After each step, reset context to just spec + updated plan
4. Progress persists in files/git, NOT in LLM context

This prevents context rot/degradation by keeping context fresh between iterations.

Supports both Claude Code and gptme as the inner execution backend.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gptme.tools.base import ToolSpec
else:
    try:
        from gptme.tools.base import ToolSpec
    except ImportError:
        ToolSpec = None

logger = logging.getLogger(__name__)


@dataclass
class LoopResult:
    """Result from a Ralph Loop execution."""

    spec_file: str
    plan_file: str
    total_steps: int
    completed_steps: int
    current_step: int
    duration_seconds: float
    backend: str
    status: str  # "running", "completed", "failed", "paused"
    session_id: str | None = None
    output: str = ""
    error: str | None = None


@dataclass
class PlanStep:
    """A single step in the implementation plan."""

    number: int
    description: str
    status: str = "pending"  # pending, in_progress, completed, failed
    notes: str = ""


@dataclass
class Plan:
    """The implementation plan with steps."""

    title: str
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 1

    @classmethod
    def from_markdown(cls, content: str) -> "Plan":
        """Parse a plan from markdown format."""
        lines = content.strip().split("\n")

        # Extract title (first heading)
        title = "Implementation Plan"
        for line in lines:
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break

        # Extract steps - look for numbered items or checkbox items
        steps = []
        step_pattern = re.compile(
            r"^(?:\s*)?(?:(?P<num>\d+)[\.\)]\s*|[-*]\s*\[(?P<check>[ xX])\]\s*)(?P<desc>.+)$"
        )

        for line in lines:
            match = step_pattern.match(line)
            if match:
                desc = match.group("desc").strip()
                check = match.group("check")

                # Determine status from checkbox
                status = "pending"
                if check:
                    status = "completed" if check.lower() == "x" else "pending"

                steps.append(
                    PlanStep(
                        number=len(steps) + 1,
                        description=desc,
                        status=status,
                    )
                )

        # Find current step (first non-completed)
        current_step = 1
        for i, step in enumerate(steps):
            if step.status != "completed":
                current_step = i + 1
                break
        else:
            current_step = len(steps) + 1  # All done

        plan = cls(title=title, steps=steps, current_step=current_step)
        return plan

    def to_markdown(self) -> str:
        """Convert plan back to markdown format."""
        lines = [f"# {self.title}", ""]

        for step in self.steps:
            check = "x" if step.status == "completed" else " "
            line = f"- [{check}] {step.description}"
            if step.notes:
                line += f" ({step.notes})"
            lines.append(line)

        return "\n".join(lines)

    def get_current_step(self) -> PlanStep | None:
        """Get the current step to work on."""
        for step in self.steps:
            if step.status == "pending":
                return step
        return None

    def mark_step_completed(self, step_num: int, notes: str = "") -> None:
        """Mark a step as completed."""
        if 1 <= step_num <= len(self.steps):
            self.steps[step_num - 1].status = "completed"
            if notes:
                self.steps[step_num - 1].notes = notes
            # Update current_step
            for i, step in enumerate(self.steps):
                if step.status != "completed":
                    self.current_step = i + 1
                    break
            else:
                self.current_step = len(self.steps) + 1


def _check_backend_available(backend: str) -> bool:
    """Check if the specified backend is available."""
    if backend == "claude":
        return shutil.which("claude") is not None
    elif backend == "gptme":
        return shutil.which("gptme") is not None
    return False


def _build_prompt(spec: str, plan: Plan, step: PlanStep, plan_file_path: str) -> str:
    """Build the prompt for a single loop iteration."""
    return f"""# Spec

{spec}

# Implementation Plan

File: `{plan_file_path}`

{plan.to_markdown()}

# Current Task

You are working on step {step.number}: {step.description}

## Instructions

1. Focus ONLY on completing step {step.number}
2. Make the minimal changes needed to complete this step
3. Test your changes if applicable
4. **CRITICAL**: When done, update `{plan_file_path}` to mark this step complete.
   Change the checkbox from `[ ]` to `[x]` for step {step.number}.
   Example using patch tool on {plan_file_path}:
   - ORIGINAL: `- [ ] {step.description}`
   - UPDATED: `- [x] {step.description}`
5. Do NOT work on other steps - stop after completing this one

## Important

- Progress is tracked by checkboxes in `{plan_file_path}` ([ ] = pending, [x] = done)
- You MUST update `{plan_file_path}` directly - DO NOT use internal todo/todowrite tools
- The loop detects completion by reading checkboxes from the file
- After you complete this step, the loop will continue with fresh context
- Make sure to save/commit your work before the step ends
"""


def _run_iteration(
    prompt: str,
    work_dir: Path,
    backend: str,
    timeout: int,
    plan_file: Path,
) -> tuple[bool, str]:
    """Run a single iteration of the loop.

    Returns (success, output).
    """
    if not _check_backend_available(backend):
        return False, f"Error: {backend} CLI not found"

    if backend == "claude":
        # Use --tools default to enable tool execution in print mode
        # Use --dangerously-skip-permissions for non-interactive execution
        # Note: claude -p requires input via stdin, not as positional argument
        cmd = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--tools",
            "default",
        ]
        stdin_input = prompt
    else:
        # gptme in non-interactive mode
        cmd = ["gptme", "-n", prompt]
        stdin_input = None

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_input,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n\nSTDERR:\n{result.stderr}"

        success = result.returncode == 0

        return success, output

    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"


def run_loop(
    spec_file: str,
    plan_file: str,
    workspace: str | None = None,
    backend: str = "gptme",
    max_iterations: int = 50,
    step_timeout: int = 600,
    background: bool = False,
) -> LoopResult | str:
    """
    Run a Ralph Loop - iterative execution with context reset.

    The loop reads a spec and plan, executes one step at a time,
    updates the plan, and resets context between steps. Progress
    persists in files, not in LLM context.

    Args:
        spec_file: Path to the spec/PRD file
        plan_file: Path to the implementation plan (markdown with checkboxes)
        workspace: Working directory (defaults to current)
        backend: "claude" or "gptme" for inner loop execution
        max_iterations: Maximum number of loop iterations (default: 50)
        step_timeout: Timeout per step in seconds (default: 10 minutes)
        background: If True, run in tmux and return session ID

    Returns:
        LoopResult with execution details, or session ID if background=True

    Examples:
        # Basic loop with spec and plan files
        run_loop("spec.md", "plan.md")

        # Use gptme instead of claude
        run_loop("spec.md", "plan.md", backend="gptme")

        # Long-running in background
        run_loop("spec.md", "plan.md", background=True, max_iterations=100)
    """
    work_dir = Path(workspace) if workspace else Path.cwd()
    spec_path = work_dir / spec_file
    plan_path = work_dir / plan_file

    # Validate files exist
    if not spec_path.exists():
        return LoopResult(
            spec_file=spec_file,
            plan_file=plan_file,
            total_steps=0,
            completed_steps=0,
            current_step=0,
            duration_seconds=0,
            backend=backend,
            status="failed",
            error=f"Spec file not found: {spec_path}",
        )

    if not plan_path.exists():
        return LoopResult(
            spec_file=spec_file,
            plan_file=plan_file,
            total_steps=0,
            completed_steps=0,
            current_step=0,
            duration_seconds=0,
            backend=backend,
            status="failed",
            error=f"Plan file not found: {plan_path}",
        )

    if background:
        return _run_background_loop(
            spec_file=spec_file,
            plan_file=plan_file,
            work_dir=work_dir,
            backend=backend,
            max_iterations=max_iterations,
            step_timeout=step_timeout,
        )

    # Run synchronous loop
    return _run_sync_loop(
        spec_path=spec_path,
        plan_path=plan_path,
        work_dir=work_dir,
        backend=backend,
        max_iterations=max_iterations,
        step_timeout=step_timeout,
    )


def _run_sync_loop(
    spec_path: Path,
    plan_path: Path,
    work_dir: Path,
    backend: str,
    max_iterations: int,
    step_timeout: int,
) -> LoopResult:
    """Run the loop synchronously."""
    start_time = time.time()

    # Read spec
    spec = spec_path.read_text()

    # Read and parse plan
    plan_content = plan_path.read_text()
    plan = Plan.from_markdown(plan_content)

    total_steps = len(plan.steps)
    outputs = []

    for iteration in range(max_iterations):
        # Get current step
        step = plan.get_current_step()
        if step is None:
            # All steps completed
            break

        logger.info(f"Ralph Loop iteration {iteration + 1}: Step {step.number}")

        # Build prompt for this iteration
        prompt = _build_prompt(spec, plan, step, plan_file_path=str(plan_path))

        # Run the iteration
        success, output = _run_iteration(
            prompt=prompt,
            work_dir=work_dir,
            backend=backend,
            timeout=step_timeout,
            plan_file=plan_path,
        )

        outputs.append(f"=== Step {step.number} ===\n{output}")

        # Re-read plan to check if step was marked complete
        # (the agent should update the plan file)
        plan_content = plan_path.read_text()
        plan = Plan.from_markdown(plan_content)

        if not success:
            return LoopResult(
                spec_file=str(spec_path),
                plan_file=str(plan_path),
                total_steps=total_steps,
                completed_steps=sum(1 for s in plan.steps if s.status == "completed"),
                current_step=step.number,
                duration_seconds=time.time() - start_time,
                backend=backend,
                status="failed",
                output="\n\n".join(outputs),
                error=f"Step {step.number} failed",
            )

    completed_after = sum(1 for s in plan.steps if s.status == "completed")

    status = "completed" if completed_after == total_steps else "paused"

    return LoopResult(
        spec_file=str(spec_path),
        plan_file=str(plan_path),
        total_steps=total_steps,
        completed_steps=completed_after,
        current_step=plan.current_step,
        duration_seconds=time.time() - start_time,
        backend=backend,
        status=status,
        output="\n\n".join(outputs),
    )


def _run_background_loop(
    spec_file: str,
    plan_file: str,
    work_dir: Path,
    backend: str,
    max_iterations: int,
    step_timeout: int,
) -> str:
    """Run the loop in a background tmux session."""
    # Check if tmux is available
    if shutil.which("tmux") is None:
        return (
            "Error: tmux is not installed or not in PATH.\n"
            "Please install tmux to use background loop execution.\n"
            "On Ubuntu/Debian: sudo apt install tmux\n"
            "On macOS: brew install tmux"
        )

    session_id = f"ralph_loop_{uuid.uuid4().hex[:8]}"

    # Create a wrapper script that runs the loop
    # We'll use a simple bash loop that calls the backend repeatedly
    script = f"""#!/bin/bash
cd {shlex.quote(str(work_dir))}
SPEC_FILE="{spec_file}"
PLAN_FILE="{plan_file}"
BACKEND="{backend}"
MAX_ITER={max_iterations}
TIMEOUT={step_timeout}

for i in $(seq 1 $MAX_ITER); do
    echo "=== Ralph Loop iteration $i ==="

    # Check if all steps are done (no unchecked boxes)
    if ! grep -q '\\[ \\]' "$PLAN_FILE" 2>/dev/null; then
        echo "All steps completed!"
        break
    fi

    # Run the agent with spec + plan as context
    if [ "$BACKEND" = "claude" ]; then
        # Use --dangerously-skip-permissions and --tools default for non-interactive execution
        # Note: claude -p requires input via stdin, not as positional argument
        cat <<PROMPT_EOF | timeout $TIMEOUT claude -p --dangerously-skip-permissions --tools default
$(cat $SPEC_FILE)

Plan file: $PLAN_FILE

$(cat $PLAN_FILE)

TASK: Complete the next unchecked step ([ ]) in the plan above.

CRITICAL: After completing the step, you MUST update $PLAN_FILE directly:
- Change the checkbox from [ ] to [x] for the completed step
- Use patch or save tool on $PLAN_FILE - DO NOT use internal todo tools
- The loop detects completion by reading checkboxes from the file
PROMPT_EOF
    else
        timeout $TIMEOUT gptme -n "$(cat $SPEC_FILE)

Plan file: $PLAN_FILE

$(cat $PLAN_FILE)

TASK: Complete the next unchecked step ([ ]) in the plan above.

CRITICAL: After completing the step, you MUST update $PLAN_FILE directly:
- Change the checkbox from [ ] to [x] for the completed step
- Use patch or save tool on $PLAN_FILE - DO NOT use internal todo tools
- The loop detects completion by reading checkboxes from the file"
    fi

    echo "=== Iteration $i complete ==="
    sleep 2
done

echo "Ralph Loop finished"
"""

    # Write the script to a temp file
    script_path = work_dir / f".ralph_loop_{session_id}.sh"
    script_path.write_text(script)
    os.chmod(script_path, 0o755)

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_id, str(script_path)],
            check=True,
        )
        return (
            f"Background Ralph Loop started in session: {session_id}\n\n"
            f"Monitor with: check_loop('{session_id}')\n"
            f"Stop with: stop_loop('{session_id}')\n\n"
            f"Progress is tracked in: {plan_file}"
        )
    except subprocess.CalledProcessError as e:
        return f"Error starting background loop: {e}"


def check_loop(session_id: str) -> str:
    """
    Check status and output of a background Ralph Loop session.

    Args:
        session_id: The session ID returned by run_loop(background=True)

    Returns:
        Current output from the session and progress status
    """
    try:
        # Check if session exists
        has_result = subprocess.run(
            ["tmux", "has-session", "-t", session_id],
            capture_output=True,
        )

        if has_result.returncode != 0:
            return f"Session {session_id} not found (may have completed)"

        # Capture current output
        capture_result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_id],
            capture_output=True,
            text=True,
        )
        return capture_result.stdout
    except Exception as e:
        return f"Error checking session: {e}"


def stop_loop(session_id: str) -> str:
    """
    Stop a background Ralph Loop session.

    Args:
        session_id: The session ID to stop

    Returns:
        Status message
    """
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_id],
            check=True,
        )
        return f"Session {session_id} stopped"
    except subprocess.CalledProcessError:
        return f"Session {session_id} not found"


def create_plan(
    task_description: str,
    output_file: str = "plan.md",
    num_steps: int = 5,
    workspace: str | None = None,
) -> str:
    """
    Create an initial implementation plan from a task description.

    This is a helper to bootstrap a Ralph Loop by generating a plan file.

    Args:
        task_description: Description of what to implement
        output_file: Where to save the plan (default: plan.md)
        num_steps: Approximate number of steps to generate (default: 5)
        workspace: Working directory (defaults to current)

    Returns:
        Path to the created plan file

    Example:
        create_plan("Implement a REST API with CRUD operations for users")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()
    output_path = work_dir / output_file

    # Simple template - the agent should refine this
    plan_template = f"""# Implementation Plan

Task: {task_description}

## Steps

- [ ] Step 1: Analyze requirements and design approach
- [ ] Step 2: Set up initial structure
- [ ] Step 3: Implement core functionality
- [ ] Step 4: Add tests
- [ ] Step 5: Documentation and cleanup

## Notes

- Each step should be atomic and verifiable
- Update this file to mark steps complete as you work
- Add more detailed sub-steps if needed
"""

    output_path.write_text(plan_template)
    return f"Plan created at: {output_path}\n\nEdit the plan to add specific steps, then run the loop."


def create_spec(
    task_description: str,
    output_file: str = "spec.md",
    workspace: str | None = None,
) -> str:
    """
    Create a spec/PRD file from a task description.

    This is a helper to bootstrap a Ralph Loop by generating a spec file.

    Args:
        task_description: Description of what to implement
        output_file: Where to save the spec (default: spec.md)
        workspace: Working directory (defaults to current)

    Returns:
        Path to the created spec file

    Example:
        create_spec("Build a REST API with CRUD operations for users")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()
    output_path = work_dir / output_file

    spec_template = f"""# Project Specification

## Overview

{task_description}

## Requirements

- Implement the functionality described above
- Follow best practices for the domain
- Write clean, maintainable code
- Include appropriate error handling

## Acceptance Criteria

- All specified functionality works correctly
- Code is well-structured and documented
- Tests pass (if applicable)

## Notes

- This spec was auto-generated from the task description
- Add more detailed requirements as needed
- Reference this file during implementation
"""

    output_path.write_text(spec_template)
    return f"Spec created at: {output_path}"


def create_project(
    task_description: str,
    workspace: str | None = None,
    use_llm: bool = True,
    backend: str = "gptme",
    model: str | None = None,
) -> tuple[str, str]:
    """
    Bootstrap a Ralph Loop project with both spec and plan files.

    This is the recommended way to start a new Ralph Loop project.
    It creates both a spec file and a task-specific plan in one call.

    Args:
        task_description: Description of what to implement
        workspace: Working directory (defaults to current)
        use_llm: If True, use LLM to generate task-specific plan steps (default: True)
        backend: Backend for LLM plan generation ('gptme' or 'claude')
        model: Model to use for plan generation (defaults to backend's default)

    Returns:
        Tuple of (spec_path, plan_path)

    Example:
        spec, plan = create_project("Build a REST API with authentication")
        run_loop(spec, plan)
    """
    work_dir = Path(workspace) if workspace else Path.cwd()

    # Create spec file
    spec_path = work_dir / "spec.md"
    create_spec(task_description, "spec.md", workspace)

    # Create plan file
    plan_path = work_dir / "plan.md"

    if use_llm:
        # Generate task-specific plan using LLM
        plan_content = _generate_plan_with_llm(
            task_description, backend, model, work_dir
        )
        if plan_content:
            plan_path.write_text(plan_content)
        else:
            # Fall back to template if LLM generation fails
            create_plan(task_description, "plan.md", 5, workspace)
    else:
        # Use template-based plan
        create_plan(task_description, "plan.md", 5, workspace)

    return (str(spec_path), str(plan_path))


def _generate_plan_with_llm(
    task_description: str,
    backend: str,
    model: str | None,
    work_dir: Path,
) -> str | None:
    """
    Generate a task-specific implementation plan using an LLM.

    Args:
        task_description: What to implement
        backend: 'gptme' or 'claude'
        model: Model to use (or None for default)
        work_dir: Working directory

    Returns:
        Plan content as markdown string, or None if generation fails
    """
    prompt = f"""Generate an implementation plan for the following task:

{task_description}

Create a plan with 3-7 specific, actionable steps. Each step should be:
- Concrete and verifiable
- Achievable in a focused work session
- Building towards the complete solution

Format the plan as a markdown file with:
1. A title heading
2. Checkbox items for each step (using - [ ] format)
3. Brief notes section if needed

Example format:
# Implementation Plan: [Brief Title]

- [ ] Step 1: [Specific action]
- [ ] Step 2: [Specific action]
- [ ] Step 3: [Specific action]

## Notes
- [Any important considerations]

Output ONLY the plan content, nothing else."""

    try:
        if backend == "gptme":
            return _generate_plan_gptme(prompt, model, work_dir)
        elif backend == "claude":
            return _generate_plan_claude(prompt, model, work_dir)
        else:
            logger.warning(f"Unknown backend {backend}, falling back to template")
            return None
    except Exception as e:
        logger.warning(f"Failed to generate plan with LLM: {e}")
        return None


def _generate_plan_gptme(
    prompt: str,
    model: str | None,
    work_dir: Path,
) -> str | None:
    """Generate plan using gptme backend."""
    cmd = ["gptme", "-n", "-y"]

    if model:
        cmd.extend(["-m", model])

    # Create temporary conversation for plan generation
    temp_name = f"ralph-plan-gen-{uuid.uuid4().hex[:8]}"
    cmd.extend(["--name", temp_name])

    # Add the prompt
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minute timeout for plan generation
        )

        if result.returncode == 0:
            # Extract plan from output - look for markdown plan format
            output = result.stdout
            plan = _extract_plan_from_output(output)
            if plan:
                return plan

        logger.warning(f"gptme plan generation returned: {result.returncode}")
        return None

    except subprocess.TimeoutExpired:
        logger.warning("gptme plan generation timed out")
        return None
    except Exception as e:
        logger.warning(f"gptme plan generation failed: {e}")
        return None


def _generate_plan_claude(
    prompt: str,
    model: str | None,
    work_dir: Path,
) -> str | None:
    """Generate plan using claude backend."""
    # Check for Claude CLI
    if not shutil.which("claude"):
        logger.warning("Claude CLI not found for plan generation")
        return None

    cmd = ["claude", "-p"]
    if model:
        cmd.extend(["-m", model])

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            # Claude output should be the plan directly
            plan = _extract_plan_from_output(result.stdout)
            if plan:
                return plan

        logger.warning(f"Claude plan generation returned: {result.returncode}")
        return None

    except subprocess.TimeoutExpired:
        logger.warning("Claude plan generation timed out")
        return None
    except Exception as e:
        logger.warning(f"Claude plan generation failed: {e}")
        return None


def _extract_plan_from_output(output: str) -> str | None:
    """
    Extract plan content from LLM output.

    Looks for markdown plan format with checkboxes.
    Filters out gptme subprocess noise (token counts, context loading, etc).
    """
    lines = output.strip().split("\n")

    # Filter out gptme-specific noise patterns
    def is_noise_line(line: str) -> bool:
        """Check if line is gptme subprocess noise."""
        stripped = line.strip()

        # Token/cost information
        if stripped.startswith("Tokens:") or stripped.startswith("Cost:"):
            return True
        if "tokens" in stripped.lower() and any(
            x in stripped for x in ["input", "output", "total", "cached"]
        ):
            return True

        # Context loading messages (specific gptme patterns, not generic)
        if stripped.startswith("Loading context") or stripped.startswith("Loaded "):
            return True
        if "context" in stripped.lower() and "loading" in stripped.lower():
            return True

        # System/tool markers
        if stripped.startswith("System:") or stripped.startswith("Tool:"):
            return True
        if stripped.startswith(">>>") or stripped.startswith("<<<"):
            return True

        # Shell prompt patterns
        if stripped.startswith("$ ") or stripped.startswith("bob@"):
            return True

        # gptme-specific markers
        if "gptme" in stripped.lower() and any(
            x in stripped.lower() for x in ["version", "model", "starting", "session"]
        ):
            return True

        return False

    # Find the start of the plan - look for plan-specific heading patterns
    plan_start = None
    for i, line in enumerate(lines):
        if is_noise_line(line):
            continue

        stripped = line.strip()

        # Strong plan markers - prefer these
        if stripped.startswith("# Implementation Plan"):
            plan_start = i
            break
        if stripped.startswith("# Plan:") or stripped.startswith("# Plan "):
            plan_start = i
            break

        # Generic plan heading (must be h1 with plan-related word)
        if stripped.startswith("# ") and any(
            word in stripped.lower()
            for word in ["plan", "steps", "implementation", "tasks"]
        ):
            plan_start = i
            break

        # Fallback: first checkbox line (but only if we haven't found heading)
        if plan_start is None and stripped.startswith("- [ ]"):
            plan_start = i
            # Don't break - keep looking for a proper heading

    if plan_start is None:
        return None

    # Collect lines until we hit something that's clearly not part of the plan
    plan_lines = []
    consecutive_empty = 0

    for idx, line in enumerate(lines[plan_start:], start=plan_start):
        # Skip noise lines
        if is_noise_line(line):
            continue

        stripped = line.strip()

        # Stop on conversation markers (gptme output format)
        if stripped.startswith("Human:") or stripped.startswith("Assistant:"):
            break
        if stripped.startswith("User:") or stripped.startswith("System:"):
            break

        # Stop on code blocks that appear after plan content
        if stripped.startswith("```") and plan_lines:
            # Check if this is a code block inside the plan (notes section)
            # or end of plan content
            remaining = "\n".join(lines[idx:])
            if "```" in remaining[3:50]:  # Closing fence nearby
                # Small code block - might be part of notes, skip it
                continue
            break

        # Stop on obvious end markers
        if stripped.startswith("---") and len(stripped) >= 3 and plan_lines:
            break

        # Track empty lines - too many in a row suggests end of plan
        if stripped == "":
            consecutive_empty += 1
            if consecutive_empty > 3:
                break
        else:
            consecutive_empty = 0

        plan_lines.append(line)

    if not plan_lines:
        return None

    plan_content = "\n".join(plan_lines).strip()

    # Validate it looks like a plan (has checkboxes)
    if "- [ ]" not in plan_content and "- [x]" not in plan_content:
        return None

    return plan_content


# Tool specification for gptme
if ToolSpec is not None:
    tool = ToolSpec(
        name="ralph_loop",
        desc="Iterative execution loops with context reset (Ralph Loop pattern)",
        instructions="""
Ralph Loop plugin for iterative execution with fresh context.

**The Pattern:**
1. Spec + Plan: Define what to build and steps to complete
2. Loop: Execute one step at a time
3. Reset: Fresh context each iteration (just spec + updated plan)
4. Persist: Progress tracked in plan file, not LLM context

**Key Benefit:** Prevents context rot/degradation on long tasks.

**Available Functions:**

1. **run_loop(spec_file, plan_file)** - Start the iterative loop
2. **check_loop(session_id)** - Check background loop progress
3. **stop_loop(session_id)** - Stop a background loop
4. **create_plan(task)** - Generate initial plan file (generic template)
5. **create_spec(task)** - Generate spec/PRD file from task description
6. **create_project(task)** - Bootstrap project with spec + LLM-generated plan (recommended)

**When to use Ralph Loop:**
- Multi-step implementation tasks
- Tasks prone to context degradation
- Long-running autonomous work
- Projects with clear step-by-step plans

**Examples:**

```python
# Recommended: Use create_project for full setup with LLM-generated plan
spec, plan = create_project("Build a REST API with authentication")
run_loop(spec, plan)

# Or create files separately
create_spec("Build a REST API with authentication")
create_plan("Build a REST API with authentication")  # generic template

# Run the loop (foreground)
run_loop("spec.md", "plan.md")

# Run in background for long tasks
run_loop("spec.md", "plan.md", background=True, max_iterations=20)

# Check progress
check_loop("ralph_loop_abc123")
```
        """,
        examples="""
### Create and Run a Loop (Recommended)

> User: I need to implement a user authentication system
> Assistant: I'll bootstrap the project with spec and LLM-generated plan.
```ipython
spec, plan = create_project("Implement user authentication with JWT tokens")
```
> System: Spec created at: spec.md
> System: ('spec.md', 'plan.md')
> Assistant: Project bootstrapped. Now let me run the loop.
```ipython
run_loop(spec, plan, backend="gptme")
```

### Create and Run a Loop (Manual)

> User: I need to implement a user authentication system
> Assistant: I'll create a plan and start the Ralph Loop.
```ipython
create_plan("Implement user authentication with JWT tokens", "auth_plan.md")
```
> System: Plan created at: auth_plan.md
> Assistant: Plan created. Now let me edit it with specific steps and start the loop.
```ipython
run_loop("spec.md", "auth_plan.md", backend="claude")
```

### Background Loop for Long Tasks

> User: This is a big refactoring task, run it in background
> Assistant: I'll start a background loop so it can run for a while.
```ipython
run_loop("refactor_spec.md", "refactor_plan.md", background=True, max_iterations=30)
```
> System: Background Ralph Loop started in session: ralph_loop_a1b2c3d4

### Check Loop Progress

> User: How's the refactoring going?
> Assistant: Let me check the background loop.
```ipython
check_loop("ralph_loop_a1b2c3d4")
```
        """,
        functions=[
            run_loop,
            check_loop,
            stop_loop,
            create_plan,
            create_spec,
            create_project,
        ],
    )
else:
    tool = None  # type: ignore
