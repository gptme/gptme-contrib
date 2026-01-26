"""
Lesson variant generation for GEPA-lite evolution loop.

Generates multiple lesson variants using different prompts and strategies,
enabling exploration of the lesson quality space.
"""

from typing import Dict, List

from gptme.llm import reply
from gptme.message import Message


def generate_lesson_variants(
    moment: Dict,
    conversation_id: str,
    num_variants: int = 5,
    temperature: float = 0.7,
) -> List[str]:
    """Generate multiple lesson variants using different prompt strategies.

    Creates diverse lessons by varying:
    - Focus areas (signals vs patterns, detection vs enforcement)
    - Temperature (exploration vs exploitation)
    - Framing (cautionary vs prescriptive)

    Args:
        moment: Experience dict with title, context, evidence, etc.
        conversation_id: ID of source conversation
        num_variants: Number of variants to generate (default: 5)
        temperature: Base temperature for LLM (default: 0.7)

    Returns:
        List of lesson markdown strings
    """
    # Read template using helper from llm module
    from gptme_lessons_extras.utils.llm import _find_lesson_template

    template_path = _find_lesson_template()

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    # Prepare base context
    evidence = "\n\n".join(moment.get("evidence_snippets", [])[:3])
    metrics = moment.get("metrics", {})

    # Define different prompt strategies
    strategies = [
        {
            "name": "detection-focused",
            "emphasis": "Focus heavily on creating grep-able failure signals and observable symptoms. Make the anti-pattern smell very specific and detectable.",
            "temp_adjust": 0.0,
        },
        {
            "name": "enforcement-focused",
            "emphasis": "Prioritize automation hooks and verification checklists. Include specific pre-commit checks, CI integration, and grep patterns.",
            "temp_adjust": 0.0,
        },
        {
            "name": "pattern-focused",
            "emphasis": "Emphasize the recommended pattern with clear before/after examples. Make the fix recipe very practical and step-by-step.",
            "temp_adjust": 0.1,
        },
        {
            "name": "cautionary",
            "emphasis": "Frame as a cautionary tale - what goes wrong and why. Make failure signals vivid and memorable.",
            "temp_adjust": 0.2,
        },
        {
            "name": "prescriptive",
            "emphasis": "Frame as clear guidance on best practices. Focus on what TO do rather than what NOT to do.",
            "temp_adjust": 0.2,
        },
        {
            "name": "balanced",
            "emphasis": "Balance all aspects: detection, enforcement, patterns, and rationale. Aim for comprehensive coverage.",
            "temp_adjust": 0.1,
        },
    ]

    # Select strategies based on num_variants
    selected_strategies = strategies[:num_variants]

    variants = []

    for strategy in selected_strategies:
        # Build user prompt with strategy-specific emphasis
        user_prompt = f"""Generate a lesson from this experience:

**Title**: {moment.get("title", "Unknown")}
**Context**: {moment.get("context", "")}
**What Changed**: {moment.get("what_changed", "")}
**Rationale**: {moment.get("rationale", "")}

**Metrics**:
- Errors: {metrics.get("errors", 0)}
- Retry depth: {metrics.get("retry_depth", 0)}
- Duration: {metrics.get("duration_min", 0):.2f} min
- Tool invocations: {metrics.get("tool_invocations", 0)}

**Evidence snippets**:
{evidence}

**Source**: Conversation {conversation_id}

**Strategy**: {strategy["emphasis"]}

Follow this template structure:
{template}

Generate a complete lesson following the template format. {strategy["emphasis"]}

Keep examples minimal and focused on the "smell" rather than full incorrect examples.

**IMPORTANT**: Return ONLY the lesson markdown, starting directly with the YAML frontmatter (---).
Do not include any preamble, introduction, or explanation before the frontmatter.
The very first characters of your response must be the opening "---" of the YAML frontmatter.
"""

        system_prompt = """You are an expert at extracting actionable lessons from software development experiences.

Your task is to transform observations into structured lessons that:
1. Prevent similar mistakes
2. Can be automatically detected
3. Provide clear remediation steps
4. Include verification mechanisms

Be specific, concrete, and actionable."""

        messages = [Message("system", system_prompt), Message("user", user_prompt)]

        # Note: reply() doesn't support temperature parameter
        # Variation comes from different prompt strategies instead
        response = reply(
            messages,
            model="anthropic/claude-3-5-sonnet-20241022",
            stream=True,
        )

        # Extract content
        if isinstance(response, list):
            content = response[-1].content
        else:
            content = response.content

        lesson_markdown = str(content) if content is not None else ""

        if lesson_markdown:
            variants.append(lesson_markdown)

    return variants
