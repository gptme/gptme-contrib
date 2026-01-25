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

__all__ = [
    "GptmeHybridMatcher",
    "HybridConfig",
    "HybridLessonMatcher",
    "LessonEmbedder",
    "plugin",
]

# Plugin instructions for gptme
_instructions = """
## ACE Context Optimization

ACE (Agentic Context Engineering) provides enhanced lesson matching using:
- **Hybrid Retrieval**: Combines keyword, semantic, effectiveness, and recency scoring
- **Semantic Matching**: Uses embeddings for similarity-based lesson discovery
- **Analytics**: Tracks retrieval patterns for continuous improvement

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
