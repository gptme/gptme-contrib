"""
Financial Advisor Prompt Calibration Module (idea #935)

Implements a structured clarifying-question system that calibrates LLM financial
advice by gathering context in the right order before generating recommendations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class TimeHorizon(str, Enum):
    """Investment time horizon categories."""

    NEAR = "near"  # < 2 years
    MEDIUM = "medium"  # 2-10 years
    LONG = "long"  # 10+ years
    UNDEFINED = "undefined"


class GoalType(str, Enum):
    """Financial goal categories."""

    RETIREMENT = "retirement"
    PROPERTY = "property"
    EDUCATION = "education"
    EMERGENCY_FUND = "emergency_fund"
    WEALTH_GROWTH = "wealth_growth"
    OTHER = "other"


class RiskTolerance(str, Enum):
    """Risk tolerance levels."""

    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class CountryContext(str, Enum):
    """Country context for tax-advantaged accounts."""

    UK = "uk"
    US = "us"
    EU = "eu"
    OTHER = "other"
    UNSPECIFIED = "unspecified"


@dataclass
class QuestionContext:
    """Tracks what we know about the user's financial situation."""

    time_horizon: TimeHorizon | None = None
    goal_type: GoalType | None = None
    risk_tolerance: RiskTolerance | None = None
    country_context: CountryContext | None = None
    has_high_interest_debt: bool | None = None
    has_emergency_fund: bool | None = None
    amount_context: str | None = None  # "<10k", "10k-100k", "100k+", "unspecified"
    raw_answers: dict[str, str] = field(default_factory=dict)

    def missing_axes_for(self, question_type: str) -> list[str]:
        """Return axes still needed for the given question type, in priority order."""
        required = REQUIRED_AXES.get(question_type, REQUIRED_AXES["default"])
        missing = []
        for axis in AXIS_PRIORITY_ORDER:
            if axis not in required:
                continue
            if getattr(self, axis, None) is None:
                missing.append(axis)
        return missing

    def is_ready_to_advise(self, question_type: str = "default") -> bool:
        """Check if we have all required context to give advice."""
        return len(self.missing_axes_for(question_type)) == 0

    def should_interrupt_with_debt_advice(self) -> bool:
        """Check if high-interest debt should interrupt and take priority."""
        return self.has_high_interest_debt is True

    def to_dict(self) -> dict:
        """Convert context to serializable dict."""
        return {
            k: (v.value if isinstance(v, Enum) else v)
            for k, v in vars(self).items()
            if k != "raw_answers" and v is not None
        }


# Axes in the order they should be asked (priority order)
AXIS_PRIORITY_ORDER = [
    "time_horizon",
    "goal_type",
    "has_high_interest_debt",
    "has_emergency_fund",
    "risk_tolerance",
    "country_context",
    "amount_context",
]

# Required axes by question type
REQUIRED_AXES: dict[str, list[str]] = {
    "should_i_invest": ["time_horizon", "goal_type", "has_high_interest_debt"],
    "portfolio_allocation": [
        "time_horizon",
        "goal_type",
        "risk_tolerance",
        "country_context",
        "has_high_interest_debt",
    ],
    "specific_investment": ["time_horizon", "risk_tolerance", "has_high_interest_debt"],
    "how_much_to_save": ["goal_type", "time_horizon", "amount_context"],
    "emergency_fund": ["has_emergency_fund"],
    "default": ["time_horizon", "goal_type", "has_high_interest_debt"],
}

# Question text for each axis
AXIS_QUESTIONS: dict[str, str] = {
    "time_horizon": "When do you need this money? (e.g., within 2 years, 2-10 years, 10+ years away, or unsure)",
    "goal_type": "What is this money for? (e.g., retirement, buying property, education, emergency fund, growing wealth, or something else)",
    "has_high_interest_debt": "Do you currently have any high-interest debt, like credit cards or personal loans above ~10% APR?",
    "has_emergency_fund": "Do you have an emergency fund covering 3-6 months of expenses?",
    "risk_tolerance": "How would you feel if this investment lost 30% of its value in a year? (a) I'd panic and sell, (b) I'd be uncomfortable but hold on, (c) I'd see it as a buying opportunity",
    "country_context": "What country are you in? (This affects which tax-advantaged accounts are available to you)",
    "amount_context": "Roughly how much are we talking about? (under £/€/$10k, £/€/$10k-100k, over £/€/$100k, or prefer not to say)",
}


def classify_question(user_question: str) -> str:
    """Classify a user question into a question type."""
    q = user_question.lower()
    if any(
        kw in q for kw in ["portfolio", "allocation", "balance", "split", "distribute"]
    ):
        return "portfolio_allocation"
    if any(kw in q for kw in ["emergency", "rainy day", "safety net"]):
        return "emergency_fund"
    if any(kw in q for kw in ["how much", "save", "saving"]):
        return "how_much_to_save"
    if (
        any(kw in q for kw in ["specific", "stock", "etf", "bond", "reit"])
        and "should" in q
    ):
        return "specific_investment"
    return "should_i_invest"


def next_questions(
    ctx: QuestionContext, question_type: str, max_questions: int = 2
) -> list[str]:
    """Return the next questions to ask, up to max_questions."""
    missing = ctx.missing_axes_for(question_type)
    return [AXIS_QUESTIONS[axis] for axis in missing[:max_questions]]


def generate_calibrated_prompt(user_question: str, ctx: QuestionContext) -> str:
    """Generate a calibrated prompt incorporating gathered context."""
    context_str = (
        json.dumps(ctx.to_dict(), indent=2) if ctx.to_dict() else "None gathered yet"
    )

    question_type = classify_question(user_question)
    missing = ctx.missing_axes_for(question_type)

    missing_questions = [f"- {AXIS_QUESTIONS[a]}" for a in missing[:2]]
    missing_str = (
        "\n".join(missing_questions)
        if missing_questions
        else "None — proceed with advice"
    )

    prompt = f"""You are a financial planning assistant. Before giving advice, you ask specific
clarifying questions to understand the user's situation. You ask no more than 2 questions at a time.

USER'S ORIGINAL QUESTION:
{user_question}

CONTEXT GATHERED SO FAR:
{context_str}

NEXT CLARIFYING QUESTIONS TO ASK (ask these before giving advice):
{missing_str}

RULES:
- If "NEXT CLARIFYING QUESTIONS" is non-empty, ask those questions first. Do not give advice yet.
- If no questions remain, give concrete advice using this structure:
  1. Summary of their situation (2 sentences, referencing their specific context)
  2. Recommendation with rationale (tied to their time horizon, goals, risk tolerance)
  3. What to do first (one concrete action)
  4. One key risk or caveat

IMPORTANT EARLY-EXIT RULES:
- If time_horizon = "near" (< 2 years): Do NOT recommend stock market investments. Recommend
  high-yield savings accounts, money market funds, or short-term bonds instead.
- If has_high_interest_debt = true: Recommend paying off that debt FIRST before investing.
  (Paying off 20%+ APR debt is a guaranteed 20% return — better than any index fund.)
- If has_emergency_fund = false: Recommend building emergency fund first
  (3-6 months expenses in a HYSA) before any investing.

Do not give generic advice. Every recommendation must be traceable to the gathered context."""

    return prompt
