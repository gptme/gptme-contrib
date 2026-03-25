---
match:
  keywords:
    - "sibling tool call errored"
    - "parallel tool call failed"
    - "tool call error in batch"
status: active
---

# Sibling Tool Call Errored Cascades from One Failed Tool in a Batch

## Rule
When a tool in a parallel batch reports "Sibling tool call errored", find the *real* failing call first — then retry only that one, not all calls in the batch.

## Context
When making multiple parallel tool calls in a single message. If one fails, other calls in the same batch may cascade-fail with "Sibling tool call errored" even though they would have succeeded independently.

## Detection
Observable signals:
- `<tool_use_error>Sibling tool call errored</tool_use_error>` in a tool result
- One or more other tool calls in the batch have actual errors
- The "sibling error" tool has no error of its own — it's collateral damage

## Pattern
```python
# Scenario: parallel batch where one call fails
Read("file-a.py")    # ← this one fails (file doesn't exist)
Read("file-b.py")    # ← reports "Sibling tool call errored" (was fine on its own)

# Fix: identify which call actually failed, retry that one only
# Don't blindly retry all calls — the sibling ones were likely fine

# Prevention: for risky operations, verify first before parallelizing
Read("uncertain-file.py")   # verify existence alone first
# Then parallelize the safe ones:
Read("file-b.py")
Read("file-c.py")
```

## Outcome
- Faster root-cause identification (look for the real error, not the cascade)
- Avoids unnecessary retries of calls that were never broken
- Better batch strategy: group calls with similar failure risk

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - Parallel tool call patterns
