# gptme-contrib Packages

Python packages for gptme agents.

## Packages

| Package | Purpose | Install |
|---------|---------|---------|
| **aw-watcher-agent** | ActivityWatch watcher for AI coding assistants (gptme, Claude Code, Codex) | `uv pip install -e packages/aw-watcher-agent` |
| **bobutils** | Shared workspace utilities for gptme agents (stdlib-only) | `uv pip install -e packages/bobutils` |
| **credential-slots** | Safe credential-slot rotation for agents running Claude Max / OAuth-backed subscriptions | `uv pip install -e packages/credential-slots` |
| **gptmail** | Email automation for gptme agents with shared communication utilities | `uv pip install -e packages/gptmail` |
| **gptme-activity-summary** | Activity summarization for gptme agents — journals, GitHub, sessions, tweets, email | `uv pip install -e packages/gptme-activity-summary` |
| **gptme-backoff** | Thin retry utilities built on tenacity — exponential backoff, jitter, and convenient async/sync decorators for agent tool calls | `uv pip install -e packages/gptme-backoff` |
| **gptme-block-registry** | Shared state-dir block-file contract and registry for gptme agent arm dispatch (credential-survival sublayer) | `uv pip install -e packages/gptme-block-registry` |
| **gptme-bob-status** | Bob-specific StatusProvider for gptme-util status | `uv pip install -e packages/gptme-bob-status` |
| **gptme-body-protocol** | Versioned, transport-neutral wire models for gptme body nodes | `uv pip install -e packages/gptme-body-protocol` |
| **gptme-browser-semantic** | Semantic browser primitives (observe/act/extract) for gptme computer-use — Path A: ARIA-snapshot scoring over gptme | `uv pip install -e packages/gptme-browser-semantic` |
| **gptme-cc-memory** | Typed, git-tracked, hook-injected session memory for Claude Code — memory types, retention scoring, behavioral correction semantics, and prompt injection | `uv pip install -e packages/gptme-cc-memory` |
| **gptme-codegraph** | Structural code retrieval for gptme via tree-sitter (callers, callees, blast radius) | `uv pip install -e packages/gptme-codegraph` |
| **gptme-contrib-lib** | Shared library code for gptme agents | `uv pip install -e packages/gptme-contrib-lib` |
| **gptme-coordination** | Generic inter-agent coordination via SQLite: work claims, message bus | `uv pip install -e packages/gptme-coordination` |
| **gptme-daily-briefing** | Generic daily-briefing bundle schema and collectors for gptme agents | `uv pip install -e packages/gptme-daily-briefing` |
| **gptme-dashboard** | Static dashboard generator for gptme workspaces | `uv pip install -e packages/gptme-dashboard` |
| **gptme-forum** | Git-native agent forum (agentboard) — threaded posts, @mentions, and direct messages for gptme agents | `uv pip install -e packages/gptme-forum` |
| **gptme-lessons-extras** | Lesson format validation and analysis | `uv pip install -e packages/gptme-lessons-extras` |
| **gptme-lessons-mcp** | MCP server that exposes gptme | `uv pip install -e packages/gptme-lessons-mcp` |
| **gptme-rag** | ChromaDB-based RAG (Retrieval-Augmented Generation) for gptme agents | `uv pip install -e packages/gptme-rag` |
| **gptme-runloops** | Python-based run loop framework for autonomous AI agent operation | `uv pip install -e packages/gptme-runloops` |
| **gptme-sessions** | Session tracking and analytics for gptme agents | `uv pip install -e packages/gptme-sessions` |
| **gptme-subscription** | Generic subscription observation, pressure scoring, and capacity-aware routing for agent credential slots | `uv pip install -e packages/gptme-subscription` |
| **gptme-usage** | Cross-backend usage, cost, and quota surface for gptme agents (model registry, cost math, quota checks) | `uv pip install -e packages/gptme-usage` |
| **gptme-vision-node** | Vision pipeline for the BobBrain presence node — frame capture, person/motion detection, LLM look tool, and place recognition | `uv pip install -e packages/gptme-vision-node` |
| **gptme-voice** | Voice interface for gptme with OpenAI and xAI Grok Realtime APIs | `uv pip install -e packages/gptme-voice` |
| **gptme-voice-node** | Embedded voice presence node for BobBrain — thin WS client to bob-voice-server | `uv pip install -e packages/gptme-voice-node` |
| **gptme-whatsapp** | WhatsApp integration for gptme agents via whatsapp-web.js | `uv pip install -e packages/gptme-whatsapp` |
| **gptme-wisdom** | Wisdom layer — BM25-searchable index of canonical reference books for gptme agents | `uv pip install -e packages/gptme-wisdom` |
| **gptme-wisdom-mcp** | RAG-as-MCP Knowledge Server for gptme agents — search CS books and session history via MCP | `uv pip install -e packages/gptme-wisdom-mcp` |
| **gptodo** | Task management and work queue generation utilities for gptme agents | `uv pip install -e packages/gptodo` |

## Backward Compatibility

Source-level symlinks are provided for backward compatibility with existing imports:
- `from lessons import ...` works via `gptme-lessons-extras/src/lessons` symlink
- `from lib import ...` works via `gptme-contrib-lib/src/lib` symlink
- `from run_loops import ...` works via `gptme-runloops/src/run_loops` symlink

## Structure

```text
packages/package-name/
├── pyproject.toml    # Config
├── src/package_name/ # Source (new name)
├── src/old_name/     # Symlink to package_name (backward compat)
└── tests/            # Tests
```

## Development

```shell
# Install workspace
uv sync --all-packages

# Run package tests
uv run pytest packages/gptodo/tests

# Run all tests
make test

# Type check
make typecheck
```

## Adding Dependencies

Edit `packages/NAME/pyproject.toml`, then:

```shell
uv sync
```

## References

- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- Root [pyproject.toml](../pyproject.toml) - workspace configuration
