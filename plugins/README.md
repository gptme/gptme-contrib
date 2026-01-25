# gptme Plugins

Collection of plugins for [gptme](https://github.com/ErikBjare/gptme).

## Available Plugins

### 🧠 gptme-attention-tracker
Attention tracking and routing plugin combining history tracking and HOT/WARM/COLD tier management. Implements dynamic context management for both meta-learning and token optimization.

**Use for**: Reducing token usage by dynamically loading context based on relevance, analyzing context patterns, improving keyword matching.

[Documentation](./gptme-attention-tracker/README.md)

### 🤖 gptme-claude-code
Full Claude Code integration plugin. Spawn Claude Code subagents from within gptme for analysis, Q&A, bug fixes, and implementation.

**Use for**: Security audits, code reviews, codebase Q&A, bug fixes, feature implementation.

[Documentation](./gptme-claude-code/README.md)

### 🤝 gptme-consortium
Multi-model consensus decision-making system that orchestrates multiple LLMs to provide diverse perspectives and synthesize consensus responses.

**Use for**: Important decisions, architectural choices, code review from multiple perspectives, model comparison.

[Documentation](./gptme-consortium/README.md)

### 🪝 gptme-hooks-examples
Example implementations of gptme hooks for customizing agent behavior.

**Use for**: Learning how to create custom hooks, template for new hook development.

[Documentation](./gptme-hooks-examples/README.md)

### 📝 gptme-gupp
Work persistence plugin for session continuity. Saves and restores work state across sessions.

**Use for**: Resuming work after interruptions, maintaining context across sessions.

[Documentation](./gptme-gupp/README.md)

### 🎨 gptme-imagen
Multi-provider image generation supporting Google Gemini (Imagen), OpenAI DALL-E, and more with a unified interface.

**Use for**: Creating diagrams, UI mockups, presentation graphics, visual prototyping.

[Documentation](./gptme-imagen/README.md)

### 🔧 gptme-lsp
Language Server Protocol integration for enhanced code intelligence.

**Use for**: Code completion, diagnostics, and navigation within gptme.

[Documentation](./gptme-lsp/README.md)

### 🔍 gptme-warpgrep
Enhanced search capabilities with Warp-style filtering and presentation.

**Use for**: Fast, intuitive code search with visual highlighting.

[Documentation](./gptme-warpgrep/README.md)

### 📦 gptme-wrapped
Wrapped tool definitions for safer, constrained tool execution.

**Use for**: Creating sandboxed tool environments with restricted capabilities.

[Documentation](./gptme-wrapped/README.md)

## Installation

Add to your gptme.toml:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins"]
enabled = ["gptme_attention_tracker"]
```

## Plugin Naming Convention

All plugins in this collection follow the naming convention:
- **Directory**: `gptme-<name>/` (with hyphen)
- **Package name**: `gptme-<name>` (with hyphen, in pyproject.toml)
- **Python module**: `gptme_<name>` (with underscore, for imports)

This ensures unique, valid package names and clear identification as gptme plugins.

## Usage

Once configured, plugins are automatically loaded.
