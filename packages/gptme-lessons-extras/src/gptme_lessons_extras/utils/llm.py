"""
LLM integration helpers for the learning system.

Contains functions for LLM-based lesson generation and scoring.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, cast

from gptme.llm import reply
from gptme.message import Message


def extract_json_from_response(content: str) -> str:
    """
    Extract JSON string from LLM response, handling code blocks.

    Args:
        content: Raw content from LLM response

    Returns:
        Extracted JSON string
    """
    content_str = str(content)

    if "```json" in content_str:
        json_start = content_str.index("```json") + 7
        json_end = content_str.index("```", json_start)
        return content_str[json_start:json_end].strip()
    elif "```" in content_str:
        json_start = content_str.index("```") + 3
        json_end = content_str.index("```", json_start)
        return content_str[json_start:json_end].strip()
    else:
        return content_str.strip()


def llm_summarize_episode(
    episode: Dict,
    messages: List[Dict],
    temperature: float = 0.7,
) -> Dict:
    """Summarize what actually happened in an episode using LLM.

    Instead of generic "struggle" or "breakthrough", extract:
    - What was being worked on (specific task/problem)
    - What tools/approaches were used
    - What the key insight or solution was
    - What pattern or principle emerged

    Args:
        episode: Episode dict with start_index, end_index, kind, etc.
        messages: Full conversation messages
        temperature: LLM temperature

    Returns:
        Enhanced episode dict with 'summary' field containing:
        - task: What was being worked on
        - approach: Tools/methods used
        - insight: Key learning or solution
        - pattern: Generalizable principle
    """
    start_idx = episode.get("start_index", 0)
    end_idx = episode.get("end_index", len(messages) - 1)

    # Extract relevant messages
    episode_messages = messages[start_idx : end_idx + 1]

    # Build context string
    context_parts = []
    for msg in episode_messages[:10]:  # Limit to first 10 for token efficiency
        role = msg.get("role", "unknown")
        content = str(msg.get("content", ""))[:500]  # Truncate long messages
        context_parts.append(f"[{role}] {content}")

    context = "\n\n".join(context_parts)

    system_prompt = """You are analyzing a conversation episode to extract specific learnings.

Your task is to identify:
1. **Task**: What specific problem or goal was being worked on
2. **Approach**: What tools, methods, or strategies were used
3. **Insight**: What was the key learning, solution, or realization
4. **Pattern**: What generalizable principle or pattern emerged

Focus on SPECIFICS, not generic observations.

Examples:
- Bad: "Used tools to solve problem"
- Good: "Used patch tool to incrementally modify Python files, avoiding rewriting entire modules"

- Bad: "Encountered and fixed error"
- Good: "Resolved mypy type error by adding explicit None checks in union-find clustering logic"

Return ONLY valid JSON with this structure:
```json
{
  "task": "Specific task/problem being worked on",
  "approach": "Tools/methods/strategies used",
  "insight": "Key learning or solution",
  "pattern": "Generalizable principle"
}
```"""

    user_prompt = f"""Analyze this conversation episode and extract specific learnings:

Episode type: {episode.get("kind", "unknown")}
Episode title: {episode.get("title", "unknown")}

Conversation excerpt:
{context}

What was actually happening here? What was learned?

Return ONLY valid JSON."""

    messages_llm = [Message("system", system_prompt), Message("user", user_prompt)]

    try:
        response = reply(
            messages_llm, model="anthropic/claude-3-5-haiku-20241022", stream=True
        )

        if isinstance(response, list):
            content = response[-1].content
        else:
            content = response.content

        # Parse JSON from response
        json_str = extract_json_from_response(content)

        summary = cast(Dict[str, str], json.loads(json_str))

        # Enhance episode with summary
        enhanced_episode = episode.copy()
        enhanced_episode["summary"] = summary

        return enhanced_episode

    except Exception as e:
        # Return episode unchanged if summarization fails
        print(f"Warning: Failed to summarize episode: {e}")
        return episode


def _find_lesson_template() -> Path:
    """Find the lesson template file, checking multiple locations."""
    import os

    # Try 1: Environment variable (workspace root)
    workspace = os.environ.get("GPTME_WORKSPACE") or os.environ.get("AGENT_WORKSPACE")
    if workspace:
        template_path = Path(workspace) / "lessons/templates/lesson-template.md"
        if template_path.exists():
            return template_path

    # Try 2: Agent's default workspace
    agent_workspace = Path.home() / "workspace" / "lessons/templates/lesson-template.md"
    if agent_workspace.exists():
        return agent_workspace

    # Try 3: Relative to package (for bundled template)
    package_template = Path(__file__).parent.parent / "templates/lesson-template.md"
    if package_template.exists():
        return package_template

    raise FileNotFoundError(
        "Lesson template not found. Set GPTME_WORKSPACE environment variable "
        "to point to your workspace root, or ensure template exists at "
        "~/workspace/lessons/templates/lesson-template.md"
    )


def llm_author_reflect(
    moment: Dict, conversation_id: str, temperature: float = 0.7
) -> str:
    """Generate a lesson using LLM reflection on a experience.

    Uses the new signals-first template format with ENHANCED evidence integration.
    """
    # Read the new lesson template
    template_path = _find_lesson_template()

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    # Prepare evidence with enhanced formatting
    evidence_snippets = moment.get("evidence_snippets", [])[:3]
    evidence_lines = []
    evidence_lines.append("**Evidence Snippets** (actual trajectory fragments):\n")
    for i, snippet in enumerate(evidence_snippets, 1):
        evidence_lines.append(f"[Evidence {i}]")
        evidence_lines.append("```")
        evidence_lines.append(snippet.strip())
        evidence_lines.append("```\n")
    evidence_lines.append(
        "**KEY REQUIREMENT**: You MUST reference specific evidence snippets in your lesson."
    )
    evidence_lines.append(
        "Use format: [Evidence 1], [Evidence 2], etc. to cite specific snippets."
    )
    evidence_lines.append(
        "Quote actual error messages, commands, or outputs from the evidence.\n"
    )
    evidence = "\n".join(evidence_lines)

    metrics = moment.get("metrics", {})

    system_prompt = (
        """You are an expert at extracting actionable lessons from agent trajectories.

🎯 **PRIMARY REQUIREMENT: EVIDENCE INTEGRATION**

You MUST incorporate the provided evidence snippets throughout the lesson. This is the MOST IMPORTANT criterion.

**How to Integrate Evidence** (Examples of >0.90 score):

1. **In Failure Signals**: Quote actual error messages from evidence
   - Error: "cd: /home/user/Programming/project: No such file" [Evidence 1]

2. **In Anti-pattern**: Show actual failing commands from evidence
   # smell: from [Evidence 1]
   cd /home/user/Programming/project  # WRONG: caused error

3. **In Recommended Pattern**: Reference evidence showing correct approach
   # correct: from [Evidence 2]
   cd /home/user/project  # RIGHT: this worked

4. **In Rationale**: Cite specific metrics from evidence
   This issue occurred 15+ times [Evidence 1], causing errors [Evidence 2]

**Requirements**:
- **Detection over description**: Observable signals that indicate this issue
- **Enforcement**: How can this be verified or automated
- **Brevity**: Keep examples ≤5 lines
- **Actionability**: Clear steps from anti-pattern to recommended pattern
- **Evidence Integration**: Use [Evidence N] citations, quote actual errors/commands

Template format:
"""
        + template
    )

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

{evidence}

**Source**: Conversation {conversation_id}

🎯 **CRITICAL**: You MUST use [Evidence 1], [Evidence 2], [Evidence 3] citations throughout.
Quote actual error messages, commands, and outputs from the evidence snippets.
Score >0.90 requires direct evidence integration, not just vague references.

Generate a complete lesson following the template format. Focus on:
1. A clear, one-sentence imperative Rule
2. Observable Failure Signals (with evidence quotes and [Evidence N] citations)
3. A concise Anti-pattern smell snippet (from evidence, cite [Evidence N])
4. A minimal Recommended Pattern (from evidence, cite [Evidence N])
5. A practical Fix Recipe (3-5 steps)
6. Automation Hooks (grep patterns, pre-commit checks if applicable)

Keep examples minimal and focused on the "smell" rather than full incorrect examples.

**IMPORTANT**: Return ONLY the lesson markdown, starting directly with the YAML frontmatter (---).
Do not include any preamble, introduction, or explanation before the frontmatter.
The very first characters of your response must be the opening "---" of the YAML frontmatter.
"""

    messages = [Message("system", system_prompt), Message("user", user_prompt)]

    # Generate lesson with LLM (use streaming to avoid timeout)
    response = reply(
        messages, model="anthropic/claude-3-5-sonnet-20241022", stream=True
    )

    # reply() with stream=True returns a list of messages
    if isinstance(response, list):
        content = response[-1].content
    else:
        content = response.content

    return str(content) if content is not None else ""


def llm_judge_score(
    lesson_markdown: str,
    moment: Dict,
    conversation_id: str,
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """Score a lesson using LLM-as-judge.

    Scores on 6 dimensions (0.0-1.0 scale):
    - correctness: rule aligns with evidence/context
    - specificity: concrete, not generic
    - detectability: failure signals grep-able; snippet short
    - enforceability: has checklist + automation hooks
    - brevity: no bloat; ≤5-line snippets
    - evidence_use: uses provided evidence

    Returns dict with:
    - scores: dict of dimension -> float (0-1)
    - rationale: string explaining scores
    - notes: additional observations
    """
    # Prepare context
    evidence = "\n\n".join(moment.get("evidence_snippets", [])[:3])
    metrics = moment.get("metrics", {})

    system_prompt = """You are a strict evaluator scoring lessons on multi-objective criteria.

Your task is to score a lesson following these criteria (0.0-1.0 scale):

1. **correctness** (0.0-1.0): Does the rule align with the evidence and context?
   - 1.0: Rule directly follows from evidence; no logical leaps
   - 0.5: Rule is plausible but not strongly supported
   - 0.0: Rule contradicts evidence or is unrelated

2. **specificity** (0.0-1.0): How concrete and actionable is the lesson?
   - 1.0: Specific tools/commands/patterns named; no generic platitudes
   - 0.5: Somewhat specific but could apply to many situations
   - 0.0: Generic advice that could apply anywhere

3. **detectability** (0.0-1.0): Are failure signals observable and grep-able?
   - 1.0: Has ≥2 specific signals; includes grep patterns or clear error messages
   - 0.5: Has signals but they're vague or hard to detect
   - 0.0: No failure signals or purely subjective

4. **enforceability** (0.0-1.0): Can this be verified or automated?
   - 1.0: Has verification checklist AND automation hooks (pre-commit/CI/grep)
   - 0.5: Has checklist OR hooks but not both
   - 0.0: No verification mechanism

5. **brevity** (0.0-1.0): Is the lesson concise without bloat?
   - 1.0: All code snippets ≤5 lines; explanations focused
   - 0.5: Some snippets >5 lines or verbose explanations
   - 0.0: Long snippets (>10 lines) or excessive prose

6. **evidence_use** (0.0-1.0): Does it use the provided evidence snippets? [ENHANCED CRITERIA]
   - 1.0 (Excellent): Uses [Evidence N] citations, quotes actual error messages/commands/outputs
   - 0.8 (Good): Quotes evidence but missing some citations, or few direct quotes
   - 0.6 (Fair): Mentions concepts from evidence with some specifics
   - 0.4 (Poor): Vague references to evidence without specifics
   - 0.2 (Minimal): Only acknowledges evidence exists
   - 0.0 (None): Ignores provided evidence entirely

   **Examples for 1.0 score**:
   - "Error: 'cd: too many arguments' [Evidence 1]"
   - "Command failed: cd /path with spaces [Evidence 2]"
   - "15+ occurrences in logs [Evidence 1]"

   **Examples for 0.6 score**:
   - "Commands with spaces caused errors" (no quotes)
   - "The trajectory showed path issues" (no specifics)

   **Examples for 0.0 score**:
   - Generic examples not from evidence
   - No mention of evidence at all

Return ONLY valid JSON with this structure:
```json
{
  "scores": {
    "correctness": 0.0,
    "specificity": 0.0,
    "detectability": 0.0,
    "enforceability": 0.0,
    "brevity": 0.0,
    "evidence_use": 0.0
  },
  "rationale": "Brief explanation of each score",
  "notes": "Additional observations"
}
```

Be strict but fair. Higher standards lead to better lessons.

IMPORTANT: Generate rationale and notes BEFORE scores in your JSON output.
This allows your reasoning to inform the numerical values."""

    user_prompt = f"""Score this lesson:

---
{lesson_markdown}
---

Context from trajectory:
- **Title**: {moment.get("title", "Unknown")}
- **Context**: {moment.get("context", "")}
- **What Changed**: {moment.get("what_changed", "")}
- **Rationale**: {moment.get("rationale", "")}

**Metrics**:
- Errors: {metrics.get("errors", 0)}
- Retry depth: {metrics.get("retry_depth", 0)}
- Duration: {metrics.get("duration_min", 0):.2f} min
- Tool invocations: {metrics.get("tool_invocations", 0)}

**Evidence snippets provided**:
{evidence}

**Source**: Conversation {conversation_id}

Score the lesson following the rubric.

For evidence_use specifically (MOST CRITICAL):
- Check for [Evidence 1], [Evidence 2], [Evidence 3] citations
- Look for quoted error messages, commands, outputs from evidence
- Verify examples come from evidence, not made up
- Score 0.8-1.0 requires direct evidence integration with citations
- Score 0.6 for concepts mentioned but no quotes
- Score 0.0-0.4 for weak or no evidence use

Return ONLY valid JSON."""

    messages = [Message("system", system_prompt), Message("user", user_prompt)]

    # Use Haiku for fast/cheap judging
    response = reply(
        messages,
        model="anthropic/claude-3-5-haiku-20241022",
        stream=True,
    )

    # Extract response content
    if isinstance(response, list):
        content = response[-1].content
    else:
        content = response.content

    # Parse JSON from response
    try:
        # Try to extract JSON from response (might have markdown fences)
        content_str = str(content)
        if "```json" in content_str:
            json_start = content_str.index("```json") + 7
            json_end = content_str.index("```", json_start)
            json_str = content_str[json_start:json_end].strip()
        elif "```" in content_str:
            json_start = content_str.index("```") + 3
            json_end = content_str.index("```", json_start)
            json_str = content_str[json_start:json_end].strip()
        else:
            json_str = content_str.strip()

        result = cast(Dict[str, Any], json.loads(json_str))

        # Validate structure
        if "scores" not in result:
            raise ValueError("Missing 'scores' field")

        required_scores = [
            "correctness",
            "specificity",
            "detectability",
            "enforceability",
            "brevity",
            "evidence_use",
        ]
        for score_name in required_scores:
            if score_name not in result["scores"]:
                raise ValueError(f"Missing score: {score_name}")

        return result

    except (json.JSONDecodeError, ValueError) as e:
        # Return error result
        return {
            "scores": {
                "correctness": 0.0,
                "specificity": 0.0,
                "detectability": 0.0,
                "enforceability": 0.0,
                "brevity": 0.0,
                "evidence_use": 0.0,
            },
            "rationale": f"Error parsing judge response: {e}",
            "notes": f"Raw response: {content}",
        }
