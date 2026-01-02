---
match:
  keywords:
    # Specific problem triggers (high precision)
    - "documentation file consuming excessive context tokens"
    - "always-included file over 500 lines"
    - "context budget consumed by rarely-needed content"
    - "monolithic documentation needs restructuring"
    # Fallback keywords (broader coverage)
    - "documentation"
    - "token"
    - "context"
    - "large file"
    - "auto-include"
status: active
---

# Progressive Disclosure for Documentation

## Rule
Restructure large documentation files (>500 lines, >5k tokens) into slim indexes with on-demand detail directories.

## Context
When always-included documentation files consume excessive context tokens.

## Detection
Observable signals:
- Documentation file > 500 lines always included
- Context budget consumed by rarely-needed content
- Single file contains 10+ distinct sections
- Agent frequently loads details never used

## Pattern
Split monolithic docs into slim index + detail directories:

```txt
# Before: monolithic (11k tokens always)
TOOLS.md
├── Section 1 (rarely needed)
├── Section 2 (rarely needed)
└── ... (15+ sections)

# After: progressive (4k tokens always)
tools/
├── README.md     # Slim index with links
├── topic1/       # On-demand (~1k each)
├── topic2/
└── .../
```

**Slim index structure:**
- Brief overview (2-3 paragraphs)
- Quick reference table (common operations)
- Navigation links to detail directories
- Core principles (bullet list)

## Outcome
Following this pattern results in:
- **40-60% token reduction** in always-included context
- **Faster responses** (less to process)
- **On-demand loading** (details when needed)
- **Better maintainability** (smaller files)

## Related
- [Progressive Disclosure Skill](../../skills/progressive-disclosure/SKILL.md) - Full implementation guide
- [Issue #49](https://github.com/gptme/gptme-contrib/issues/49) - Original proposal
