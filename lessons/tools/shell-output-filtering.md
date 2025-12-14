---
match:
  keywords:
  - shell
  - output
  - tokens
  - grep
  - filter
  - large output
---

# Shell Output Filtering for Token Efficiency

## Rule
Always filter shell output with grep/head/tail when expecting large results (>1000 lines).

## Context
When running shell commands that produce extensive output like logs, file listings, or data dumps.

## Detection
Observable signals that filtering is needed:
- Command expected to produce 1000+ lines
- Reading entire log files without filtering
- Listing all files in large directories
- Dumping full JSON/XML without jq/xmllint
- Previous command dumped 10k+ tokens into context

## Pattern
Filter output before it enters context:
```shell
# Wrong: Dump entire log (could be 10k+ lines)
cat large-log.log

# Correct: Filter to relevant sections
grep "ERROR" large-log.log | tail -50

# Wrong: List all files
ls -la /var/log/

# Correct: Filter by pattern or limit count
ls -la /var/log/*.error | head -20

# Wrong: Full JSON dump
cat huge-config.json

# Correct: Extract specific fields
cat huge-config.json | jq '.relevant.section'
```

## Outcome
Following this pattern results in:
- **Token efficiency**: 1k tokens instead of 10k+
- **Faster responses**: Less content to process
- **Focused analysis**: Only relevant information
- **Cost savings**: Fewer input/output tokens

Examples:
- Full log: 50k lines, ~40k tokens → Filtered: 50 lines, ~400 tokens (99% reduction)
- Directory listing: 10k files, ~15k tokens → Filtered: 20 files, ~300 tokens (98% reduction)

## Related
- Full context: [knowledge/lessons/shell-output-filtering.md](../../knowledge/lessons/tools/shell-output-filtering.md)
- [Shell Command Chaining](./shell-command-chaining.md) - Combining commands
- [Shell Path Quoting](./shell-path-quoting.md) - Proper path handling
