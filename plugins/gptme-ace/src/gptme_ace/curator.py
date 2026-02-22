#!/usr/bin/env python3
"""
ACE Curator Agent - Phase 3.2

Synthesizes refined insights from Reflector into delta operations
for incremental lesson updates.

Input: Refined insights from ace_storage.py
Output: Delta documents (ADD/REMOVE/MODIFY operations)

Usage:
  ./scripts/lessons/ace-curator.py generate --insight-id abc123
  ./scripts/lessons/ace-curator.py batch --status approved
  ./scripts/lessons/ace-curator.py list [--status pending]
"""

# /// script
# dependencies = [
#   "click>=8.0.0",
#   "anthropic>=0.40.0",
# ]
# ///

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import click
from dotenv import load_dotenv

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from .storage import InsightStorage, StoredInsight

try:
    from anthropic import Anthropic

    HAS_ANTHROPIC = True
except ImportError:
    Anthropic = None
    HAS_ANTHROPIC = False


@dataclass
class DeltaOperation:
    """Single delta operation (ADD/REMOVE/MODIFY)"""

    type: str  # add, remove, modify
    section: str  # Target lesson section
    content: str | None = None  # For ADD/MODIFY
    position: str | None = None  # For ADD: append, prepend, after:hash
    target: Dict[str, str] | None = None  # For REMOVE/MODIFY


@dataclass
class Delta:
    """Complete delta document for lesson update"""

    delta_id: str
    created: str  # ISO 8601
    source: str  # "ace_curator"
    source_insights: List[str]  # Insight IDs
    lesson_id: str
    operations: List[DeltaOperation]
    rationale: str
    review_status: str  # pending, approved, rejected
    applied_at: str | None = None
    applied_by: str | None = None


class CuratorAgent:
    """
    ACE Curator Agent: Synthesizes refined insights into delta operations
    """

    def __init__(self, api_key: str | None = None, dry_run: bool = False):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.dry_run = dry_run
        if not dry_run and not HAS_ANTHROPIC:
            raise ImportError(
                "anthropic package required for non-dry-run mode. "
                "Install: pip install anthropic"
            )
        self.client = None if dry_run else Anthropic(api_key=self.api_key)
        self.storage = InsightStorage()
        self.delta_dir = Path("deltas")
        self.delta_dir.mkdir(exist_ok=True)
        (self.delta_dir / "pending").mkdir(exist_ok=True)
        (self.delta_dir / "approved").mkdir(exist_ok=True)
        (self.delta_dir / "rejected").mkdir(exist_ok=True)

    def generate_delta(
        self, insight: StoredInsight, lesson_content: str | None = None
    ) -> Delta:
        """
        Generate delta operations from refined insight

        Args:
            insight: Refined insight from storage
            lesson_content: Current lesson content (if updating existing)

        Returns:
            Delta document with operations
        """
        if self.dry_run:
            return self._mock_delta(insight)

        # Read lesson if updating existing
        lesson_path = None
        if lesson_content is None:
            lesson_path = self._find_lesson(insight.category)
            if lesson_path is not None:
                # Validate that insight actually matches this lesson
                is_match, reasoning = self._validate_lesson_match(insight, lesson_path)

                if not is_match:
                    print(f"⚠️  Lesson match validation failed for {lesson_path.stem}")
                    print(f"   Reasoning: {reasoning}")
                    print("   → Creating new lesson instead of modifying")
                    lesson_content = None  # Treat as "no lesson found"
                    lesson_path = None
                else:
                    print(f"✓ Validated: {insight.title} → {lesson_path.stem}")
                    lesson_content = lesson_path.read_text()

        # Generate operations using Claude
        operations = self._generate_operations(insight, lesson_content)

        # Create delta document
        delta_id = self._generate_delta_id(insight)
        delta = Delta(
            delta_id=delta_id,
            created=datetime.utcnow().isoformat() + "Z",
            source="ace_curator",
            source_insights=[insight.metadata.insight_id],
            lesson_id=self._determine_lesson_id(insight, lesson_content),
            operations=operations,
            rationale=self._create_rationale(insight),
            review_status="pending",
        )

        return delta

    def _extract_json_from_response(self, text: str) -> Any:
        """
        Extract JSON from Claude's response, handling markdown blocks

        Handles formats:
        - Pure JSON: [...]
        - Markdown: ```json\n[...]\n```
        - With text: "Here's the JSON:\n```json\n[...]\n```"
        """
        # Try to parse as-is first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Found JSON block but failed to parse: {e}\nContent: {json_match.group(1)[:200]}"
                )

        # Try finding JSON array or object
        array_match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if array_match:
            try:
                return json.loads(array_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(
            f"Could not extract valid JSON from response. Response preview: {text[:500]}"
        )

    def _validate_lesson_match(
        self, insight: StoredInsight, lesson_path: Path
    ) -> tuple[bool, str]:
        """
        Validate if insight content actually matches the lesson's topic

        Prevents categorization errors like:
        - CASCADE checks being added to git-worktree-workflow
        - Pytest patterns being added to safe-operation-patterns

        Args:
            insight: The refined insight to validate
            lesson_path: Path to potential matching lesson

        Returns:
            (is_match, reasoning) tuple where:
            - is_match: True if insight belongs in this lesson
            - reasoning: Explanation of match/mismatch
        """
        if self.client is None:
            return True, "Dry-run mode: skipping validation"

        lesson_content = lesson_path.read_text()

        # Extract lesson title and rule for context
        lesson_title = lesson_path.stem.replace("-", " ").title()
        rule_section = ""
        lines = lesson_content.split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("## Rule"):
                # Get rule and next few lines
                rule_section = "\n".join(lines[i : min(i + 6, len(lines))])
                break

        prompt = f"""Validate whether this insight belongs in the target lesson.

**Insight**:
- Title: {insight.title}
- Description: {insight.description}
- Category: {insight.category}

**Target Lesson**:
- File: {lesson_path.stem}
- Title: {lesson_title}
- Rule Section:
{rule_section}

**Task**: Determine if insight content actually matches lesson topic.

Respond with only:
- "MATCH" if insight directly relates to lesson's core topic
- "NO_MATCH" if insight is unrelated or only tangentially related

Then provide 1-2 sentence reasoning.

**Examples**:
- "git worktree commands" + "git-worktree-workflow" = MATCH (direct fit)
- "CASCADE task selection" + "git-worktree-workflow" = NO_MATCH (unrelated)
- "pytest config" + "safe-operation-patterns" = NO_MATCH (different topic)

Respond now:"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=200,
                temperature=0.0,  # Deterministic
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = response.content[0].text.strip()
            is_match = response_text.startswith(
                "MATCH"
            ) and not response_text.startswith("NO_MATCH")
            reasoning = response_text

            return is_match, reasoning

        except Exception as e:
            # On error, default to NO_MATCH to be safe
            print(f"⚠️  Validation error: {e}")
            return False, f"Validation failed: {e}"

    def _generate_operations(
        self, insight: StoredInsight, lesson_content: str | None
    ) -> List[DeltaOperation]:
        """
        Use Claude to generate appropriate delta operations with retry logic

        Analyzes insight and current lesson to determine:
        - Whether to ADD new content
        - Whether to MODIFY existing content
        - Whether to REMOVE outdated content
        """
        if self.client is None:
            raise RuntimeError(
                "Client not initialized. Cannot generate operations in dry-run mode."
            )

        prompt = self._build_curator_prompt(insight, lesson_content)

        # Retry logic for LLM calls
        max_retries = 3
        retry_delay = 2  # seconds

        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=4000,
                    temperature=0.2,
                    messages=[{"role": "user", "content": prompt}],
                )

                # Parse Claude's JSON response using helper
                response_text = response.content[0].text
                operations_json = self._extract_json_from_response(response_text)
                return [DeltaOperation(**op) for op in operations_json]

            except (json.JSONDecodeError, ValueError) as e:
                if attempt < max_retries - 1:
                    print(f"⚠️  Attempt {attempt + 1} failed: {e}")
                    print(f"   Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    print(
                        f"❌ All {max_retries} attempts failed for insight {insight.metadata.insight_id}"
                    )
                    raise
            except Exception as e:
                print(f"❌ Unexpected error: {e}")
                raise

        raise RuntimeError("Retry loop exhausted without success")

    def _build_curator_prompt(
        self, insight: StoredInsight, lesson_content: str | None
    ) -> str:
        """Build prompt for Claude to generate delta operations"""
        return f"""You are the ACE Curator Agent. Generate delta operations to update a lesson based on this refined insight.

**Insight**:
- Category: {insight.category}
- Title: {insight.title}
- Description: {insight.description}
- Pattern Type: {insight.pattern_type}
- Evidence: {json.dumps(insight.evidence, indent=2)}
- Confidence: {insight.confidence}

**Current Lesson** (if updating existing):
{lesson_content if lesson_content else "NO EXISTING LESSON - Will create new one"}

**Your Task**:
Generate delta operations (ADD/REMOVE/MODIFY) to incorporate this insight.

**Operation Guidelines**:

1. **ADD Operations**: Add new content to lesson
   - Detection signals from evidence
   - New patterns from successful strategies
   - Outcome benefits from pattern_type
   - Position: "append" (usually) or "prepend" (if critical)

2. **MODIFY Operations**: Refine existing content
   - Clarify rule statements
   - Improve pattern examples
   - Sharpen detection signals
   - Requires exact old_content match

3. **REMOVE Operations**: Remove outdated content
   - Obsolete signals (if evidence shows no occurrences)
   - Incorrect patterns (if insight contradicts)
   - Deprecated examples

**Response Format** (JSON array of operations):
```json
[
  {{
    "type": "add",
    "section": "Detection",
    "content": "- New observable signal from evidence",
    "position": "append"
  }},
  {{
    "type": "modify",
    "section": "Rule",
    "target": {{
      "type": "line_match",
      "old_content": "Exact current line"
    }},
    "content": "Improved line with more clarity"
  }}
]
```

**Guidelines**:
- Prefer ADD over MODIFY (less fragile)
- Use MODIFY only when improving clarity of existing content
- Use REMOVE sparingly (only if evidence shows obsolescence)
- Each operation should be atomic and focused
- Target specific sections (Rule, Context, Detection, Pattern, Outcome)

Generate the delta operations now:"""

    def _mock_delta(self, insight: StoredInsight) -> Delta:
        """Generate mock delta for dry-run mode"""
        operations = [
            DeltaOperation(
                type="add",
                section="Detection",
                content=f"- Mock detection signal from insight: {insight.title}",
                position="append",
            )
        ]

        return Delta(
            delta_id=f"d_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_mock",
            created=datetime.utcnow().isoformat() + "Z",
            source="ace_curator",
            source_insights=[insight.metadata.insight_id],
            lesson_id=f"{insight.category}_mock_lesson",
            operations=operations,
            rationale=f"Mock delta for dry-run: {insight.title}",
            review_status="pending",
        )

    def _find_lesson(self, category: str) -> Path | None:
        """Find existing lesson file by category"""
        lessons_dir = Path("lessons")
        if not lessons_dir.exists():
            return None
        # Search subdirectories
        for subdir in lessons_dir.iterdir():
            if not subdir.is_dir():
                continue
            for lesson_file in subdir.glob("*.md"):
                # Simple match: if category in filename or content
                if category.lower() in lesson_file.stem.lower():
                    return lesson_file
        return None

    def _determine_lesson_id(
        self, insight: StoredInsight, lesson_content: str | None
    ) -> str:
        """
        Determine lesson_id for delta

        If updating existing: Extract from frontmatter
        If creating new: Generate from category and title
        """
        if lesson_content:
            # Extract lesson_id from frontmatter
            for line in lesson_content.split("\n"):
                if line.startswith("lesson_id:"):
                    return line.split("lesson_id:")[1].strip()

        # Generate new lesson_id
        category_slug = insight.category.lower().replace(" ", "-")
        title_slug = insight.title.lower().replace(" ", "-")[:30]
        return f"{category_slug}_{title_slug}_generated"

    def _generate_delta_id(self, insight: StoredInsight) -> str:
        """Generate unique delta ID"""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        insight_hash = insight.metadata.insight_id[:8]
        return f"d_{timestamp}_{insight_hash}"

    def _create_rationale(self, insight: StoredInsight) -> str:
        """Create human-readable rationale for delta"""
        evidence_summary = (
            f"{len(insight.evidence)} sessions"
            if insight.evidence
            else "multiple sessions"
        )
        return f"{insight.title} ({insight.pattern_type} pattern, confidence {insight.confidence:.2f}). Evidence: {evidence_summary}. {insight.refinement_notes or ''}"

    def save_delta(self, delta: Delta) -> Path:
        """Save delta to pending/ directory"""
        delta_path = self.delta_dir / "pending" / f"{delta.delta_id}.json"

        # Convert to dict with proper serialization
        delta_dict = {
            "delta_id": delta.delta_id,
            "created": delta.created,
            "source": delta.source,
            "source_insights": delta.source_insights,
            "lesson_id": delta.lesson_id,
            "operations": [asdict(op) for op in delta.operations],
            "rationale": delta.rationale,
            "review_status": delta.review_status,
            "applied_at": delta.applied_at,
            "applied_by": delta.applied_by,
        }

        delta_path.write_text(json.dumps(delta_dict, indent=2))
        return delta_path

    def list_deltas(self, status: str = "pending") -> List[Dict[str, Any]]:
        """List deltas by status"""
        status_dir = self.delta_dir / status
        if not status_dir.exists():
            return []

        deltas = []
        for delta_file in status_dir.glob("*.json"):
            delta_data = json.loads(delta_file.read_text())
            deltas.append(
                {
                    "delta_id": delta_data["delta_id"],
                    "lesson_id": delta_data["lesson_id"],
                    "operations_count": len(delta_data["operations"]),
                    "created": delta_data["created"],
                    "source_insights": delta_data["source_insights"],
                }
            )

        return sorted(deltas, key=lambda d: d["created"], reverse=True)


@click.group()
def cli():
    """ACE Curator Agent - Generate delta operations from refined insights"""
    # Load environment variables from .env and .env.local
    load_dotenv()
    load_dotenv(".env.local", override=True)


@cli.command()
@click.option("--insight-id", required=True, help="Insight ID to process")
@click.option(
    "--dry-run", is_flag=True, help="Don't call Claude API, use mock operations"
)
def generate(insight_id: str, dry_run: bool):
    """Generate delta operations for single insight"""
    curator = CuratorAgent(dry_run=dry_run)

    # Load insight from storage
    insight = curator.storage.get_insight(insight_id, source_agent="refined")
    if not insight:
        click.echo(f"Error: Insight {insight_id} not found in refined storage")
        return

    click.echo(f"Generating delta for insight: {insight.title}")

    # Generate delta
    delta = curator.generate_delta(insight)

    # Save delta
    delta_path = curator.save_delta(delta)

    click.echo(f"\n✓ Delta generated: {delta.delta_id}")
    click.echo(f"  Lesson: {delta.lesson_id}")
    click.echo(f"  Operations: {len(delta.operations)}")
    click.echo(f"  Saved to: {delta_path}")
    click.echo(f"  Rationale: {delta.rationale}")

    # Show operations
    click.echo("\nOperations:")
    for i, op in enumerate(delta.operations, 1):
        click.echo(f"  {i}. {op.type.upper()} → {op.section}")
        if op.content:
            preview = op.content[:60] + "..." if len(op.content) > 60 else op.content
            click.echo(f"     Content: {preview}")


@cli.command()
@click.option("--status", default="approved", help="Process insights with this status")
@click.option(
    "--dry-run", is_flag=True, help="Don't call Claude API, use mock operations"
)
@click.option("--limit", type=int, help="Maximum insights to process")
def batch(status: str, dry_run: bool, limit: int | None):
    """Generate deltas for batch of insights"""
    curator = CuratorAgent(dry_run=dry_run)

    # List insights from storage
    insights = curator.storage.list_insights(status=status, source_agent="refined")

    if not insights:
        click.echo(f"No {status} refined insights found")
        return

    if limit:
        insights = insights[:limit]

    click.echo(f"Processing {len(insights)} {status} insights...")

    for insight_data in insights:
        insight = curator.storage.get_insight(
            insight_data["insight_id"], source_agent="refined"
        )
        if not insight:
            continue

        click.echo(f"\n→ {insight.title}")

        try:
            delta = curator.generate_delta(insight)
            curator.save_delta(delta)
            click.echo(
                f"  ✓ Delta {delta.delta_id}: {len(delta.operations)} operations"
            )
        except Exception as e:
            click.echo(f"  ✗ Error: {e}")

    click.echo("\n✓ Batch processing complete")


@cli.command("list")
@click.option(
    "--status",
    default="pending",
    type=click.Choice(["pending", "approved", "rejected"]),
    help="Delta status to list",
)
def list_cmd(status: str):
    """List deltas by status"""
    curator = CuratorAgent(dry_run=True)  # No API needed for list
    deltas = curator.list_deltas(status=status)

    if not deltas:
        click.echo(f"No {status} deltas found")
        return

    click.echo(f"\n{status.upper()} Deltas ({len(deltas)}):\n")

    for delta in deltas:
        click.echo(f"Delta: {delta['delta_id']}")
        click.echo(f"  Lesson: {delta['lesson_id']}")
        click.echo(f"  Operations: {delta['operations_count']}")
        click.echo(f"  Created: {delta['created']}")
        click.echo(f"  Source Insights: {', '.join(delta['source_insights'])}")
        click.echo()


if __name__ == "__main__":
    cli()
