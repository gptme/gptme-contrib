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
    UNDEFINED = "undefined"  # user said "unsure" — treated as not-yet-answered


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
    UNSPECIFIED = (
        "unspecified"  # user said "prefer not to say" — treated as not-yet-answered
    )


# Sentinel values meaning "user said unsure / prefer not to say".
# These are non-None, but should NOT satisfy a missing-axis check — a user
# answering "I don't know" for time_horizon stores UNDEFINED, which must still
# be treated as not-yet-answered so the LLM can probe further.
#
# The plain string "unspecified" covers amount_context (a str field, not an Enum),
# which stores that literal when the user picks "prefer not to say". Without it,
# missing_axes_for would count amount_context as gathered and skip the probe.
#
# Both enum members AND their .value strings are included: to_dict() serializes
# to strings, and a caller reconstructing from JSON gets plain strings. Although
# str, Enum equality makes the check work without the strings, including them
# explicitly avoids dependence on that subtlety and guards future refactors.
_UNANSWERED_SENTINELS: frozenset[object] = frozenset(
    [TimeHorizon.UNDEFINED, CountryContext.UNSPECIFIED, "undefined", "unspecified"]
)


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
        """Return axes still needed for the given question type, in priority order.

        An axis is considered missing if its value is None OR is a sentinel meaning
        "user didn't know" (e.g. TimeHorizon.UNDEFINED, CountryContext.UNSPECIFIED).
        Treating sentinels as present would cause the LLM to generate advice for an
        unknown time horizon — e.g. recommending equities to someone who said "unsure"
        about their horizon, which could be near-term.
        """
        required = REQUIRED_AXES.get(question_type, REQUIRED_AXES["default"])
        missing = []
        for axis in AXIS_PRIORITY_ORDER:
            if axis not in required:
                continue
            val = getattr(self, axis, None)
            if val is None or val in _UNANSWERED_SENTINELS:
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
    # Note: "balance" alone is intentionally excluded — it is too broad and matches
    # "account balance" or "balance my budget", causing misclassification.
    # "rebalance" is included because it is unambiguously portfolio-related.
    if any(
        kw in q
        for kw in [
            "portfolio",
            "allocation",
            "rebalance",
            "balance my portfolio",
            "split",
            "distribute",
        ]
    ):
        return "portfolio_allocation"
    # Check specific-instrument keywords first so "How much should I invest in ETFs?"
    # routes to specific_investment rather than how_much_to_save.  The "how much"
    # branch is intentionally broad; without this guard it swallows questions that
    # also contain ETF/stock/bond identifiers, routing them to a path that collects
    # amount_context instead of risk_tolerance and time_horizon.
    if (
        any(kw in q for kw in ["specific", "stock", "etf", "bond", "reit"])
        and "should" in q
    ):
        return "specific_investment"
    # Check savings-amount keywords before emergency keywords so that "How much should I
    # save for a rainy day?" routes to how_much_to_save (which gathers amount_context)
    # rather than emergency_fund (which only gathers has_emergency_fund).
    if any(kw in q for kw in ["how much", "save", "saving"]):
        return "how_much_to_save"
    if any(kw in q for kw in ["emergency", "rainy day", "safety net"]):
        return "emergency_fund"
    return "should_i_invest"


def next_questions(
    ctx: QuestionContext, question_type: str, max_questions: int = 2
) -> list[str]:
    """Return the next questions to ask, up to max_questions.

    Returns an empty list when the debt-interrupt early-exit should fire — the
    caller must handle that case (e.g. by routing to generate_calibrated_prompt
    or by showing the debt-payoff message directly) rather than asking further
    clarifying questions.
    """
    if ctx.should_interrupt_with_debt_advice():
        return []
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

    # High-interest debt is an early-exit interrupt: the debt-payoff advice ("pay off
    # 20%+ APR first") is always correct and takes priority over gathering more context.
    # A user who reports high-interest debt must receive that guidance IMMEDIATELY —
    # asking further questions about time horizon or risk tolerance first degrades the
    # skill's core value proposition and contradicts the stated early-exit behaviour.
    if ctx.should_interrupt_with_debt_advice():
        questions_block = "NEXT CLARIFYING QUESTIONS: (none — high-interest debt interrupt takes priority)"
        action_instruction = (
            "INTERRUPT: The user has high-interest debt. "
            "Advise them to pay it off FIRST before any investing. "
            "Do NOT ask further clarifying questions. "
            "(Paying off 20%+ APR debt is a guaranteed 20%+ return — better than any index fund.)"
        )
    elif missing_questions:
        # Still gathering context — tell the LLM exactly what to ask next.
        questions_block = (
            "NEXT CLARIFYING QUESTIONS TO ASK (ask these before giving advice):\n"
            + "\n".join(missing_questions)
        )
        action_instruction = (
            "Ask the 1-2 questions listed above. Do NOT give investment advice yet."
        )
    else:
        # All required context has been gathered — tell the LLM to proceed.
        # We do NOT use a non-empty placeholder here; that would cause the LLM to
        # treat "None — proceed with advice" as a question and defer advice anyway.
        questions_block = (
            "NEXT CLARIFYING QUESTIONS: (none — all required context gathered)"
        )
        action_instruction = (
            "All required context has been gathered. Give concrete advice now."
        )

    # Build early-exit rules only for axes that are actually in the gathered context.
    # Unconditional rules cause the LLM to receive conditions it cannot evaluate —
    # e.g. `has_emergency_fund = false` appears in the prompt even when that axis
    # was never gathered (it is not required for `should_i_invest` questions), so
    # the LLM is told "give advice now" with an unfalsifiable rule still active.
    ctx_dict = ctx.to_dict()
    early_exit_lines = []
    if "time_horizon" in ctx_dict:
        early_exit_lines.append(
            '- If time_horizon = "near" (< 2 years): Do NOT recommend stock market investments.'
            " Recommend high-yield savings accounts, money market funds, or short-term bonds instead."
        )
    if "has_high_interest_debt" in ctx_dict:
        early_exit_lines.append(
            "- If has_high_interest_debt = true: Recommend paying off that debt FIRST before investing."
            " (Paying off 20%+ APR debt is a guaranteed 20% return — better than any index fund.)"
        )
    if "has_emergency_fund" in ctx_dict:
        early_exit_lines.append(
            "- If has_emergency_fund = false: Recommend building emergency fund first"
            " (3-6 months expenses in a HYSA) before any investing."
        )
    early_exit_section = (
        "IMPORTANT EARLY-EXIT RULES:\n" + "\n".join(early_exit_lines)
        if early_exit_lines
        else "IMPORTANT EARLY-EXIT RULES: (none — no conditional axes gathered yet)"
    )

    prompt = f"""You are a financial planning assistant. Before giving advice, you ask specific
clarifying questions to understand the user's situation. You ask no more than 2 questions at a time.

USER'S ORIGINAL QUESTION:
{user_question}

CONTEXT GATHERED SO FAR:
{context_str}

{questions_block}

ACTION: {action_instruction}

ADVICE STRUCTURE (use when all context is gathered):
  1. Summary of their situation (2 sentences, referencing their specific context)
  2. Recommendation with rationale (tied to their time horizon, goals, risk tolerance)
  3. What to do first (one concrete action)
  4. One key risk or caveat

{early_exit_section}

Do not give generic advice. Every recommendation must be traceable to the gathered context."""

    return prompt
