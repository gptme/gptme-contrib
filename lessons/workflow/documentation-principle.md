---
match:
  keywords:
  - "let me document this"
  - "worth documenting"
  - "create a knowledge article"
  - "update the knowledge base"
  - "document the finding"
status: active
---

# Documentation Principle

## Rule
Only document if it will be read and used within next few conversations.

## Context
When deciding whether to create documentation during autonomous operation.

## Detection
Observable signals indicating documentation debt:
- Creating documentation that sits unread
- Writing detailed docs for one-time work
- Documentation cluttering context without value
- Spending time documenting instead of executing
- Creating "just in case" documentation

## Pattern
Strategic documentation decisions based on usage:
```text
Context files (agent config):
  → Always read in future runs ✓

README updates:
  → Users/maintainers will read ✓

One-time investigation:
  → Won't be referenced again ✗

Lessons from recurring issues:
  → Prevents future mistakes ✓
```

## Outcome
Following this principle leads to:
- Efficient use of time (execution over documentation)
- Clean context budget (only valuable content)
- No documentation debt (all docs serve purpose)
- Strategic knowledge building (focus on reusable patterns)

## Related
- [Persistent Learning](../patterns/persist-before-noting.md) - When to create lessons
