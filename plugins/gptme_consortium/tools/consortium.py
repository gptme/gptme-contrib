"""
Consortium tool for multi-model consensus decision-making.

Orchestrates multiple LLMs to provide diverse perspectives and synthesize
consensus responses with confidence scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from gptme.message import Message
from gptme.tools.base import ToolSpec


@dataclass
class ConsortiumResult:
    """Result from consortium query."""

    question: str
    consensus: str
    confidence: float
    responses: dict[str, str]
    synthesis_reasoning: str
    models_used: list[str]
    arbiter_model: str


def query_consortium(
    question: str,
    models: list[str] | None = None,
    arbiter: str | None = None,
    confidence_threshold: float = 0.8,
) -> ConsortiumResult:
    """
    Query multiple models and synthesize consensus response.

    Args:
        question: The question or prompt to ask
        models: List of model IDs to query (defaults to frontier models)
        arbiter: Model to use for synthesis (defaults to Claude Sonnet 4.5)
        confidence_threshold: Minimum confidence for consensus (0-1)

    Returns:
        ConsortiumResult with consensus, confidence, and metadata
    """
    if models is None:
        models = [
            "anthropic/claude-sonnet-4-5",
            "openai/gpt-5.1",
            "google/gemini-3-pro",
            "xai/grok-2",
        ]

    if arbiter is None:
        arbiter = "anthropic/claude-sonnet-4-5"

    # Get responses from each model
    responses = {}
    for model in models:
        try:
            response = _query_single_model(model, question)
            responses[model] = response
        except Exception as e:
            responses[model] = f"Error: {e}"

    # Synthesize consensus using arbiter
    synthesis = _synthesize_consensus(
        question, responses, arbiter, confidence_threshold
    )

    return ConsortiumResult(
        question=question,
        consensus=synthesis["consensus"],
        confidence=synthesis["confidence"],
        responses=responses,
        synthesis_reasoning=synthesis["reasoning"],
        models_used=models,
        arbiter_model=arbiter,
    )


def _query_single_model(model: str, prompt: str) -> str:
    """
    Query a single model using gptme's infrastructure.

    This is a simplified implementation that would need to integrate
    with gptme's actual model infrastructure.
    """
    # TODO: Integrate with gptme's model system
    # For now, return a placeholder
    return f"Response from {model}: [Would query model here]"


def _synthesize_consensus(
    question: str,
    responses: dict[str, str],
    arbiter: str,
    threshold: float,
) -> dict[str, Any]:
    """
    Synthesize consensus from multiple responses using arbiter model.

    Args:
        question: Original question
        responses: Dict of model -> response
        arbiter: Arbiter model ID
        threshold: Confidence threshold

    Returns:
        Dict with consensus, confidence, and reasoning
    """
    # Build synthesis prompt
    responses_text = "\n\n".join(
        f"Model {model}:\n{response}" for model, response in responses.items()
    )

    synthesis_prompt = f"""Given the following question and multiple model responses, synthesize a consensus answer.

Question: {question}

Responses:
{responses_text}

Analyze the responses and provide:
1. A synthesized consensus answer that incorporates the best insights from all models
2. A confidence score (0-1) indicating agreement level between models
3. Reasoning for the synthesis and confidence score

Respond in JSON format:
{{
    "consensus": "synthesized answer",
    "confidence": 0.0-1.0,
    "reasoning": "explanation of synthesis"
}}"""

    # Query arbiter model
    # TODO: Integrate with gptme's model system
    arbiter_response = _query_single_model(arbiter, synthesis_prompt)

    # Parse response (simplified - would need proper JSON extraction)
    try:
        result = json.loads(arbiter_response)
        return {
            "consensus": result.get("consensus", "Unable to reach consensus"),
            "confidence": result.get("confidence", 0.5),
            "reasoning": result.get("reasoning", "No reasoning provided"),
        }
    except json.JSONDecodeError:
        return {
            "consensus": arbiter_response,
            "confidence": 0.5,
            "reasoning": "Unable to parse structured response",
        }


def _execute_query_consortium(
    question: str,
    models: list[str] | None = None,
    arbiter: str | None = None,
    confidence_threshold: float = 0.8,
) -> str:
    """Execute consortium query and format results for display."""
    result = query_consortium(question, models, arbiter, confidence_threshold)

    # Format output
    output = [
        "=== Consortium Query Results ===\n",
        f"Question: {result.question}\n",
        f"\nConsensus (confidence: {result.confidence:.2f}):",
        result.consensus,
        f"\n\nSynthesis Reasoning:",
        result.synthesis_reasoning,
        f"\n\nIndividual Responses:",
    ]

    for model, response in result.responses.items():
        output.append(f"\n{model}:")
        output.append(response)

    output.append(f"\n\nModels used: {', '.join(result.models_used)}")
    output.append(f"Arbiter: {result.arbiter_model}")

    return "\n".join(output)


# Tool specification
consortium_tool = ToolSpec(
    name="consortium",
    desc="Multi-model consensus decision-making",
    instructions="""Use this tool to get multiple LLM perspectives on important questions or decisions.

The tool queries multiple frontier models, has them provide independent responses,
then uses an arbiter model to synthesize a consensus answer with confidence scoring.

Best for:
- Important architectural or design decisions
- Questions where diverse perspectives add value
- Situations requiring high confidence in the answer
- Comparing model capabilities on specific topics

Arguments:
- question: The question or prompt (required)
- models: List of model IDs to query (optional, defaults to Claude, GPT-4o, o1-mini)
- arbiter: Model for synthesis (optional, defaults to Claude Sonnet 4.5)
- confidence_threshold: Minimum confidence (optional, default 0.8)
    """,
    examples="""
### Query consortium for architectural decision

> User: Should we use microservices or monolith for this project?
> Assistant: Let me get multiple perspectives on this.
```consortium
query_consortium(
    question=\"\"\"For a project with 3 developers, expected to scale to 100K users
    in 2 years, should we use microservices or monolithic architecture? Consider
    development speed, maintenance, and scalability.\"\"\"
)
```
> System: === Consortium Query Results ===
> Consensus (confidence: 0.85): Start with a modular monolith...

### Compare model perspectives on technical topic

> User: What's the best approach for handling rate limiting?
> Assistant: I'll query the consortium for different perspectives.
```consortium
query_consortium(
    question="What are the pros and cons of different rate limiting strategies 
    (token bucket, leaky bucket, fixed window, sliding window)?",
    models=["anthropic/claude-sonnet-4-5", "openai/gpt-5.1", "google/gemini-3-pro"]
)
```
    """,
    functions=[query_consortium],
    block_types=["consortium"],
)
