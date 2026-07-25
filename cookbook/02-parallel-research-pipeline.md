---
title: Parallel Research Pipeline
description: Spawn subagents to research multiple topics simultaneously, then synthesize results
category: multi-agent
difficulty: intermediate
tags: [multi-agent, subagents, research, parallelism]
deep_link: "https://gptme.ai/?prompt=Research+the+top+3+Python+async+libraries+%28asyncio%2C+anyio%2C+trio%29+in+parallel%2C+compare+their+APIs+for+HTTP+clients%2C+and+write+a+recommendation."
---

# Parallel Research Pipeline

## Problem

Deep research takes time when done sequentially. If you need to compare five
frameworks or investigate three bug hypotheses, doing them one-by-one wastes
wall-clock time and burns context on already-answered sub-questions.

## Solution

gptme supports spawning subagents via the `subagent` tool. Each subagent gets a
clean context and runs its task independently. The coordinator waits for all
subagents to finish, then synthesizes the results.

This pattern works well for:
- Comparing N alternatives simultaneously
- Running independent research branches
- Executing the same task against multiple inputs

## Example

Create a coordinator prompt file and run it:

```bash
cat > research-prompt.md << 'EOF'
Research the following three Python HTTP client libraries in parallel:
1. httpx
2. aiohttp
3. requests

For each, find: latest version, async support, connection pooling behavior, and
known issues with timeouts. Then write a comparison table to comparison.md and
recommend one for a high-concurrency microservice.
EOF

gptme --tools subagent "$(cat research-prompt.md)"
```

Or invoke multi-agent mode directly in an interactive session:

```bash
gptme
# > Spawn three subagents: one researches httpx, one aiohttp, one requests.
# > Each should write its findings to a temp file named after the library.
# > When all are done, synthesize the findings into comparison.md.
```

The coordinator agent will:
1. Spawn three subagents with separate contexts
2. Each subagent researches its library (reading docs, running code, etc.)
3. Results are written to shared files
4. Coordinator reads the files and writes the synthesis

## Notes

- Subagents share your filesystem but not context. Use files as the
  communication channel between coordinator and workers.
- For independent tasks, the total wall-clock time is roughly `max(subtask times)`
  rather than `sum(subtask times)`.
- Keep each subagent's prompt self-contained — it won't see the coordinator's
  conversation history.
