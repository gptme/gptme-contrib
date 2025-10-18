#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10,<3.12"
# dependencies = [
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
#   "pyyaml>=6.0.0",
# ]
# [tool.uv]
# exclude-newer = "2024-01-01T00:00:00Z"
# ///
"""
LLM integration for Twitter workflow.

This module handles:
1. Tweet evaluation using LLM
2. Response generation
3. Review assistance
"""

import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeVar,
    cast,
)

import yaml
from gptme.dirs import get_project_git_dir
from gptme.llm import reply
from gptme.llm.models import get_default_model
from gptme.message import Message
from gptme.prompts import prompt_workspace
from rich.console import Console


class TaskType(Enum):
    """Types of LLM tasks"""

    EVALUATE = "evaluate"
    RESPONSE = "response"
    REVIEW = "review"


@dataclass
class EvaluationResponse:
    """Tweet evaluation response"""

    # always reason first, "LLMs need tokens to think"
    reasoning: str

    relevance: float
    engagement_type: str
    priority: int
    # Valid actions: "respond" (generate reply), "ignore" (skip)
    action: Literal["respond", "ignore"]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvaluationResponse":
        action_str = str(data["action"])

        # Validate that action is valid
        if action_str not in ["respond", "ignore"]:
            raise ValueError(f"Invalid action value: {action_str}")

        # Use typing.cast to explicitly tell mypy about the type
        action_literal = cast(Literal["respond", "ignore"], action_str)

        return cls(
            relevance=float(data["relevance"]),
            engagement_type=str(data["engagement_type"]),
            priority=int(data["priority"]),
            action=action_literal,
            reasoning=str(data["reasoning"]),
        )

    @classmethod
    def example(cls) -> "EvaluationResponse":
        """Example for LLM format documentation"""
        return cls(
            reasoning="Not relevant to our interests",
            relevance=0.0,
            engagement_type="none",
            priority=0,
            action="ignore",
        )

    @classmethod
    def default(cls) -> "EvaluationResponse":
        return cls(
            relevance=0.0,
            engagement_type="none",
            priority=0,
            action="ignore",
            reasoning="Error processing LLM response",
        )


@dataclass
class TweetResponse:
    """Generated tweet response"""

    # always reason first, "LLMs need tokens to think"
    reasoning: str

    text: str
    type: str
    thread_needed: bool
    follow_up: Optional[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TweetResponse":
        return cls(
            text=str(data["text"]),
            type=str(data["type"]),
            thread_needed=bool(data["thread_needed"]),
            follow_up=str(data["follow_up"]) if data.get("follow_up") else None,
            reasoning=str(data["reasoning"]),
        )

    @classmethod
    def example(cls) -> "TweetResponse":
        """Example for LLM format documentation"""
        return cls(
            reasoning="User asked about ActivityWatch features",
            text="Thanks for the question! ActivityWatch...",
            type="reply",
            thread_needed=False,
            follow_up=None,
        )

    @classmethod
    def default(cls) -> "TweetResponse":
        return cls(
            reasoning="Could not parse LLM response",
            text="Error processing response",
            type="reply",
            thread_needed=False,
            follow_up=None,
        )


@dataclass
class ReviewResult:
    """Review criteria result"""

    notes: str
    passed: bool

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewResult":
        # Handle all possible field names for backward compatibility
        pass_value = data.get("passed", data.get("pass_", data.get("pass", False)))
        return cls(
            notes=str(data["notes"]),
            passed=bool(pass_value),
        )

    @classmethod
    def example(cls) -> "ReviewResult":
        """Example for LLM format documentation"""
        return cls(
            notes="Clear and helpful",
            passed=True,
        )


@dataclass
class ReviewResponse:
    """Tweet review response"""

    criteria_results: Dict[str, ReviewResult]
    recommendation: str
    improvements: List[str]
    reasoning: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewResponse":
        return cls(
            criteria_results={k: ReviewResult.from_dict(v) for k, v in data["criteria_results"].items()},
            recommendation=str(data["recommendation"]),
            improvements=[str(i) for i in data["improvements"]],
            reasoning=str(data["reasoning"]),
        )

    @classmethod
    def example(cls) -> "ReviewResponse":
        """Example for LLM format documentation"""
        return cls(
            reasoning="Draft maintains professional tone and adds value",
            criteria_results={"professional_tone": ReviewResult.example()},
            recommendation="approve",
            improvements=["Consider adding specific example"],
        )

    @classmethod
    def default(cls) -> "ReviewResponse":
        return cls(
            criteria_results={},
            recommendation="reject",
            improvements=["Error processing review"],
            reasoning="Could not parse LLM response",
        )


ResponseType = EvaluationResponse | TweetResponse | ReviewResponse


# Initialize rich console
console = Console()


def load_config() -> Dict[Any, Any]:
    """Load workflow configuration"""
    config_path = Path(__file__).parent / "config" / "config.yml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open() as f:
        return cast(Dict[Any, Any], yaml.safe_load(f))


def create_tweet_eval_prompt(tweet: Dict, config: Dict) -> str:
    """Create prompt for tweet evaluation"""
    # Include thread context if available
    thread_context = ""
    if tweet.get("thread_context"):
        thread_context = "\nConversation Thread:\n"
        for i, t in enumerate(tweet["thread_context"]):
            thread_context += f"Tweet {i + 1} - @{t['author']}: {t['text']}\n"

    return f"""Evaluate this tweet for response suitability.

Tweet: "{tweet["text"]}"
Author: @{tweet["author"]}
Context: {json.dumps(tweet.get("context", {}), indent=2)}
{thread_context}

Evaluation criteria:
1. Relevance to our topics: {", ".join(config["evaluation"]["topics"])}
2. Mentions of our projects: {", ".join(config["evaluation"]["projects"])}
3. Type of engagement needed (if any):
   {json.dumps(config["evaluation"]["triggers"], indent=2)}
4. Sufficient context to provide a meaningful response

Blacklist check:
- Forbidden topics: {", ".join(config["blacklist"]["topics"])}
- Spam patterns: {", ".join(config["blacklist"]["patterns"])}

IMPORTANT: For the "action" field, use ONLY one of these values:
- "respond" - Generate a response to this tweet
- "ignore" - Skip this tweet, no response needed"""


def create_response_prompt(tweet: Dict, eval_result: Dict, config: Dict) -> str:
    """Create prompt for response generation"""
    # Include thread context if available
    thread_context = ""
    if tweet.get("thread_context"):
        thread_context = "\nConversation Thread:\n"
        for i, t in enumerate(tweet["thread_context"]):
            thread_context += f"Tweet {i + 1} - @{t['author']}: {t['text']}\n"

    return f"""Draft a response tweet.

Original Tweet: "{tweet["text"]}"
Author: @{tweet["author"]}
Context: {json.dumps(tweet.get("context", {}), indent=2)}
{thread_context}

Evaluation:
{json.dumps(eval_result, indent=2)}

Response Guidelines:
1. Maintain professional, helpful tone
2. Focus on technical accuracy
3. Keep under 280 characters
4. Use 1-2 emojis maximum
5. Put links in follow-up tweets
6. Avoid controversial topics
7. Add value to the discussion
8. Demonstrate understanding of specific context from the thread
9. Reference relevant details to show you've understood the conversation

Few-shot examples:
{yaml.dump(config["templates"]["examples"])}"""


def create_review_prompt(draft: Dict, config: Dict) -> str:
    """Create prompt for draft review"""
    # Build detailed criteria section
    detailed_criteria = ""
    if "criteria_descriptions" in config["review"]:
        detailed_criteria = "\nDetailed Criteria:\n"
        for criterion in config["review"]["criteria_descriptions"]:
            detailed_criteria += f"- {criterion['name']}: {criterion['description']}\n"
            if "examples" in criterion:
                for example in criterion["examples"]:
                    for status, text in example.items():
                        detailed_criteria += f"  • {status.upper()}: {text}\n"

    # Include thread context if available
    thread_context = ""
    if draft.get("context", {}).get("original_tweet", {}).get("thread_context"):
        thread_context = "\nThread Context:\n"
        for i, tweet in enumerate(draft["context"]["original_tweet"]["thread_context"]):
            thread_context += f"Tweet {i + 1} - @{tweet['author']}: {tweet['text']}\n"

    return f"""Review this draft tweet.

Draft Tweet: "{draft["text"]}"
Type: {draft["type"]}
Context: {json.dumps(draft.get("context", {}), indent=2)}
{thread_context}

Review Criteria:
{yaml.dump(config["review"]["required_checks"])}
{detailed_criteria}"""


def get_system_prompt() -> Message:
    """Get system prompt for Twitter interactions."""
    workspace = get_project_git_dir()
    context = prompt_workspace(workspace) if workspace else ""

    def get_format_examples() -> Dict[str, Any]:
        """Generate format examples from dataclasses"""
        return {
            "evaluation": asdict(EvaluationResponse.example()),
            "response": asdict(TweetResponse.example()),
            "review": asdict(ReviewResponse.example()),
        }

    formats = get_format_examples()

    # Create Twitter-specific system prompt
    twitter_prompt = f"""You are Bob (@TimeToBuildBob), an AI agent who evaluates and responds to tweets.
Your task is to evaluate tweets and generate appropriate responses while:
1. Maintaining your established personality (direct, opinionated, occasionally witty)
2. Focusing on technical topics and project updates
3. Following Twitter best practices (character limits, emoji usage, etc.)

IMPORTANT RESPONSE FORMAT:
- Return ONLY a single JSON object
- Do NOT include any other text, thinking, or commentary
- Do NOT use multiple responses
- Do NOT include <thinking> tags
- Follow EXACTLY the JSON format for each task type:

Evaluation format:
{json.dumps(formats["evaluation"], indent=2)}

Response format:
{json.dumps(formats["response"], indent=2)}

Review format:
{json.dumps(formats["review"], indent=2)}

Any analysis or thinking should be included in the "reasoning" field of the JSON."""

    # Combine context and Twitter-specific prompt
    system_prompt = f"{twitter_prompt}\n\n{context}"
    return Message("system", system_prompt)


T = TypeVar("T", EvaluationResponse, TweetResponse, ReviewResponse)


def parse_llm_response(content: str, response_type: Type[T], task: TaskType) -> T:
    """Parse LLM response into a typed response object.

    Args:
        content: The LLM response content to parse
        response_type: The type to parse into (must implement ResponseProtocol)
        task: Task type for error messages
    """
    try:
        # Extract just the JSON part (between first { and last })
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            json_str = content[start:end]
            data = json.loads(json_str)
            return response_type.from_dict(data)
        raise json.JSONDecodeError("No JSON found", content, 0)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        console.print(f"[red]Error: Could not parse LLM response for {task.value}: {e}")
        console.print(content)
        logging.exception(f"Error parsing LLM response for {task.value}")
        return response_type.default()


def evaluate_tweet(tweet: Dict) -> EvaluationResponse:
    """Evaluate tweet using LLM"""
    config = load_config()
    prompt = create_tweet_eval_prompt(tweet, config)

    # Get LLM response
    model = get_default_model()
    if not model:
        raise RuntimeError("default model not set")
    messages = [get_system_prompt(), Message("user", prompt)]
    response = reply(messages, model.full, stream=False)

    return parse_llm_response(response.content, EvaluationResponse, TaskType.EVALUATE)


def generate_response(tweet: Dict, eval_result: EvaluationResponse) -> Optional[TweetResponse]:
    """Generate response using LLM"""
    if eval_result.action != "respond":
        return None

    config = load_config()
    # Convert dataclass to dict for JSON serialization
    eval_dict = {
        "relevance": eval_result.relevance,
        "engagement_type": eval_result.engagement_type,
        "priority": eval_result.priority,
        "action": eval_result.action,
        "reasoning": eval_result.reasoning,
    }
    prompt = create_response_prompt(tweet, eval_dict, config)

    # Get LLM response
    model = get_default_model()
    if not model:
        raise RuntimeError("default model not set")
    messages = [get_system_prompt(), Message("user", prompt)]
    response = reply(messages, model.full, stream=False)

    return parse_llm_response(response.content, TweetResponse, TaskType.RESPONSE)


def review_draft(draft: Dict) -> ReviewResponse:
    """Review draft using LLM"""
    config = load_config()
    prompt = create_review_prompt(draft, config)

    # Get LLM response
    model = get_default_model()
    if not model:
        raise RuntimeError("default model not set")
    messages = [get_system_prompt(), Message("user", prompt)]
    response = reply(messages, model.full, stream=False)

    return parse_llm_response(response.content, ReviewResponse, TaskType.REVIEW)


def process_tweet(tweet: Dict) -> Tuple[EvaluationResponse, Optional[TweetResponse]]:
    """Process a tweet through evaluation and response generation"""
    # Evaluate tweet
    eval_result = evaluate_tweet(tweet)
    console.print("Evaluation result:")
    console.print(eval_result)

    # Generate response if needed
    response = None
    if eval_result.action == "respond":
        response = generate_response(tweet, eval_result)

    return eval_result, response


def verify_draft(draft: Dict) -> Tuple[bool, ReviewResponse]:
    """Verify a draft tweet"""
    review_result = review_draft(draft)
    approved = review_result.recommendation == "approve"
    return approved, review_result
