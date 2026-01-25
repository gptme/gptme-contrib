# gptme-ralph-loop

A gptme plugin implementing the **Ralph Loop** pattern - iterative execution with context reset between steps.

## Overview

The Ralph Loop pattern (named after Ralph Wiggum from a popular AI coding video) solves the problem of **context rot/degradation** in long-running agent tasks by:

1. **Spec + Plan**: Give the agent a specification and implementation plan
2. **Iterative Execution**: Work through the plan step by step
3. **Context Reset**: After each step, reset context to just spec + updated plan
4. **File Persistence**: Progress persists in files/git, NOT in LLM context

This keeps the agent's context fresh and focused, preventing the quality degradation that happens when context fills up with old information.

## Installation

```bash
# Add to your gptme.toml
[plugins]
enabled = ["gptme_ralph_loop"]
paths = ["path/to/gptme-contrib/plugins/ralph-loop/src"]
```

## Usage

### Basic Loop

```python
from gptme_ralph_loop import run_loop, create_plan

# Create a plan file
create_plan("Build a REST API with user authentication", "api_plan.md")

# Edit the plan to add specific steps, then run
run_loop("spec.md", "api_plan.md")
```

### Plan File Format

Plans use markdown with checkboxes:

```markdown
# Implementation Plan

Task: Build REST API with authentication

## Steps

- [ ] Step 1: Set up FastAPI project structure
- [ ] Step 2: Implement user model and database
- [ ] Step 3: Add JWT authentication
- [x] Step 4: Create CRUD endpoints (completed)
- [ ] Step 5: Add tests
```

### Background Execution

For long-running tasks:

```python
# Start in background
run_loop("spec.md", "plan.md", background=True, max_iterations=30)
# Returns: "Background Ralph Loop started in session: ralph_loop_abc123"

# Check progress
check_loop("ralph_loop_abc123")

# Stop if needed
stop_loop("ralph_loop_abc123")
```

### Backend Selection

Supports both Claude Code and gptme as the inner loop:

```python
# Use Claude Code (default)
run_loop("spec.md", "plan.md", backend="claude")

# Use gptme
run_loop("spec.md", "plan.md", backend="gptme")
```

## API Reference

### `run_loop(spec_file, plan_file, **kwargs)`

Run a Ralph Loop with the given spec and plan.

**Parameters:**
- `spec_file`: Path to the specification/PRD file
- `plan_file`: Path to the implementation plan (markdown with checkboxes)
- `workspace`: Working directory (default: current)
- `backend`: "claude" or "gptme" (default: "claude")
- `max_iterations`: Maximum loop iterations (default: 50)
- `step_timeout`: Timeout per step in seconds (default: 600)
- `background`: Run in tmux session (default: False)

### `create_plan(task_description, output_file, **kwargs)`

Create an initial implementation plan from a task description.

**Parameters:**
- `task_description`: What to implement
- `output_file`: Where to save the plan (default: "plan.md")
- `num_steps`: Approximate number of steps (default: 5)
- `workspace`: Working directory (default: current)

### `check_loop(session_id)`

Check status of a background loop session.

### `stop_loop(session_id)`

Stop a background loop session.

## How It Works

1. **Read Spec & Plan**: Load the specification and current plan state
2. **Find Current Step**: Identify the first uncompleted step
3. **Build Prompt**: Create a focused prompt with spec + plan + current step
4. **Execute**: Run the backend (Claude/gptme) with the prompt
5. **Check Completion**: Re-read plan to see if step was marked complete
6. **Loop or Exit**: Continue to next step or exit if all done

The key insight is that each iteration gets a **fresh context** containing only:
- The original specification
- The current state of the plan
- Instructions for the current step

This prevents the context from filling up with old tool outputs, failed attempts, and other noise.

## When to Use

**Good fit:**
- Multi-step implementation tasks
- Long-running autonomous work
- Tasks with clear step-by-step plans
- Projects prone to context degradation

**Not ideal for:**
- Quick one-off tasks
- Highly interactive debugging
- Tasks requiring continuous context

## References

- [Ralph Wiggum Loops YouTube Video](https://youtu.be/I7azCAgoUHc)
- [11 Tips for AI Coding](https://www.reddit.com/r/ClaudeAI/comments/ralph_loops)
- Related: [gptme LLM resume/compact feature](https://gptme.org/docs/)

## License

MIT
