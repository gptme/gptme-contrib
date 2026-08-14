# Financial Advisor Prompt Calibration

Implements a structured clarifying-question system that calibrates LLM financial advice by gathering context in the right order before generating recommendations.

## Problem

Generic financial advice is often unhelpful because it doesn't account for individual circumstances. The same recommendation (e.g., "invest in index funds") is wrong for someone who:
- Needs the money within 2 years (should use high-yield savings instead)
- Has high-interest credit card debt (should pay that off first for a guaranteed "return")
- Has no emergency fund (should build one before investing)

This skill implements research-backed question ordering (time horizon before risk tolerance, etc.) to gather the *right* context before giving advice.

## How It Works

The skill defines:

1. **Axes** (dimensions of context): time horizon, goal type, risk tolerance, country, debt status, emergency fund, amount
2. **Question Priority Order**: Research-backed ordering (time horizon matters more than risk tolerance for sequencing)
3. **Question Types**: Classify user questions ("Should I invest?", "How much to save?", etc.) and determine required context
4. **Early-Exit Rules**: If near-term horizon or high-interest debt exists, interrupt with appropriate advice first

### Core Module

`calibration.py` exports:
- `QuestionContext`: Dataclass tracking gathered context
- `classify_question()`: Classify a user question into a type
- `next_questions()`: Return the next 1-2 clarifying questions, or `None` if the debt-interrupt should fire, or `[]` if all required axes are gathered
- `generate_calibrated_prompt()`: Generate a structured LLM prompt incorporating gathered context

### Example Usage

```python
import sys
sys.path.insert(0, "skills/financial-advisor")  # adjust to your repo root
from calibration import (
    QuestionContext,
    TimeHorizon,
    GoalType,
    classify_question,
    next_questions,
    generate_calibrated_prompt,
)

# Start with empty context
ctx = QuestionContext()
question_type = classify_question("Should I invest in index funds?")

# Get the next questions to ask.
# Returns:
#   None       → high-interest debt interrupt: show payoff advice now
#   []         → all required context gathered: proceed to generate_calibrated_prompt
#   [str, ...] → questions remaining: ask the first one
next_qs = next_questions(ctx, question_type)
if next_qs is None:
    # Debt interrupt — give payoff advice instead of investment advice
    print("You have high-interest debt. Pay it off before investing.")
elif next_qs:
    # More clarifying questions needed
    print(next_qs[0])  # → "When do you need this money?"
else:
    # All axes gathered — generate calibrated prompt
    prompt = generate_calibrated_prompt("Should I invest in index funds?", ctx)

# Full example: gather context then generate prompt
ctx.time_horizon = TimeHorizon.LONG
ctx.goal_type = GoalType.WEALTH_GROWTH
ctx.has_high_interest_debt = False
ctx.has_emergency_fund = True
prompt = generate_calibrated_prompt("Should I invest in index funds?", ctx)
```

## Research Context

- Question ordering is based on MIT Sloan financial planning research
- Time horizon is the most important predictor of appropriate strategy
- High-interest debt payoff is mathematically superior to investing (guaranteed return)
- Emergency fund is a prerequisite for responsible investing (to avoid forced liquidation)

## Design Decisions

1. **Enums over strings**: TimeHorizon, GoalType, etc. are enums to ensure type safety and make the system extensible
2. **Question classification**: Classifies user questions into types to select the appropriate required axes
3. **Research-backed ordering**: AXIS_PRIORITY_ORDER follows financial planning best practices, not arbitrary order
4. **No LLM coupling**: Core module has zero dependencies on any LLM or provider (pure Python logic)

## Testing

Run the tests:
```bash
pytest skills/financial-advisor/tests/test_calibration.py -v
```

Test coverage includes:
- Context tracking and serialization
- Question classification for various question types
- Priority-ordered question sequencing
- Prompt generation with full and partial context
- Integration scenarios (young investor, near-retirement, high debt, etc.)

## Integration

To use this skill in gptme:
1. The calibration module can be imported directly
2. An LLM tool wrapper would accept a user question, manage the context gathering loop, and call `generate_calibrated_prompt()`
3. The generated prompt is passed to the LLM to get advice

This is a **decision-making accelerator**, not an end-to-end advice engine — it focuses on the crucial "ask the right questions first" step that generic LLMs skip.

## Related

- Idea #935: Financial advisor calibration
- Prior work: `scripts/financial-advisor-calibration.py` (prototype)
- Design: `knowledge/research/2026-08-03-financial-advisor-prompt-calibration.md`
