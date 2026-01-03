# gptme Plugins

Collection of plugins for [gptme](https://github.com/ErikBjare/gptme).

## Available Plugins

### 🧠 attention-router
Attention-based context routing with HOT/WARM/COLD tiers. Implements dynamic context management inspired by Claude Cognitive.

**Use for**: Reducing token usage by dynamically loading context based on relevance.

[Documentation](./attention-router/README.md)

### 📊 attention-history
Attention history tracking for meta-learning. Queryable record of what was in context during each session.

**Use for**: Analyzing context patterns, finding underutilized files, improving keyword matching.

[Documentation](./attention-history/README.md)

### 🤝 consortium
Multi-model consensus decision-making system that orchestrates multiple LLMs to provide diverse perspectives and synthesize consensus responses.

**Use for**: Important decisions, architectural choices, code review from multiple perspectives, model comparison.

[Documentation](./consortium/README.md)

### 🔍 cc-analyze
Claude Code analysis subagent plugin. Spawn focused Claude Code analysis tasks from within gptme.

**Use for**: Security audits, code reviews, test coverage analysis.

[Documentation](./cc-analyze/README.md)

### 🎨 imagen
Multi-provider image generation supporting Google Gemini (Imagen), OpenAI DALL-E, and more with a unified interface.

**Use for**: Creating diagrams, UI mockups, presentation graphics, visual prototyping.

[Documentation](./imagen/README.md)

## Installation

Add to your gptme.toml:

    [plugins]
    paths = ["path/to/gptme-contrib/plugins"]
    enabled = ["attention_router", "attention_history"]

## Usage

Once configured, plugins are automatically loaded.
