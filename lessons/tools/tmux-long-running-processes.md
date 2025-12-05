---
match:
  keywords:
  - tmux
  - long-running
  - shell timeout
  - benchmark
  - timeout
---

# Tmux for Long-Running Processes

## Rule
Always use tmux (not shell tool) for processes exceeding 120-second shell timeout.

## Context
When running commands that need more than 2 minutes to complete, such as benchmarks, optimization runs, builds, or data processing.

## Detection
Observable signals that tmux is needed:
- Command expected to run 5+ minutes
- Process previously timed out at 120 seconds
- Background execution needed (survives shell exit)
- Need to monitor progress over time
- Long-running evaluation or benchmark suites

Common examples:
- Optimization runs (30-60 minutes)
- Full benchmark suites (10-30 minutes)
- Large data processing (variable time)
- Model training or evaluation (minutes to hours)

## Pattern
Use tmux with proper verification:
```bash
# Start long-running process in tmux
tmux new-session 'cd /path/to/project && command with args'

# Monitor progress periodically
tmux inspect-pane session_name

# After completion, retrieve results
tmux inspect-pane session_name
# Process results...

# Clean up when done
tmux kill-session session_name
```

**Anti-pattern**: Using shell tool for long processes
```shell
# Wrong: shell timeout kills process at 120s
poetry run python -m long_benchmark --train-size 20 ...
# Process starts, reaches 120s, gets killed

# Correct: tmux persists beyond timeout
tmux new-session 'poetry run python -m long_benchmark --train-size 20 ...'
# Process runs to completion (30-60 min)
```

## Outcome
Following this pattern ensures:
- **Process persistence**: Runs complete despite shell timeouts
- **Progress monitoring**: Can check status with inspect-pane
- **Result preservation**: Output captured when complete
- **No wasted work**: Process completes without interruption

Benefits:
- Benchmarks complete (30-60 min vs 120s timeout)
- Full evaluation suites finish successfully
- No need to restart failed processes
- Results available when ready
