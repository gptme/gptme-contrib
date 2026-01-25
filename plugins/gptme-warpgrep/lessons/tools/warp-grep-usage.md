---
match:
  keywords: ["warp_grep", "warp-grep", "semantic search", "code search", "find code"]
---

# Using Warp-Grep for Semantic Code Search

## Rule
Use warp-grep for natural language code search across repositories; it works on any git repo regardless of size.

## Context
When you need to understand unfamiliar codebases, find implementations, locate patterns, or search through documentation/journals using natural language.

## Detection
Observable signals that warp-grep is appropriate:
- Need to find implementation of a concept described in prose
- Looking for "how does X work" in a codebase
- Searching across multiple files for related functionality
- Traditional grep/ripgrep would require knowing exact patterns
- Exploring unfamiliar code structure
- Finding historical context (journals, session logs)

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

# Works for both code and documentation
warp_grep("Find the ToolSpec class definition", repo)  # ✅ Code
warp_grep("What work was done in Session 1695", repo)  # ✅ Journals/markdown
warp_grep("Find shell command execution", repo)       # ✅ Implementation details

# Multi-repo exploration
repos = ["/path/to/project1", "/path/to/project2", "/path/to/project3"]
for repo in repos:
    result = warp_grep("How are plugins loaded", repo)
    print(f"{repo}: {result[:500]}")
```

**How it works:**
- Uses git grep + git ls-files (only searches tracked files)
- Model generates search commands, we execute locally, send results back
- Typically 2-4 API calls per query (~5-15 seconds)
- Results include syntax-highlighted code blocks

**Tips:**
- Specific queries work better than broad ones
- If no results, try rephrasing or being more specific
- Works on large repos (gptme: 130k files → searches only 431 tracked)

## Outcome
Following this pattern leads to:
- **Rapid understanding**: Grasp unfamiliar codebases quickly
- **Natural queries**: No need to know exact patterns
- **Comprehensive search**: Finds related code across files
- **Syntax highlighting**: Results formatted with proper language tags
- **Large repo support**: Git-aware search prevents context overflow

Batch queries enable thorough exploration:
- 3-4 queries covering different aspects (~30-45s total)
- Results can be cross-referenced for full picture
- Better than single broad query that may miss aspects

## Related
- [Warp-Grep Plugin](../../) - Implementation
- [Morph Documentation](https://docs.morphllm.com/sdk/components/warp-grep/) - API reference
