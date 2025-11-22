# Consortium Plugin for gptme

Multi-model consensus decision-making for gptme.

## Overview

The consortium plugin orchestrates multiple LLMs to provide diverse perspectives and synthesize consensus responses. It queries multiple frontier models, then uses an arbiter model to analyze and synthesize a consensus answer with confidence scoring.

## Features

- **Multi-model orchestration**: Query multiple models in parallel
- **Consensus synthesis**: Arbiter model synthesizes best answer
- **Confidence scoring**: Quantifies agreement between models
- **Flexible configuration**: Choose models and arbiter
- **Detailed output**: See individual responses and synthesis reasoning

## Installation

The plugin is automatically discovered when placed in a configured plugin path. Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/plugins"]
enabled = ["gptme_consortium"]
```

## Usage

### Basic Query

```consortium
query_consortium(
    question="What's the best approach for handling rate limiting?"
)
```

### With Custom Models

```consortium
query_consortium(
    question="Should we use microservices or monolith?",
    models=[
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-4o",
        "openai/o1"
    ],
    arbiter="anthropic/claude-opus-4"
)
```

### With Confidence Threshold

```consortium
query_consortium(
    question="Critical architectural decision...",
    confidence_threshold=0.9  # Require 90% confidence
)
```

## Output Format

The tool returns:
- **Consensus**: Synthesized answer incorporating all perspectives
- **Confidence**: Score from 0-1 indicating model agreement
- **Individual Responses**: Each model's perspective
- **Synthesis Reasoning**: Why the arbiter chose this consensus
- **Metadata**: Models used, arbiter model

## Use Cases

- **Architectural Decisions**: Get multiple expert perspectives
- **Code Review**: Multiple models review the same code
- **Quality Checking**: Validate important outputs
- **Model Comparison**: See how different models approach a problem
- **High-Stakes Decisions**: Require consensus before proceeding

## Dependencies

- gptme (core)
- Access to configured LLM providers (Anthropic, OpenAI, etc.)

## Configuration

Default models (used if not specified):
- anthropic/claude-sonnet-4-5 (Claude Sonnet 4.5, Sept 2025)
- openai/gpt-5.1 (GPT-5.1, Nov 2025)
- google/gemini-3-pro (Gemini 3 Pro, Nov 2025)
- xai/grok-2 (Grok 2)

Default arbiter:
- anthropic/claude-sonnet-4-5 (Claude Sonnet 4.5)

These represent diverse frontier models for comprehensive perspectives.

## Future Enhancements

- [ ] Iterative refinement with multiple rounds
- [ ] Voting mechanisms for discrete choices
- [ ] Integration with gptme's model configuration
- [ ] Caching of model responses
- [ ] Async parallel querying for speed
- [ ] Support for structured output formats
