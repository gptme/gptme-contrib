"""Tests for the financial advisor calibration module."""

import sys
from pathlib import Path

# Add parent directory to path for relative imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from calibration import (
    AXIS_QUESTIONS,
    CountryContext,
    GoalType,
    QuestionContext,
    RiskTolerance,
    TimeHorizon,
    classify_question,
    generate_calibrated_prompt,
    next_questions,
)


class TestQuestionContext:
    """Test QuestionContext data class."""

    def test_missing_axes_for_should_i_invest(self):
        """Test missing axes for 'should_i_invest' question type."""
        ctx = QuestionContext()
        missing = ctx.missing_axes_for("should_i_invest")
        assert "time_horizon" in missing
        assert "goal_type" in missing
        assert "has_high_interest_debt" in missing

    def test_is_ready_to_advise_when_missing_context(self):
        """Test that context is not ready when axes are missing."""
        ctx = QuestionContext(time_horizon=TimeHorizon.LONG)
        assert not ctx.is_ready_to_advise("should_i_invest")

    def test_is_ready_to_advise_when_complete(self):
        """Test that context is ready when all required axes are present."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.WEALTH_GROWTH,
            has_high_interest_debt=False,
        )
        assert ctx.is_ready_to_advise("should_i_invest")

    def test_undefined_time_horizon_still_counts_as_missing(self):
        """TimeHorizon.UNDEFINED must not satisfy the time_horizon axis (P1 fix).

        A user who answers "I don't know" gets UNDEFINED stored, but the system
        must keep asking — advising with unknown horizon risks recommending equities
        to someone who actually needs money in 6 months.
        """
        ctx = QuestionContext(time_horizon=TimeHorizon.UNDEFINED)
        missing = ctx.missing_axes_for("should_i_invest")
        assert "time_horizon" in missing, "UNDEFINED should still be treated as missing"
        assert not ctx.is_ready_to_advise("should_i_invest")

    def test_unspecified_country_still_counts_as_missing(self):
        """CountryContext.UNSPECIFIED must not satisfy the country_context axis (P1 fix)."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.RETIREMENT,
            risk_tolerance=RiskTolerance.MODERATE,
            country_context=CountryContext.UNSPECIFIED,
            has_high_interest_debt=False,
        )
        missing = ctx.missing_axes_for("portfolio_allocation")
        assert (
            "country_context" in missing
        ), "UNSPECIFIED should still be treated as missing"
        assert not ctx.is_ready_to_advise("portfolio_allocation")

    def test_unspecified_amount_context_still_counts_as_missing(self):
        """amount_context='unspecified' must not satisfy the amount_context axis (P1 fix).

        A user who says 'prefer not to say' to the amount question stores the plain
        string "unspecified" in amount_context.  Without "unspecified" in
        _UNANSWERED_SENTINELS, missing_axes_for would count amount_context as gathered
        and skip further probing, producing advice with no knowledge of the amount.
        """
        ctx = QuestionContext(
            goal_type=GoalType.WEALTH_GROWTH,
            time_horizon=TimeHorizon.LONG,
            amount_context="unspecified",  # user said "prefer not to say"
        )
        missing = ctx.missing_axes_for("how_much_to_save")
        assert (
            "amount_context" in missing
        ), '"unspecified" amount_context should still be treated as missing'
        assert not ctx.is_ready_to_advise("how_much_to_save")

    def test_should_interrupt_with_debt_advice(self):
        """Test debt interrupt detection."""
        ctx_no_debt = QuestionContext(has_high_interest_debt=False)
        assert not ctx_no_debt.should_interrupt_with_debt_advice()

        ctx_with_debt = QuestionContext(has_high_interest_debt=True)
        assert ctx_with_debt.should_interrupt_with_debt_advice()

    def test_to_dict_serialization(self):
        """Test conversion to dict with enum serialization."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.RETIREMENT,
            has_high_interest_debt=False,
        )
        d = ctx.to_dict()
        assert d["time_horizon"] == "long"
        assert d["goal_type"] == "retirement"
        assert d["has_high_interest_debt"] is False
        assert "raw_answers" not in d  # raw_answers should be filtered out


class TestClassifyQuestion:
    """Test question classification logic."""

    def test_classify_portfolio_question(self):
        """Test classification of portfolio allocation questions."""
        assert (
            classify_question("How should I allocate my portfolio?")
            == "portfolio_allocation"
        )
        # "balance" alone is excluded (too broad — matches "account balance", "balance budget").
        # Portfolio-specific phrasings must still classify correctly.
        assert (
            classify_question("How should I rebalance my portfolio?")
            == "portfolio_allocation"
        )
        assert (
            classify_question("What's the best way to balance my portfolio?")
            == "portfolio_allocation"
        )

    def test_classify_emergency_fund_question(self):
        """Test classification of emergency fund questions."""
        assert classify_question("Do I need an emergency fund?") == "emergency_fund"
        assert classify_question("Should I build a safety net?") == "emergency_fund"

    def test_classify_saving_question(self):
        """Test classification of saving amount questions."""
        assert (
            classify_question("How much should I save each month?")
            == "how_much_to_save"
        )
        # Ambiguous: contains "rainy day" (emergency keyword) AND "how much" (saving keyword).
        # Must resolve to how_much_to_save because the question is about saving *amount*.
        assert (
            classify_question("How much should I save for a rainy day?")
            == "how_much_to_save"
        )

    def test_classify_specific_investment_question(self):
        """Test classification of specific investment questions."""
        assert classify_question("Should I buy this ETF?") == "specific_investment"
        assert (
            classify_question("Should I invest in this stock?") == "specific_investment"
        )

    def test_classify_generic_invest_question(self):
        """Test classification of generic investment questions."""
        assert classify_question("Should I invest in index funds?") == "should_i_invest"

    def test_classify_etf_question_not_how_much(self):
        """'How much should I invest in ETFs?' must route to specific_investment, not how_much_to_save.

        The question contains 'how much' (a how_much_to_save keyword) AND 'etf'
        (a specific_investment keyword). Without the specific_investment check being
        evaluated first, it routes to how_much_to_save and asks about amount_context
        instead of risk_tolerance and time_horizon (P2 regression guard).
        """
        result = classify_question("How much should I invest in ETFs?")
        assert result == "specific_investment", (
            f"Expected specific_investment, got {result!r}. "
            "The 'how much' check must not shadow ETF investment questions."
        )


class TestNextQuestions:
    """Test question ordering logic."""

    def test_next_questions_empty_context(self):
        """Test that we get prioritized questions with empty context."""
        ctx = QuestionContext()
        questions = next_questions(ctx, "should_i_invest", max_questions=2)
        assert len(questions) <= 2
        assert AXIS_QUESTIONS["time_horizon"] in questions

    def test_next_questions_respects_priority(self):
        """Test that questions follow AXIS_PRIORITY_ORDER."""
        ctx = QuestionContext(time_horizon=TimeHorizon.LONG)
        questions = next_questions(ctx, "should_i_invest")
        # Should ask goal_type next (after time_horizon)
        assert AXIS_QUESTIONS["goal_type"] in questions

    def test_next_questions_max_limit(self):
        """Test that max_questions is respected."""
        ctx = QuestionContext()
        questions = next_questions(ctx, "should_i_invest", max_questions=1)
        assert len(questions) == 1

    def test_next_questions_empty_when_debt_interrupt(self):
        """next_questions must return [] when high-interest debt is flagged.

        A caller driving a clarifying-question loop via next_questions must not
        keep asking about time_horizon / goal_type when the debt interrupt should
        fire.  Returning [] signals the caller to show the debt-payoff message
        instead of asking further questions (P1 regression guard).
        """
        ctx = QuestionContext(has_high_interest_debt=True)
        # Even with missing required axes (time_horizon, goal_type), must return [].
        assert ctx.should_interrupt_with_debt_advice()
        assert ctx.missing_axes_for("should_i_invest")  # sanity: axes ARE missing
        questions = next_questions(ctx, "should_i_invest")
        assert questions == [], (
            "next_questions must return [] when debt interrupt is active, "
            "not a list of clarifying questions"
        )


class TestGenerateCalibratedPrompt:
    """Test prompt generation."""

    def test_prompt_with_full_context(self):
        """Test prompt generation with complete context — should instruct LLM to give advice."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.WEALTH_GROWTH,
            has_high_interest_debt=False,
            has_emergency_fund=True,
            risk_tolerance=RiskTolerance.MODERATE,
            country_context=CountryContext.UK,
        )
        prompt = generate_calibrated_prompt("Should I invest in index funds?", ctx)
        assert "index funds" in prompt.lower()
        assert "long" in prompt.lower()
        # When all context is gathered the ACTION must say "give advice now",
        # NOT defer with a non-empty questions placeholder (P1 fix).
        assert "all required context gathered" in prompt.lower()
        assert "give concrete advice now" in prompt.lower()
        # Must NOT contain a non-empty questions block that could trigger deferral.
        assert "Do NOT give investment advice yet" not in prompt

    def test_prompt_with_partial_context(self):
        """Test prompt generation with incomplete context."""
        ctx = QuestionContext(time_horizon=TimeHorizon.LONG)
        prompt = generate_calibrated_prompt("Should I invest?", ctx)
        assert "NEXT CLARIFYING QUESTIONS" in prompt
        assert "goal_type" in prompt or "What is this money for" in prompt

    def test_prompt_includes_context_data(self):
        """Test that prompt includes the actual gathered context."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.MEDIUM,
            has_high_interest_debt=True,
        )
        prompt = generate_calibrated_prompt("Should I invest?", ctx)
        assert "medium" in prompt.lower()
        assert "true" in prompt.lower()  # debt flag

    def test_prompt_early_exit_for_near_horizon(self):
        """Test that near-horizon time horizon gets appropriate guidance."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.NEAR,
            goal_type=GoalType.PROPERTY,
            has_high_interest_debt=False,
        )
        prompt = generate_calibrated_prompt(
            "Should I invest for a house down payment?", ctx
        )
        # Prompt should mention the early-exit rules
        assert "EARLY-EXIT RULES" in prompt or "near" in prompt.lower()

    def test_prompt_debt_interrupt_guidance(self):
        """Test that high-interest debt gets appropriate priority."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.WEALTH_GROWTH,
            has_high_interest_debt=True,
        )
        prompt = generate_calibrated_prompt("Should I invest?", ctx)
        assert "EARLY-EXIT RULES" in prompt
        assert "debt" in prompt.lower()

    def test_prompt_debt_interrupt_fires_with_missing_axes(self):
        """High-interest debt must interrupt even when other required axes are missing.

        This is the P1 regression guard: should_interrupt_with_debt_advice() must
        be called in generate_calibrated_prompt so a user with high-interest debt
        gets immediate payoff advice — not a question about time horizon first.
        """
        # Only has_high_interest_debt is set; time_horizon and goal_type are missing.
        ctx = QuestionContext(has_high_interest_debt=True)
        assert ctx.should_interrupt_with_debt_advice()
        assert not ctx.is_ready_to_advise("should_i_invest")

        prompt = generate_calibrated_prompt("Should I invest my savings?", ctx)
        # Must NOT ask clarifying questions despite missing axes.
        assert "Do NOT give investment advice yet" not in prompt
        assert "Ask the 1-2 questions listed above" not in prompt
        # Must INTERRUPT with debt advice.
        assert "INTERRUPT" in prompt or "pay" in prompt.lower()
        assert "debt" in prompt.lower()

    def test_early_exit_rules_only_for_gathered_axes(self):
        """Early-exit rules must only appear for axes that are present in context.

        For should_i_invest, has_emergency_fund is not required. When we are ready
        to advise without it, the emergency-fund rule must NOT be in the prompt —
        the LLM cannot evaluate a condition for a value it was never given.
        """
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.WEALTH_GROWTH,
            has_high_interest_debt=False,
            # has_emergency_fund deliberately not set — not required for this type
        )
        assert ctx.is_ready_to_advise("should_i_invest")
        prompt = generate_calibrated_prompt("Should I invest?", ctx)
        # Emergency fund rule must not appear — axis was never gathered
        assert "emergency fund" not in prompt.lower(), (
            "Emergency-fund early-exit rule appeared in prompt even though "
            "has_emergency_fund was never gathered for this question type"
        )
        # Time horizon and debt rules ARE present because those axes were gathered
        assert "near" in prompt.lower()
        assert "debt" in prompt.lower()


class TestIntegrationScenarios:
    """Integration tests for realistic scenarios."""

    def test_young_investor_scenario(self):
        """Test scenario: young investor with no debt."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.WEALTH_GROWTH,
            has_high_interest_debt=False,
            has_emergency_fund=True,
            risk_tolerance=RiskTolerance.MODERATE,
            country_context=CountryContext.UK,
        )
        assert ctx.is_ready_to_advise("portfolio_allocation")
        prompt = generate_calibrated_prompt("How should I allocate my portfolio?", ctx)
        assert "long" in prompt.lower()
        assert "wealth_growth" in prompt or "wealth growth" in prompt.lower()

    def test_near_retirement_scenario(self):
        """Test scenario: near-retirement with short time horizon."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.NEAR,
            goal_type=GoalType.PROPERTY,
            has_high_interest_debt=False,
            has_emergency_fund=True,
        )
        assert ctx.is_ready_to_advise("should_i_invest")
        prompt = generate_calibrated_prompt(
            "Should I invest for a house down payment?", ctx
        )
        assert "near" in prompt.lower()

    def test_credit_card_debt_scenario(self):
        """Test scenario: user with high-interest debt."""
        ctx = QuestionContext(
            time_horizon=TimeHorizon.LONG,
            goal_type=GoalType.RETIREMENT,
            has_high_interest_debt=True,
        )
        assert ctx.should_interrupt_with_debt_advice()
        prompt = generate_calibrated_prompt("How should I invest for retirement?", ctx)
        assert "debt" in prompt.lower()

    def test_no_context_scenario(self):
        """Test scenario: first question with no prior context."""
        ctx = QuestionContext()
        assert not ctx.is_ready_to_advise()
        missing = ctx.missing_axes_for("should_i_invest")
        assert len(missing) > 0
        questions = next_questions(ctx, "should_i_invest")
        assert len(questions) > 0
        assert AXIS_QUESTIONS["time_horizon"] in questions
