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
ACE Generator Agent - Optimized (Phase 7.1)

Analyzes session trajectories to extract thought-action-observation chains,
identify effective strategies and pitfalls, and generate candidate insights.

Optimizations:
- Few-shot examples
- Lesson awareness (duplicate detection)
- Domain context (Bob's environment)
- Evidence extraction guidance
- Anti-pattern identification
- Confidence calibration
- Category definitions
- Quality filtering
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import click

try:
    import anthropic
except ImportError:
    anthropic = None  # noqa: F811


# Optimization 3: Domain Context
DOMAIN_CONTEXT = """
Operating Context:
- Agent: Bob (autonomous AI assistant)
- Framework: gptme (CLI agent framework)
- Primary Activities: Code development, PR reviews, autonomous task execution
- Key Tools: git, GitHub CLI, shell, Python, tmux
- Repositories: gptme (main project), gptme-bob (workspace)
- Environment: Ubuntu 24.04, SSH access, systemd services

Autonomous Operation:
- Runs 48x per week (scheduled via systemd)
- 3-step workflow: Loose Ends → Task Selection → Execution
- Budget: 200k tokens per run (~160k for work)
- Non-interactive (all actions auto-executed)
"""

# Optimization 7: Category Definitions
CATEGORY_DEFINITIONS = """
Category Definitions:

**workflow**: Task management, planning, execution patterns, decision-making processes, session organization
  Examples: task selection methods, loose ends checks, CASCADE protocol

**tools**: Specific tool usage (git, shell, tmux, etc.), tool limitations and workarounds, tool-specific best practices
  Examples: git worktree workflow, shell quoting, tmux session management

**patterns**: Cross-tool patterns and principles, architectural approaches, meta-patterns
  Examples: fail-fast, incremental commits, persistent learning

**strategic**: Goal-oriented decision-making, resource allocation, priority management
  Examples: MIQ scoring, task alignment, goal-directed work

**social**: Human interaction patterns, communication practices, coordination workflows
  Examples: PR etiquette, issue engagement, GitHub collaboration
"""

# Optimization 6: Confidence Calibration
CONFIDENCE_GUIDANCE = """
Confidence Score Guidelines:

0.9-1.0 (Very High): Pattern appears 5+ times, clear cause-effect, direct evidence of success/failure, generalizable
0.7-0.8 (High): Pattern appears 3-4 times, strong evidence, likely to generalize, minor context dependencies
0.5-0.6 (Medium): Pattern appears 2 times, suggestive but not conclusive, may be session-specific
< 0.5 (Low): Pattern appears once, weak/indirect evidence, highly context-specific (consider not including)
"""

# Optimization 1: Few-Shot Examples
EXAMPLE_INSIGHTS = """
Example 1 - Good insight:
{
  "category": "workflow",
  "title": "Use Git Worktrees for External PRs",
  "description": "Always create git worktrees when working on external repositories to enable parallel work and proper PR workflow. Use original branch names from PRs, not pr-NUMBER format.",
  "evidence": [
    "Chain 5: Thought 'checked out pr-812 locally' → Action: git checkout pr-812 → Observation: Tracking issues when pushing",
    "Chain 8: Thought 'create worktree with original branch name' → Action: git worktree add feature-task-loop → Observation: Proper tracking enabled"
  ],
  "confidence": 0.9
}

Example 2 - Bad insight (too generic):
{
  "category": "tools",
  "title": "Use version control",
  "description": "Git is important for tracking changes.",
  "evidence": ["Used git throughout session"],
  "confidence": 0.5
}
Why bad: Too generic, not actionable, weak evidence, obvious statement.
"""

# Optimization 4: Evidence Requirements
EVIDENCE_REQUIREMENTS = """
Evidence Requirements:
- Quote exact phrases from trajectory (use "..." for quotes)
- Include chain numbers for traceability
- Show both context and key action/observation
- Provide 2-4 pieces of evidence per insight minimum

Good Evidence Examples:
✓ "Chain 3: Thought 'I should use worktree here...' → Action: git worktree add → Observation: Success, proper tracking"
✓ "Chain 7: Error 'cd: too many arguments' due to unquoted path with spaces"

Bad Evidence Examples:
✗ "Used worktrees successfully"
✗ "Had issues with paths"
"""

# Optimization 5: Pattern Types
PATTERN_TYPES = """
Pattern Types to Identify:

**Effective Strategies** (what worked):
- Successful tool usage patterns
- Efficient workflows
- Problem-solving approaches

**Anti-Patterns** (what failed):
- Error-prone practices
- Inefficient approaches
- Common mistakes

**Recovery Patterns** (how failures were fixed):
- Error diagnosis methods
- Successful fixes
- Lessons from failures

For anti-patterns, describe:
1. The mistake/problem
2. Why it failed
3. How to avoid it
4. What to do instead
"""

# Optimization 8: Quality Criteria
QUALITY_CRITERIA = """
Insight Quality Requirements:

MUST HAVE:
- Clear, specific action (imperative verb)
- Concrete examples from trajectory
- Generalizable to future sessions
- Confidence >= 0.5

SHOULD HAVE:
- Multiple pieces of evidence (2-4)
- Causal explanation (why this matters)
- Context boundaries (when to apply)
- Novel or more specific than existing lessons

AVOID:
- Generic advice ("use best practices")
- Obvious statements ("commit your work")
- Session-specific details (file names, specific bugs)
- Low-confidence hunches (< 0.5)
- Duplicates of existing lessons
"""


@dataclass
class ThoughtActionObservation:
    """Represents a thought-action-observation chain from a session."""

    thought: str
    action: str
    observation: str
    session_id: str
    timestamp: Optional[str] = None


@dataclass
class Insight:
    """Candidate insight extracted from trajectories."""

    category: str
    title: str
    description: str
    evidence: List[str]
    confidence: float
    source_sessions: List[str]


class TrajectoryParser:
    """Parses session logs to extract structured trajectories."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.content = log_path.read_text()
        self.session_id = self._extract_session_id()

    def _extract_session_id(self) -> str:
        """Extract session ID from log filename."""
        match = re.search(r"(\d{8}-\d{6})", self.log_path.name)
        return match.group(1) if match else self.log_path.stem

    def extract_tao_chains(self) -> List[ThoughtActionObservation]:
        """Extract thought-action-observation chains from log."""
        chains = []
        think_pattern = r"<think>(.*?)</think>"
        think_blocks = re.finditer(think_pattern, self.content, re.DOTALL)

        for think_match in think_blocks:
            thought = think_match.group(1).strip()
            action_start = think_match.end()
            action_pattern = r"```(?:shell|python|ipython)(.*?)```"
            action_match = re.search(
                action_pattern,
                self.content[action_start : action_start + 2000],
                re.DOTALL,
            )

            if not action_match:
                continue

            action = action_match.group(1).strip()
            obs_start = action_start + action_match.end()
            obs_pattern = r"<system>(.*?)</system>"
            obs_match = re.search(
                obs_pattern, self.content[obs_start : obs_start + 2000], re.DOTALL
            )

            observation = (
                obs_match.group(1).strip()[:500] if obs_match else "(no observation)"
            )

            chains.append(
                ThoughtActionObservation(
                    thought=thought,
                    action=action,
                    observation=observation,
                    session_id=self.session_id,
                )
            )

        return chains


class GeneratorAgent:
    """ACE Generator Agent: analyzes trajectories and generates insights."""

    def __init__(self, api_key: Optional[str] = None):
        """Initialize with Anthropic API."""
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-5"

    def analyze_trajectory(
        self,
        chains: List[ThoughtActionObservation],
        session_id: str,
        existing_lessons: Optional[List[str]] = None,
    ) -> List[Insight]:
        """
        Analyze TAO chains to extract insights.

        Args:
            chains: Thought-action-observation chains from session
            session_id: Session identifier
            existing_lessons: Optional list of existing lesson titles to avoid duplicates
        """

        chains_text = self._format_chains(chains)

        # Optimization 2: Lesson Awareness
        lessons_context = ""
        if existing_lessons:
            lessons_list = "\n".join(f"- {title}" for title in existing_lessons[:50])
            lessons_context = f"""
Existing Lessons (avoid duplicating):
{lessons_list}

Only generate insights that are:
- Novel (not covered by existing lessons)
- More specific than existing guidance
- Complementary to current knowledge base
"""

        # Comprehensive optimized prompt
        prompt = f"""{DOMAIN_CONTEXT}

{CATEGORY_DEFINITIONS}

Analyze this session trajectory to extract actionable insights.

Session ID: {session_id}

Thought-Action-Observation Chains:
{chains_text}

{PATTERN_TYPES}

{EVIDENCE_REQUIREMENTS}

{CONFIDENCE_GUIDANCE}

{QUALITY_CRITERIA}

{lessons_context}

{EXAMPLE_INSIGHTS}

Return JSON array of insights following the good example format:
[{{
  "category": "workflow|tools|patterns|strategic|social",
  "title": "Imperative statement",
  "description": "What to do and why (2-3 sentences)",
  "evidence": ["Chain X: Quoted evidence with context", "Chain Y: Another example"],
  "confidence": 0.5-1.0
}}]
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content[0].text
            json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
            json_str = json_match.group(1) if json_match else content
            insights_data = json.loads(json_str)

            insights = []
            for item in insights_data:
                insights.append(
                    Insight(
                        category=item["category"],
                        title=item["title"],
                        description=item["description"],
                        evidence=item["evidence"],
                        confidence=item["confidence"],
                        source_sessions=[session_id],
                    )
                )

            return insights

        except Exception as e:
            click.echo(f"Error analyzing trajectory: {e}", err=True)
            return []

    def _format_chains(self, chains: List[ThoughtActionObservation]) -> str:
        """Format TAO chains for LLM consumption."""
        formatted = []
        for i, chain in enumerate(chains, 1):
            formatted.append(
                f"""
Chain {i}:
Thought: {chain.thought[:300]}...
Action: {chain.action[:200]}...
Observation: {chain.observation[:300]}...
"""
            )
        return "\n".join(formatted)


def load_lesson_titles(workspace_path: Path) -> List[str]:
    """Load existing lesson titles from workspace."""
    lessons_dir = workspace_path / "lessons"
    if not lessons_dir.exists():
        return []

    titles = []
    for lesson_file in lessons_dir.rglob("*.md"):
        # Extract title from filename (remove .md, replace hyphens with spaces, title case)
        title = lesson_file.stem.replace("-", " ").title()
        titles.append(title)

    return sorted(titles)


@click.group()
def cli():
    """ACE Generator Agent - Extract insights from session trajectories."""
    pass


@cli.command()
@click.argument("log_path", type=click.Path(exists=True))
@click.option(
    "--output", "-o", type=click.Path(), help="Output file for insights (JSON)"
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    help="Anthropic API key (or set ANTHROPIC_API_KEY)",
)
@click.option("--dry-run", is_flag=True, help="Parse trajectory without LLM analysis")
@click.option(
    "--workspace",
    type=click.Path(exists=True),
    default="/home/bob/bob",
    help="Workspace path for lesson loading",
)
@click.option(
    "--no-lessons",
    is_flag=True,
    help="Skip loading existing lessons (faster, may generate duplicates)",
)
def analyze(
    log_path: str,
    output: Optional[str],
    api_key: Optional[str],
    dry_run: bool,
    workspace: str,
    no_lessons: bool,
):
    """Analyze a single session log to extract insights."""

    log_file = Path(log_path)
    output_file = Path(output) if output else None
    workspace_path = Path(workspace)

    click.echo(f"Analyzing session: {log_file.name}")

    # Parse trajectory
    parser = TrajectoryParser(log_file)
    chains = parser.extract_tao_chains()

    click.echo(f"Extracted {len(chains)} thought-action-observation chains")

    if not chains:
        click.echo("No chains found in log. Exiting.")
        return

    if dry_run:
        click.echo("\nParsed Thought-Action-Observation chains:\n")
        for i, chain in enumerate(chains[:5], 1):
            click.echo(f"Chain {i}:")
            click.echo(f"  Thought: {chain.thought[:150]}...")
            click.echo(f"  Action: {chain.action[:100]}...")
            click.echo(f"  Observation: {chain.observation[:100]}...")
            click.echo()

        if len(chains) > 5:
            click.echo(f"... and {len(chains) - 5} more chains")

        click.echo("\nSkipping LLM analysis (--dry-run mode)")
        return

    # Load existing lessons (Optimization 2)
    existing_lessons = None
    if not no_lessons:
        click.echo("Loading existing lessons for duplicate detection...")
        existing_lessons = load_lesson_titles(workspace_path)
        click.echo(f"Loaded {len(existing_lessons)} existing lesson titles")

    # Generate insights
    generator = GeneratorAgent(api_key=api_key)
    insights = generator.analyze_trajectory(chains, parser.session_id, existing_lessons)

    click.echo(f"\nGenerated {len(insights)} insights")

    # Display insights
    for i, insight in enumerate(insights, 1):
        click.echo(f"\n{i}. [{insight.category}] {insight.title}")
        click.echo(f"   Confidence: {insight.confidence:.2f}")
        click.echo(f"   {insight.description}")
        click.echo(f"   Evidence: {len(insight.evidence)} examples")
        for j, evidence in enumerate(insight.evidence, 1):
            click.echo(f"     {j}. {evidence[:100]}...")

    # Save to file if requested
    if output_file:
        insights_data = [
            {
                "category": insight.category,
                "title": insight.title,
                "description": insight.description,
                "evidence": insight.evidence,
                "confidence": insight.confidence,
                "source_sessions": insight.source_sessions,
            }
            for insight in insights
        ]

        output_file.write_text(json.dumps(insights_data, indent=2))
        click.echo(f"\nInsights saved to: {output_file}")


if __name__ == "__main__":
    cli()
