#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "anthropic>=0.40.0",
# ]
# [tool.uv]
# exclude-newer = "2025-11-04T00:00:00Z"
# ///
"""
ACE Reflector Agent

Critiques Generator output to identify patterns across insights,
classify success/failure modes, and refine insights iteratively.

Part of ACE-inspired context optimization (Phase 4).

The Reflector agent performs two key functions:
1. Pattern Analysis: Identifies meta-patterns across insights (success, failure,
   recurring, emergent themes)
2. Insight Refinement: Improves clarity, actionability, and evidence quality
   of insights based on pattern analysis
"""

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

import logging

import click

_logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore
    _logger.warning(
        "anthropic package not installed. Reflector features will be unavailable. "
        "Install with: pip install anthropic"
    )


@dataclass
class Pattern:
    """Meta-pattern identified across insights."""

    pattern_type: str  # success, failure, recurring, emergent
    theme: str  # High-level theme (e.g., "Context efficiency", "Tool selection")
    insights: List[str]  # Insight titles that exhibit this pattern
    description: str  # What the pattern reveals
    confidence: float  # 0.0-1.0, strength of pattern
    recommendations: List[str]  # Actions based on pattern


@dataclass
class RefinedInsight:
    """Insight refined by Reflector."""

    category: str
    title: str
    description: str
    evidence: List[str]
    confidence: float
    source_sessions: List[str]
    pattern_type: str  # success, failure, both
    refinement_notes: str  # What was improved
    actionability_score: float  # 0.0-1.0, how actionable


class ReflectorAgent:
    """
    ACE Reflector Agent: extracts patterns and refines insights.

    The Reflector works in conjunction with the Generator to form the
    Generator-Reflector loop in ACE:
    1. Generator extracts raw insights from trajectories
    2. Reflector identifies patterns and refines insights
    3. Curator then synthesizes refined insights into delta operations
    """

    def __init__(
        self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-20250514"
    ):
        """
        Initialize with Anthropic API.

        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            model: Model to use for analysis (default: claude-sonnet-4-5)
        """
        if anthropic is None:
            raise ImportError(
                "anthropic package required. Install: pip install anthropic"
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def analyze_patterns(
        self, insights: List[dict], existing_patterns: Optional[List[dict]] = None
    ) -> List[Pattern]:
        """
        Identify meta-patterns across insights.

        Args:
            insights: List of Insight dicts from Generator
            existing_patterns: Optional existing patterns to avoid duplicates

        Returns:
            List of Pattern objects representing meta-patterns
        """
        # Format insights for LLM analysis
        insights_text = self._format_insights_for_analysis(insights)

        # Include existing patterns for deduplication
        existing_context = ""
        if existing_patterns:
            existing_context = f"""
Existing patterns (avoid duplicates):
{json.dumps(existing_patterns, indent=2)}

"""

        prompt = f"""Analyze these insights to identify meta-patterns.

Insights from Generator:
{insights_text}
{existing_context}
Extract patterns that reveal:

1. **Success Patterns**: What consistently leads to good outcomes
   - Efficient workflows that save time/tokens
   - Effective tool usage
   - Productive decision-making

2. **Failure Patterns**: What consistently causes problems
   - Common mistakes or anti-patterns
   - Tool misuse or limitations
   - Inefficient approaches

3. **Recurring Themes**: Topics that appear across multiple insights
   - Core capabilities being developed
   - Persistent challenges
   - Emerging best practices

4. **Emergent Patterns**: Non-obvious connections between insights
   - How different practices interact
   - Compound effects (A + B = C)
   - System-level behaviors

For each pattern, provide:
- pattern_type: success, failure, recurring, or emergent
- theme: High-level category (2-4 words)
- insights: List of insight titles showing this pattern
- description: What the pattern reveals (2-3 sentences)
- confidence: 0.0-1.0 based on evidence strength
- recommendations: Actionable next steps (list)

Focus on patterns with 2+ supporting insights.

Return JSON array:
[{{
  "pattern_type": "success",
  "theme": "Context Efficiency",
  "insights": ["Title 1", "Title 2"],
  "description": "What the pattern reveals.",
  "confidence": 0.85,
  "recommendations": ["Action 1", "Action 2"]
}}]
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.3,  # Lower for more consistent analysis
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content[0].text

            # Try to extract JSON from markdown code fence first
            json_fence_match = re.search(
                r"```(?:json)?\s*(\[.*?\])\s*```", content, re.DOTALL
            )
            if json_fence_match:
                json_str = json_fence_match.group(1)
            else:
                # Fall back to finding raw JSON array
                start_idx = content.find("[")
                end_idx = content.rfind("]") + 1
                if start_idx == -1 or end_idx <= start_idx:
                    click.echo("Warning: No JSON array found in response", err=True)
                    return []
                json_str = content[start_idx:end_idx]

            patterns_data = json.loads(json_str)

            return [Pattern(**p) for p in patterns_data]

        except json.JSONDecodeError as e:
            click.echo(f"Error parsing patterns JSON: {e}", err=True)
            return []
        except Exception as e:
            click.echo(f"Error analyzing patterns: {e}", err=True)
            return []

    def refine_insights(
        self, insights: List[dict], patterns: List[Pattern]
    ) -> List[RefinedInsight]:
        """
        Refine insights based on pattern analysis.

        Args:
            insights: List of Insight dicts from Generator
            patterns: List of Pattern objects from analyze_patterns

        Returns:
            List of RefinedInsight objects with improved quality
        """
        insights_text = self._format_insights_for_analysis(insights)
        patterns_text = self._format_patterns(patterns)

        prompt = f"""Refine these insights for clarity and actionability.

Original Insights:
{insights_text}

Identified Patterns:
{patterns_text}

For each insight, refine:

1. **Clarity**: Make title and description crystal clear
   - Title: Imperative statement (do X)
   - Description: What, when, why (concise)

2. **Actionability**: Ensure it's immediately applicable
   - Concrete steps, not vague advice
   - Observable triggers/conditions
   - Measurable outcomes

3. **Pattern Classification**: Label as success or failure pattern
   - success: What to do (positive pattern)
   - failure: What to avoid (anti-pattern)
   - both: Context-dependent

4. **Evidence Strengthening**: Improve evidence quality
   - Specific examples from trajectories
   - Remove vague references
   - Add pattern connections if relevant

Return refined insights as JSON array:
[{{
  "category": "workflow",
  "title": "Refined imperative title",
  "description": "Clearer what/when/why.",
  "evidence": ["Specific example 1", "Specific example 2"],
  "confidence": 0.85,
  "source_sessions": ["session-1", "session-2"],
  "pattern_type": "success",
  "refinement_notes": "What was improved",
  "actionability_score": 0.9
}}]
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=6000,
                temperature=0.2,  # Very low for consistent refinement
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content[0].text

            # Try to extract JSON from markdown code fence first
            json_fence_match = re.search(
                r"```(?:json)?\s*(\[.*?\])\s*```", content, re.DOTALL
            )
            if json_fence_match:
                json_str = json_fence_match.group(1)
            else:
                # Fall back to finding raw JSON array
                start_idx = content.find("[")
                end_idx = content.rfind("]") + 1
                if start_idx == -1 or end_idx <= start_idx:
                    click.echo("Warning: No JSON array found in response", err=True)
                    return []
                json_str = content[start_idx:end_idx]

            refined_data = json.loads(json_str)

            return [RefinedInsight(**r) for r in refined_data]

        except json.JSONDecodeError as e:
            click.echo(f"Error parsing refined insights JSON: {e}", err=True)
            return []
        except Exception as e:
            click.echo(f"Error refining insights: {e}", err=True)
            return []

    def _format_insights_for_analysis(self, insights: List[dict]) -> str:
        """Format insights for LLM analysis."""
        lines = []
        for i, insight in enumerate(insights, 1):
            lines.append(f"\n## Insight {i}: {insight.get('title', 'Untitled')}")
            lines.append(f"Category: {insight.get('category', 'unknown')}")
            lines.append(f"Confidence: {insight.get('confidence', 0.0)}")
            lines.append(f"\nDescription: {insight.get('description', '')}")
            lines.append("\nEvidence:")
            for e in insight.get("evidence", []):
                lines.append(f"  - {e}")
            sessions = insight.get("source_sessions", [])
            if sessions:
                lines.append(f"\nSessions: {', '.join(sessions)}")

        return "\n".join(lines)

    def _format_patterns(self, patterns: List[Pattern]) -> str:
        """Format patterns for display."""
        lines = []
        for i, pattern in enumerate(patterns, 1):
            lines.append(f"\n## Pattern {i}: {pattern.theme}")
            lines.append(f"Type: {pattern.pattern_type}")
            lines.append(f"Confidence: {pattern.confidence}")
            lines.append(f"\nDescription: {pattern.description}")
            lines.append("\nSupporting Insights:")
            for insight_title in pattern.insights:
                lines.append(f"  - {insight_title}")
            lines.append("\nRecommendations:")
            for rec in pattern.recommendations:
                lines.append(f"  - {rec}")

        return "\n".join(lines)


@click.group()
def cli():
    """ACE Reflector Agent CLI - Pattern analysis and insight refinement."""
    pass


@cli.command()
@click.argument("insights_file", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Output file for patterns")
@click.option(
    "--existing-patterns",
    type=click.Path(exists=True),
    help="Existing patterns file for deduplication",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would happen without API calls"
)
def analyze(
    insights_file: str,
    output: Optional[str],
    existing_patterns: Optional[str],
    dry_run: bool,
):
    """Analyze insights to extract meta-patterns."""
    insights_path = Path(insights_file)
    output_path = Path(output) if output else None

    # Load insights from Generator output
    with open(insights_path) as f:
        insights_data = json.load(f)

    click.echo(f"Loaded {len(insights_data)} insights from {insights_file}")

    # Load existing patterns if provided
    existing_patterns_data = None
    if existing_patterns:
        with open(existing_patterns) as f:
            existing_patterns_data = json.load(f)
        click.echo(f"Loaded {len(existing_patterns_data)} existing patterns")

    if dry_run:
        click.echo("\n=== DRY RUN ===")
        click.echo("Would analyze these insights for patterns:")
        for i, insight in enumerate(insights_data[:5], 1):
            click.echo(f"\n{i}. {insight.get('title', 'Untitled')}")
            click.echo(f"   Category: {insight.get('category', 'unknown')}")
            click.echo(f"   Confidence: {insight.get('confidence', 0.0)}")
        if len(insights_data) > 5:
            click.echo(f"\n... and {len(insights_data) - 5} more insights")
        return

    # Run analysis
    agent = ReflectorAgent()
    patterns = agent.analyze_patterns(insights_data, existing_patterns_data)

    click.echo(f"\n=== Identified {len(patterns)} Patterns ===\n")
    click.echo(agent._format_patterns(patterns))

    # Save patterns
    if output_path:
        patterns_dict = [asdict(p) for p in patterns]
        with open(output_path, "w") as f:
            json.dump(patterns_dict, f, indent=2)
        click.echo(f"\nPatterns saved to {output_path}")


@cli.command()
@click.argument("insights_file", type=click.Path(exists=True))
@click.option(
    "--patterns-file",
    type=click.Path(exists=True),
    help="Patterns from analyze command",
)
@click.option(
    "--output", "-o", type=click.Path(), help="Output file for refined insights"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would happen without API calls"
)
def refine(
    insights_file: str,
    patterns_file: Optional[str],
    output: Optional[str],
    dry_run: bool,
):
    """Refine insights based on pattern analysis."""
    insights_path = Path(insights_file)
    patterns_path = Path(patterns_file) if patterns_file else None
    output_path = Path(output) if output else None

    # Load insights
    with open(insights_path) as f:
        insights_data = json.load(f)

    # Load patterns if provided
    patterns = []
    if patterns_path:
        with open(patterns_path) as f:
            patterns_data = json.load(f)
        patterns = [Pattern(**p) for p in patterns_data]
        click.echo(f"Loaded {len(patterns)} patterns from {patterns_path}")

    click.echo(f"Loaded {len(insights_data)} insights from {insights_file}")

    if dry_run:
        click.echo("\n=== DRY RUN ===")
        click.echo("Would refine these insights:")
        for i, insight in enumerate(insights_data[:5], 1):
            click.echo(f"\n{i}. {insight.get('title', 'Untitled')}")
            click.echo(f"   Current confidence: {insight.get('confidence', 0.0)}")
        if len(insights_data) > 5:
            click.echo(f"\n... and {len(insights_data) - 5} more insights")
        return

    # Run refinement
    agent = ReflectorAgent()
    refined = agent.refine_insights(insights_data, patterns)

    click.echo(f"\n=== Refined {len(refined)} Insights ===\n")
    for i, r in enumerate(refined, 1):
        click.echo(f"\n{i}. {r.title}")
        click.echo(f"   Pattern Type: {r.pattern_type}")
        click.echo(f"   Actionability: {r.actionability_score}")
        click.echo(f"   Refinement: {r.refinement_notes}")

    # Save refined insights
    if output_path:
        refined_dict = [asdict(r) for r in refined]
        with open(output_path, "w") as f:
            json.dump(refined_dict, f, indent=2)
        click.echo(f"\nRefined insights saved to {output_path}")


if __name__ == "__main__":
    cli()
