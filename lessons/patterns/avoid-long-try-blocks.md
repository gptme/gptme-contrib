---
match:
  keywords:
  - try block too long
  - exception handling > 10 lines
  - split error handling into focused blocks
  - multiple operations in one try block
  - generic error messages
status: active
---

# Avoid Long Try/Catch Blocks

## Rule
Keep try blocks focused on single operations (≤10 lines) to make errors identifiable and patches manageable.

## Context
When writing error handling code, particularly in complex workflows or API interactions.

## Detection
Observable signals indicating problematic try blocks:
- Try blocks longer than ~10 lines
- Multiple unrelated operations in one try block
- Difficult to identify which specific operation failed
- Patches failing because error location is unclear
- Generic error messages like "Operation failed"

## Pattern
Split into focused try blocks:

```python
# ❌ Anti-pattern: Long try block (hard to debug, patch fails)
def complex_operation(data):
    try:
        validate_input(data)
        processed = process_items(data)
        save_to_database(processed)
        send_notifications(processed)
        update_cache(processed)
        return processed
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        return None

# ✅ Correct: Focused try blocks (clear error sources)
def complex_operation(data):
    validate_input(data)  # Let validation errors propagate

    processed = process_items_safely(data)
    if not processed:
        return None

    if not save_to_database_safely(processed):
        return None

    send_notifications_safely(processed)  # Non-critical
    update_cache_safely(processed)  # Non-critical
    return processed

def process_items_safely(data):
    processed = []
    for item in data:
        try:
            result = process_item(item)
            processed.append(result)
        except Exception as e:
            logger.warning(f"Failed to process item: {e}")
            continue  # Skip failed items
    return processed
```

## Outcome
Following this pattern leads to:
- Clear error identification (know exactly what failed)
- Easier patching (patch tool targets specific operations)
- Better error messages (specific context per failure)
- Improved debugging (focused error handling)
- Cleaner code (single responsibility per try block)

## Related
- [Avoid Deep Nesting](./avoid-deep-nesting.md) - Related code structure pattern
