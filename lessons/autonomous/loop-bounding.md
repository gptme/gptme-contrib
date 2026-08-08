---
description: "Bound every autonomous loop with iteration caps, wall-clock timeouts, and stall detection before it runs"
match:
  keywords:
  - "autonomous loop"
  - "run loop"
  - "long-running agent"
  - "infinite loop"
status: active
---
# Bound Every Autonomous Loop Before It Runs
## Rule
Every autonomous run loop must have a hard iteration cap and a wall-clock timeout, checked before the loop starts, not just monitored after it runs.
## Context
Applies whenever an agent runs unattended in a loop — scheduled runs, background jobs, or any session without a human watching each step. Applies especially to steps that call external services (network requests, browser actions, subprocess calls), since these can stall silently without raising an error, which iteration counts alone won't catch.
## Detection
- About to start an autonomous or scheduled run loop
- Loop calls external services (network, browser, subprocess) that can hang without erroring
- No existing iteration cap or timeout defined for the loop
## Pattern
Set two independent bounds before starting: a maximum iteration count and a maximum wall-clock duration. Check both on every iteration, not only at the end. Additionally, track a lightweight progress signal (e.g. a hash of the current task state) across iterations — if it hasn't changed after N iterations, treat it as churn and exit the loop, even if neither bound has been hit yet.

```python
import time
start_time=time.monotonic()
max_iterations=50
max_seconds=1800
last_progress_signal=None
stall_count=0

for i in range(max_iterations):
    if time.monotonic()-start_time>max_seconds:
        break

    progress_signal=get_progress_signal()
    if progress_signal==last_progress_signal:
        stall_count+=1
        if stall_count>=3:
            break
    else:
        stall_count=0
    last_progress_signal = progress_signal

    run_iteration()

```

## Outcome
Prevents silent cost drift from runaway loops and catches stalls that neither an iteration cap nor a timeout alone would detect. A loop that's technically still running but making no progress is functionally the same failure as one that's crashed — this pattern treats it that way.
## Related
- [Autonomous Operation Safety](./autonomous-operation-safety.md) - security isolation during autonomous work
- [Safe Operation Patterns](./safe-operation-patterns.md) - GREEN/YELLOW/RED classification END
