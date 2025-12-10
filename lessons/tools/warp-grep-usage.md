---
match:
  keywords: ["warp_grep", "warp-grep", "semantic search", "code search", "find code"]
---

# Using Warp-Grep for Semantic Code Search

## Rule
Use warp-grep for natural language code search across repositories, preferring smaller focused repos and code-oriented queries.

## Context
When you need to understand unfamiliar codebases, find implementations, or locate specific patterns using natural language queries.

## Detection
Observable signals that warp-grep is appropriate:
- Need to find implementation of a concept described in prose
- Looking for "how does X work" in a codebase
- Searching across multiple files for related functionality
- Traditional grep/ripgrep would require knowing exact patterns
- Exploring unfamiliar code structure

## Pattern
```python
# Basic usage
warp_grep("How does authentication work", "/path/to/repo")

# Batch queries for comprehensive understanding
from gptme_warp_grep.tools.warp_grep import warp_grep

results = []
results.append(warp_grep("Find the main entry point", repo))
results.append(warp_grep("How are errors handled", repo))
results.append(warp_grep("What validation is performed", repo))
# Process results...

# Code-focused queries work best
warp_grep("Find the function that validates user input", repo)  # ✅ Good
warp_grep("Find documentation about the API", repo)  # ⚠️ May miss markdown
```

**Limitations to know:**
- 176k token context limit - large repos may fail
- Optimized for code, not documentation (markdown less reliable)
- Each query takes 2-6 API calls (~5-15 seconds)
- Some queries return empty results - try rephrasing

## Outcome
Following this pattern leads to:
- **Rapid understanding**: Grasp unfamiliar codebases quickly
- **Natural queries**: No need to know exact patterns
- **Comprehensive search**: Finds related code across files
- **Syntax highlighting**: Results formatted with proper language tags

Batch queries enable thorough exploration:
- 4 queries covering different aspects (~40s total)
- Results can be cross-referenced for full picture
- Better than single broad query that may miss aspects

## Related
- [Warp-Grep Plugin](../../plugins/warp-grep/) - Implementation
- [Morph Documentation](https://docs.morphllm.com/sdk/components/warp-grep/) - API reference
