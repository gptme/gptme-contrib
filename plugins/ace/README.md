# ACE - Agentic Context Engineering Plugin

Context optimization plugin for gptme, providing hybrid retrieval, semantic matching, and context curation for the lesson system.

## Research Background

Based on **Agentic Context Engineering (ACE)** research (Stanford/SambaNova/UC Berkeley, October 2025):

- **Core Insight**: Treat agent prompts as "living playbooks" that evolve through iterative generation, reflection, and curation
- **Problem Solved**: Addresses brevity bias (loss of nuanced details) and context collapse (degradation from repeated rewrites)
- **Framework Components**:
  - Generator: Creates candidate reasoning trajectories
  - Reflector: Evaluates outputs, distilling insights
  - Curator: Iteratively refines and prunes the playbook

Also incorporates insights from:
- HGM (Huxley-Gödel Machine): +10.6% performance via context optimization
- GEPA (Genetic-Pareto): 70-90% token savings through intelligent context selection

## Features

### Hybrid Lesson Matching

Replaces simple keyword matching with multi-signal retrieval:

| Signal | Weight | Description |
|--------|--------|-------------|
| Keyword | 25% | Traditional keyword matching |
| Semantic | 40% | Embedding-based similarity |
| Effectiveness | 25% | Historical usage success |
| Recency | 10% | Recently used lessons boosted |
| Tool Bonus | +20% | Bonus for matching tools |

### Semantic Deduplication

Uses sentence embeddings to detect similar lessons:
- Prevents redundant lesson accumulation
- Identifies consolidation opportunities
- Supports multiple similarity thresholds

### Retrieval Analytics

Tracks retrieval patterns for continuous improvement:
- Session-level retrieval logging
- Method comparison (keyword vs hybrid)
- Effectiveness correlation

## Installation

```bash
# Basic installation
pip install gptme-ace

# With embeddings support (recommended)
pip install gptme-ace[embeddings]

# Full installation (includes analytics tools)
pip install gptme-ace[full]
```

## Configuration

Enable hybrid matching via environment variable:

```bash
export GPTME_LESSONS_HYBRID=true
```

## Usage

### As gptme Plugin

The plugin automatically enhances gptme's lesson matching when enabled:

```toml
# gptme.toml
[plugins]
enabled = ["gptme_ace"]
```

### Programmatic Usage

```python
from gptme_ace import GptmeHybridMatcher, LessonEmbedder

# Initialize with embeddings
embedder = LessonEmbedder()
matcher = GptmeHybridMatcher(embedder=embedder)

# Match lessons
results = matcher.match(lessons, context, threshold=0.5)
```

## Dependencies

**Required:**
- gptme
- pydantic ≥2.0.0
- pyyaml ≥6.0

**Optional (embeddings):**
- sentence-transformers ≥2.2.0
- faiss-cpu ≥1.7.0
- numpy ≥1.24.0
- scipy ≥1.9.0

## Migration from packages/ace

This plugin was migrated from Bob's workspace (`packages/ace/`) to gptme-contrib for broader use. The core functionality is preserved:

- `GptmeHybridMatcher` - Drop-in replacement for gptme's LessonMatcher
- `LessonEmbedder` - Embedding generation and similarity search
- `HybridLessonMatcher` - Core hybrid retrieval algorithm
- `RetrievalTracker` - Analytics and tracking

## License

MIT
