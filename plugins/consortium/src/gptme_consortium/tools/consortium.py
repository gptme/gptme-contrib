"""
Consortium tool for multi-model consensus decision-making.

Orchestrates multiple LLMs to provide diverse perspectives and synthesize
consensus responses with confidence scoring.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from gptme.tools.base import ToolSpec


def _calculate_response_similarity(responses: dict[str, str]) -> float:
    """
    Calculate average pairwise similarity between responses.

    Uses a simple word-based Jaccard similarity metric.

    Args:
        responses: Dict of model -> response

    Returns:
        Average similarity score (0-1), or 1.0 if fewer than 2 responses
    """
    if len(responses) < 2:
        return 1.0

    # Normalize responses: lowercase and split into word sets
    normalized = {}
    for model, response in responses.items():
        # Simple normalization: lowercase and split on whitespace
        words = set(response.lower().split())
        normalized[model] = words

    # Calculate pairwise Jaccard similarities
    similarities = []
    models_list = list(normalized.keys())
    for i, model1 in enumerate(models_list):
        for model2 in models_list[i + 1 :]:
            words1 = normalized[model1]
            words2 = normalized[model2]

            # Jaccard similarity: intersection / union
            if not words1 and not words2:
                similarity = 1.0  # Both empty = perfectly similar
            elif not words1 or not words2:
                similarity = 0.0  # One empty = not similar
            else:
                intersection = len(words1 & words2)
                union = len(words1 | words2)
                similarity = intersection / union if union > 0 else 0.0

            similarities.append(similarity)

    # Return average similarity
    return sum(similarities) / len(similarities) if similarities else 1.0


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
    agreement_score: float


def _retry_with_backoff(
    func,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
):
    """
    Retry a function with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        backoff_factor: Multiplier for delay after each retry

    Returns:
        Result from function call

    Raises:
        Last exception if all retries fail
    """
    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            error_msg = str(e).lower()

            # Check if this is a rate limit error
            is_rate_limit = any(
                indicator in error_msg
                for indicator in ["rate limit", "429", "too many requests", "quota"]
            )

            if attempt < max_retries:
                # Use longer delay for rate limit errors
                wait_time = delay * 3 if is_rate_limit else delay
                time.sleep(wait_time)
                delay *= backoff_factor
            else:
                # Last attempt failed
                raise last_exception from e


def query_consortium(
    question: str,
    models: list[str] | None = None,
    arbiter: str | None = None,
    confidence_threshold: float = 0.8,
    query_delay: float = 0.5,
) -> ConsortiumResult:
    """
    Query multiple models and synthesize consensus response.

    Args:
        question: The question or prompt to ask
        models: List of model IDs to query (defaults to frontier models)
        arbiter: Model to use for synthesis (defaults to Claude Sonnet 4.5)
        confidence_threshold: Minimum confidence for consensus (0-1)
        query_delay: Delay in seconds between model queries (default 0.5)

    Returns:
        ConsortiumResult with consensus, confidence, and metadata
    """
    if models is None:
        # Default to latest frontier models from major providers
        # Requires API keys: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, XAI_API_KEY
        # Falls back gracefully if some providers unavailable
        models = [
            "anthropic/claude-opus-4-5",
            "openai/gpt-5.1",
            "google/gemini-3-pro-preview",
            "xai/grok-4",
        ]

    if arbiter is None:
        arbiter = "anthropic/claude-sonnet-4-5"

    # Get responses from each model with rate limiting awareness
    responses = {}
    successful_responses = {}
    for i, model in enumerate(models):
        # Add delay between queries to avoid rate limits (except first query)
        if i > 0 and query_delay > 0:
            time.sleep(query_delay)

        try:
            # Use retry logic with exponential backoff
            response = _retry_with_backoff(
                lambda m=model, q=question: _query_single_model(m, q)
            )
            responses[model] = response
            successful_responses[model] = response
        except Exception as e:
            responses[model] = f"Error: {e}"

    # Calculate success rate for confidence adjustment
    success_rate = len(successful_responses) / len(models) if models else 0

    # Calculate agreement score from successful responses
    agreement_score = _calculate_response_similarity(successful_responses)

    # Synthesize consensus using arbiter (pass successful responses for better synthesis)
    synthesis = _synthesize_consensus(
        question, successful_responses or responses, arbiter, confidence_threshold
    )

    # Adjust confidence based on success rate and agreement
    # Higher agreement = more reliable consensus
    base_confidence = synthesis["confidence"]
    adjusted_confidence = base_confidence * success_rate * (0.5 + 0.5 * agreement_score)

    return ConsortiumResult(
        question=question,
        consensus=synthesis["consensus"],
        confidence=adjusted_confidence,
        responses=responses,
        synthesis_reasoning=synthesis["reasoning"],
        models_used=models,
        arbiter_model=arbiter,
        agreement_score=agreement_score,
    )


def _query_single_model(model: str, prompt: str) -> str:
    """
    Query a single model using gptme's infrastructure.

    Args:
        model: Fully qualified model name (e.g., "anthropic/claude-sonnet-4-5")
        prompt: The prompt to send to the model

    Returns:
        The model's response as a string

    Raises:
        Exception: If model query fails
    """
    from gptme.llm import reply
    from gptme.message import Message

    # Create message for the model
    messages = [Message("user", prompt)]

    # Query the model (non-streaming)
    try:
        response = reply(messages, model, stream=False, tools=None)
        return response.content
    except Exception as e:
        error_msg = str(e) if str(e) else "Unknown error - model query failed"
        raise Exception(f"Failed to query {model}: {error_msg}") from e


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
    try:
        arbiter_response = _query_single_model(arbiter, synthesis_prompt)
    except Exception as e:
        # If arbiter fails, return a basic consensus from responses
        return {
            "consensus": "Unable to synthesize consensus due to arbiter failure",
            "confidence": 0.3,
            "reasoning": f"Arbiter model failed: {e}",
        }

    # Parse JSON response (handle markdown code blocks)
    def extract_json(text: str) -> dict[str, Any] | None:
        """Extract JSON from text that might contain markdown code blocks."""
        # Try direct JSON parse first
        try:
            result = json.loads(text)
            return result if isinstance(result, dict) else None
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        json_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
        match = re.search(json_pattern, text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                return result if isinstance(result, dict) else None
            except json.JSONDecodeError:
                pass

        # Try finding JSON object in text
        # Improved pattern to handle multiple nesting levels
        json_obj_pattern = r"\{(?:[^{}]|\{[^{}]*\})*\}"
        match = re.search(json_obj_pattern, text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                return result if isinstance(result, dict) else None
            except json.JSONDecodeError:
                pass

        return None

    result = extract_json(arbiter_response)

    if result:
        return {
            "consensus": result.get("consensus", "Unable to reach consensus"),
            "confidence": float(result.get("confidence", 0.5)),
            "reasoning": result.get("reasoning", "No reasoning provided"),
        }
    else:
        # Fallback: use raw response as consensus
        return {
            "consensus": arbiter_response,
            "confidence": 0.5,
            "reasoning": "Unable to parse structured response - using raw output",
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
        f"\nConsensus (confidence: {result.confidence:.2f}, agreement: {result.agreement_score:.2f}):",
        result.consensus,
        "\n\nSynthesis Reasoning:",
        result.synthesis_reasoning,
        "\n\nIndividual Responses:",
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

Default models (latest frontier):
- anthropic/claude-opus-4-5 (Claude Opus 4.5)
- openai/gpt-5.1 (GPT-5.1)
- google/gemini-3-pro-preview (Gemini 3 Pro)
- xai/grok-4 (Grok 4)

Requires API keys for each provider (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, XAI_API_KEY).
Models without valid keys will gracefully fail and be excluded from consensus.

Arguments:
- question: The question or prompt (required)
- models: List of model IDs to query (optional, defaults to frontier models above)
- arbiter: Model for synthesis (optional, defaults to Claude Sonnet 4.5)
- confidence_threshold: Minimum confidence (optional, default 0.8)
- query_delay: Delay between queries in seconds (optional, default 0.5)
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
    models=["anthropic/claude-opus-4-5", "openai/gpt-5.1", "google/gemini-3-pro-preview"]
)
```
    """,
    functions=[query_consortium],
    block_types=["consortium"],
)
