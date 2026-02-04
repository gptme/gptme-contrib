"""
ACE - Agentic Context Engineering plugin for gptme.

Provides hybrid retrieval, semantic matching, and context optimization
for gptme's lesson system.

Based on ACE research (Stanford/SambaNova/UC Berkeley, October 2025):
- Treat prompts as "living playbooks" that evolve through generation, reflection, curation
- Addresses brevity bias and context collapse in LLM context management

Key Features:
- Hybrid lesson matching (keyword + semantic + effectiveness + recency)
- Semantic deduplication via embeddings
- Retrieval analytics and tracking
"""

from gptme.tools import ToolSpec

from .gptme_integration import GptmeHybridMatcher
from .hybrid_retriever import HybridConfig, HybridLessonMatcher
from .embedder import LessonEmbedder
from .storage import InsightStorage, StoredInsight
from .curator import CuratorAgent, Delta, DeltaOperation
from .generator import (
    GeneratorAgent,
    TrajectoryParser,
    Insight,
    ThoughtActionObservation,
)

__all__ = [
    "GptmeHybridMatcher",
    "HybridConfig",
    "HybridLessonMatcher",
    "LessonEmbedder",
    # Phase 2: Curator module
    "InsightStorage",
    "StoredInsight",
    "CuratorAgent",
    "Delta",
    "DeltaOperation",
    # Phase 3: Generator module
    "GeneratorAgent",
    "TrajectoryParser",
    "Insight",
    "ThoughtActionObservation",
    "plugin",
]

# Plugin instructions for gptme
_instructions = """
## ACE Context Optimization

ACE (Agentic Context Engineering) provides enhanced lesson matching using:
- **Hybrid Retrieval**: Combines keyword, semantic, effectiveness, and recency scoring
- **Semantic Matching**: Uses embeddings for similarity-based lesson discovery
- **Analytics**: Tracks retrieval patterns for continuous improvement
- **Curator**: Synthesizes insights into delta operations for lesson lifecycle management

### Configuration

Enable hybrid matching with environment variable:
```bash
export GPTME_LESSONS_HYBRID=true
```

### Usage

ACE automatically enhances gptme's lesson matching when enabled. The hybrid
matcher replaces simple keyword matching with multi-signal retrieval that
considers:
- Keyword relevance (25% weight)
- Semantic similarity (40% weight)
- Historical effectiveness (25% weight)
- Recency (10% weight)
- Tool context bonus (20% boost)

### Curator Module

The Curator agent synthesizes refined insights into delta operations for
incremental lesson updates. It generates ADD/REMOVE/MODIFY operations:

```python
from gptme_ace import CuratorAgent, InsightStorage

# Initialize curator
curator = CuratorAgent()

# Generate delta from insight
storage = InsightStorage()
insight = storage.get_insight("insight-id", source_agent="refined")
delta = curator.generate_delta(insight)

# Save and review delta
curator.save_delta(delta)
```

CLI usage:
```bash
# Generate delta for single insight
python -m gptme_ace.curator generate --insight-id abc123

# Batch process approved insights
python -m gptme_ace.curator batch --status approved

# List pending deltas
python -m gptme_ace.curator list --status pending
```

### Generator Module

The Generator agent analyzes session trajectories to extract thought-action-observation
chains and generate candidate insights for the lesson system:

```python
from gptme_ace import GeneratorAgent, TrajectoryParser
from pathlib import Path

# Parse session log
parser = TrajectoryParser(Path("logs/session.log"))
chains = parser.extract_tao_chains()

# Generate insights
generator = GeneratorAgent()
insights = generator.analyze_trajectory(chains, parser.session_id)

for insight in insights:
    print(f"[{insight.category}] {insight.title} ({insight.confidence:.2f})")
```

CLI usage:
```bash
# Analyze single session log
python -m gptme_ace.generator analyze path/to/session.log

# Dry run (parse only, no LLM)
python -m gptme_ace.generator analyze path/to/log --dry-run

# With duplicate detection
python -m gptme_ace.generator analyze path/to/log --workspace ~/bob
```

For embeddings support, install with:
```bash
pip install gptme-ace[embeddings]
```
"""

# Define the plugin specification
plugin = ToolSpec(
    name="ace",
    desc="ACE context optimization - hybrid retrieval and semantic matching for lessons",
    instructions=_instructions,
    available=True,
)
