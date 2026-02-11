---
match:
  keywords:
    - "posting same comment multiple times in loop"
    - "agent stuck in endless retry cycle"
    - "repeating failed action without progress"
    - "detected repetitive tool calls"
status: active
---

# Detect and Break Repetitive Action Loops

## Rule
Detect when performing the same action repeatedly (3+ times) and break the loop.

## Context
When automation gets stuck (e.g., bot reviews that can't be resolved), it may repeat the same action indefinitely, wasting resources and creating spam.

## Detection
Observable signals:
- Same comment text posted multiple times
- Same verification check run repeatedly
- Monitoring service posts identical updates
- Log shows repeated identical actions
- Time spent without progress increasing

## Pattern

**Detect repetition:**
```python
def detect_action_loop(action_history, current_action, max_repeats=2):
    """Detect if we're repeating the same action"""
    # Compare current action with recent history
    recent_similar = [
        a for a in action_history[-5:]
        if action_similarity(a, current_action) > 0.8
    ]
    return len(recent_similar) >= max_repeats
```

**Break loop with summary:**
```python
if detect_action_loop(history, current_action):
    logger.warning("Loop detected: same action repeated 3+ times")

    # Post final summary
    post_summary_comment(
        "🛑 Breaking repetitive loop",
        "Detected repetitive action pattern. Summary of situation:",
        findings
    )

    # Break loop
    break
```

**Example from PR monitoring:**
```python
# Track recent comments
recent_comments = get_my_recent_comments(pr_number, limit=10)
comment_texts = [c['body'] for c in recent_comments]

# Before posting new comment
if detect_repetition(comment_texts, new_comment_text, threshold=0.8):
    # Post one final summary instead
    post_final_summary(pr_number,
        "Breaking verification loop - all fixes confirmed in code")
    return
```

## Outcome
Following this pattern:
- Prevents spam in issues/PRs
- Saves computational resources
- Indicates when system is stuck
- Forces human review of situation
- Maintains professional appearance

## Related
- [Avoid Excessive Setup](./avoid-excessive-setup-when-dir.md) - Related anti-pattern
- [GitHub Bot Reviews](../tools/greptile-pr-reviews.md) - Bot review loops are a common trigger
