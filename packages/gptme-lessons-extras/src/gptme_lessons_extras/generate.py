#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
# ]
# [tool.uv]
# exclude-newer = "2025-10-02T00:00:00Z"
# ///
"""
Lesson Generator

Converts conversation insights into draft lesson files following the established
lesson format. Generates lessons that can be reviewed and merged into the lessons/
directory.
"""

import sys

import json
from pathlib import Path
from typing import List, Optional

if __name__ == "__main__" and __package__ is None:
    workspace_root = Path(__file__).parent.parent.parent

import click

from gptme_lessons_extras.utils.llm import llm_author_reflect, llm_judge_score
from gptme_lessons_extras.utils.similarity import (
    deduplicate_lessons,
    find_similar_lessons,
)
from gptme_lessons_extras.utils.formatting import ensure_dir, generate_slug
from gptme_lessons_extras.utils.keywords import (
    extract_keywords_from_lesson,
    replace_placeholder_keywords,
    strip_preamble_before_frontmatter,
)
from gptme_lessons_extras.utils.evolution import gepa_lite_evolve, format_pareto_summary


# Removed: categorize_insight() - was used by old insight-based generation
# Removed: create_lesson_from_learnable_moment() - old format, heuristic-based
# Removed: format_lesson_markdown() - old template format
# These used the old lesson format and heuristic-based generation.
# We now use LLM Author exclusively for lesson generation.


def save_lesson_draft(lesson_markdown: str, title: str, output_dir: Path) -> Path:
    """Save lesson draft to file with extracted keywords in YAML frontmatter."""
    category_dir = output_dir / "patterns"  # Default category
    ensure_dir(category_dir)

    # Extract title and create slug
    title_line = title.split("\n")[0].replace("#", "").strip()
    slug = generate_slug(title_line)

    filename = f"{slug}.md"
    filepath = category_dir / filename

    # Avoid overwriting
    counter = 1
    while filepath.exists():
        filepath = category_dir / f"{slug}-{counter}.md"
        counter += 1

    # Strip any preamble before YAML frontmatter (safety fallback)
    clean_markdown = strip_preamble_before_frontmatter(lesson_markdown)

    # Extract keywords from lesson content
    keywords = extract_keywords_from_lesson(clean_markdown)

    # Replace placeholder keywords in YAML frontmatter
    updated_markdown = replace_placeholder_keywords(clean_markdown, keywords)

    with open(filepath, "w") as f:
        f.write(updated_markdown)

    return filepath


def generate_lessons_with_evolution(
    analysis_file: Path,
    output_dir: Path,
    min_confidence: float = 0.6,
    max_lessons: Optional[int] = None,
    num_variants: int = 5,
    check_existing: bool = True,
    existing_lessons_dir: Optional[Path] = None,
    similarity_threshold: float = 0.7,
    skip_duplicates: bool = True,
    judge_threshold: Optional[float] = None,
    verbose: bool = False,
) -> List[Path]:
    """Generate lesson drafts using GEPA-lite evolution loop.

    This function uses the GEPA-lite approach to:
    1. Generate multiple lesson variants (exploration)
    2. Judge each variant on quality dimensions
    3. Select best from Pareto front (exploitation)
    4. Optionally keep alternative variants

    Args:
        analysis_file: Path to conversation analysis JSON
        output_dir: Directory to save lesson drafts
        min_confidence: Minimum confidence for experiences (default: 0.6)
        max_lessons: Maximum number of lessons to generate (default: None)
        num_variants: Number of variants per experience (default: 5)
        check_existing: Check for similar existing lessons (default: True)
        existing_lessons_dir: Directory with existing lessons (default: None)
        similarity_threshold: Threshold for duplicate detection (default: 0.7)
        skip_duplicates: Skip generating duplicates (default: True)
        judge_threshold: Minimum average judge score (default: None)
        verbose: Print detailed progress (default: False)

    Returns:
        List of paths to generated lesson files
    """
    with open(analysis_file, "r") as f:
        analysis = json.load(f)

    # Support both formats: top-level "experiences" or nested "learnable_moments"
    experiences = analysis.get("experiences", [])
    if not experiences:
        # Fall back to metadata.learnable_moments for older format
        experiences = analysis.get("metadata", {}).get("learnable_moments", [])

    conversation_id = analysis.get("conversation_id", "unknown")

    # Filter by confidence
    experiences = [
        exp for exp in experiences if exp.get("confidence", 0.0) >= min_confidence
    ]

    if max_lessons:
        experiences = experiences[:max_lessons]

    if verbose:
        print("\n📚 Generating lessons with GEPA-lite evolution")
        print(f"  Source: {analysis_file.name}")
        print(f"  Experiences: {len(experiences)} (min confidence: {min_confidence})")
        print(f"  Variants per experience: {num_variants}")

    generated_files = []

    for i, moment in enumerate(experiences, 1):
        title = moment.get("title", "Unknown")

        if verbose:
            print(f"\n{'=' * 60}")
            print(f"Experience {i}/{len(experiences)}: {title}")
            print(f"{'=' * 60}")

        # TODO: Add preliminary similarity check using check_against_existing_lessons()
        # For now, let deduplication system handle similar lessons after generation

        # Run GEPA-lite evolution
        try:
            recommended_lesson, recommended_scores, pareto_front = gepa_lite_evolve(
                moment=moment,
                conversation_id=conversation_id,
                num_variants=num_variants,
                temperature=0.7,
                verbose=verbose,
            )

            # Check judge threshold if specified
            if judge_threshold:
                avg_score = sum(recommended_scores["scores"].values()) / len(
                    recommended_scores["scores"]
                )
                if avg_score < judge_threshold:
                    if verbose:
                        print(
                            f"  ⚠️  Skipping - avg score {avg_score:.3f} below threshold {judge_threshold}"
                        )
                    continue

            # Save recommended lesson
            filepath = save_lesson_draft(recommended_lesson, title, output_dir)
            generated_files.append(filepath)

            if verbose:
                print(f"\n  ✅ Saved recommended lesson: {filepath.name}")

                # Print Pareto front summary
                print(format_pareto_summary(pareto_front))

        except Exception as e:
            if verbose:
                print(f"  ❌ Error generating lesson: {e}")
            continue

    if verbose:
        print(f"\n{'=' * 60}")
        print("✨ Generation complete!")
        print(f"  Total lessons generated: {len(generated_files)}")
        print(f"  Output directory: {output_dir}")

    return generated_files


def generate_lessons_from_analysis(
    analysis_file: Path,
    output_dir: Path,
    min_confidence: float = 0.6,
    max_lessons: Optional[int] = None,
    check_existing: bool = True,
    existing_lessons_dir: Optional[Path] = None,
    similarity_threshold: float = 0.7,
    skip_duplicates: bool = True,
    judge_threshold: Optional[float] = None,
) -> List[Path]:
    """Generate lesson drafts from conversation analysis using LLM Author.

    This function exclusively uses trajectory-first learning:
    1. Loads experiences from analysis
    2. Filters by confidence threshold
    3. Checks against existing lessons (if enabled)
    4. Generates lessons using LLM Author
    5. Judges lesson quality (if threshold set)
    6. Saves lesson drafts that meet quality threshold

    Args:
        analysis_file: Path to conversation analysis JSON
        output_dir: Directory to save generated lessons
        min_confidence: Minimum confidence threshold for experiences
        max_lessons: Maximum number of lessons to generate
        check_existing: Whether to check against existing lessons
        existing_lessons_dir: Directory containing existing lessons (default: lessons/)
        similarity_threshold: Similarity threshold for duplicate detection (0.0-1.0)
        skip_duplicates: If True, skip generating similar lessons; if False, warn only
        judge_threshold: Minimum quality score (0.0-1.0) to save lessons (default: None - no filtering)

    Returns list of generated lesson file paths.
    """
    with open(analysis_file, "r", encoding="utf-8") as f:
        analysis = json.load(f)

    conversation_id = analysis["conversation_id"]
    # Support both "experiences" and "learnable_moments" keys
    experiences = analysis.get("metadata", {}).get("experiences", [])
    if not experiences:
        experiences = analysis.get("metadata", {}).get("learnable_moments", [])

    if not experiences:
        click.echo(f"No experiences found in {analysis_file.name}")
        click.echo("Run analyzer first to extract episodes and moments.")
        return []

    # Filter by confidence
    high_confidence_moments = [
        m for m in experiences if m.get("confidence", 0.0) >= min_confidence
    ]

    if not high_confidence_moments:
        click.echo(f"No experiences above confidence threshold {min_confidence}")
        return []

    # Limit number of lessons if requested
    if max_lessons is not None and len(high_confidence_moments) > max_lessons:
        high_confidence_moments = high_confidence_moments[:max_lessons]

    # Set up existing lessons directory if checking is enabled
    if check_existing:
        if existing_lessons_dir is None:
            existing_lessons_dir = Path("lessons")

        if not existing_lessons_dir.exists():
            click.echo(
                f"Warning: Existing lessons directory not found: {existing_lessons_dir}"
            )
            click.echo("Skipping duplicate check.")
            check_existing = False

    click.echo(f"Generating {len(high_confidence_moments)} lessons using LLM Author...")
    if check_existing:
        click.echo(f"  Checking against existing lessons in {existing_lessons_dir}")

    generated_lessons = []
    skipped_lessons = []

    for i, moment in enumerate(high_confidence_moments, 1):
        title = moment.get("title", "Lesson")
        click.echo(f"  [{i}/{len(high_confidence_moments)}] {title}...")

        # Check against existing lessons if enabled
        if check_existing and existing_lessons_dir is not None:
            from .utils.similarity import check_against_existing_lessons

            # Create a temporary lesson info for similarity checking
            temp_lesson = {
                "title": title,
                "context": moment.get("context", ""),
                "filepath": Path("temp"),  # Dummy path for checking
            }

            similar_lessons = check_against_existing_lessons(
                temp_lesson, existing_lessons_dir, threshold=similarity_threshold
            )

            if similar_lessons:
                top_match = similar_lessons[0]
                similarity_pct = int(top_match["similarity"] * 100)
                click.echo(
                    f"    ⚠️  Similar lesson found ({similarity_pct}% match): {top_match['title']}"
                )
                click.echo(
                    f"       Location: {top_match['filepath'].relative_to(existing_lessons_dir)}"
                )

                if skip_duplicates:
                    click.echo("    ⏭️  Skipping (duplicate detection enabled)")
                    skipped_lessons.append({"moment": moment, "similar_to": top_match})
                    continue
                else:
                    click.echo("    ⚠️  Generating anyway (--no-skip-duplicates)")

        # Generate the lesson
        try:
            lesson_md = llm_author_reflect(moment, conversation_id)

            # Judge lesson quality if threshold is set
            if judge_threshold is not None:
                scores = llm_judge_score(lesson_md, moment, conversation_id)
                avg_score = sum(scores["scores"].values()) / len(scores["scores"])

                if avg_score < judge_threshold:
                    click.echo(
                        f"    ⚠️  Low quality score: {avg_score:.2f} (threshold: {judge_threshold:.2f})"
                    )
                    click.echo("    ⏭️  Skipping (below quality threshold)")
                    skipped_lessons.append(
                        {"moment": moment, "reason": "low_quality", "score": avg_score}
                    )
                    continue
                else:
                    click.echo(f"    ✓ Quality score: {avg_score:.2f}")

            filepath = save_lesson_draft(lesson_md, title, output_dir)
            generated_lessons.append(filepath)
            click.echo(f"    ✓ Saved to {filepath}")
        except Exception as e:
            click.echo(f"    ✗ Error: {e}", err=True)
            continue

    # Report summary
    if skipped_lessons:
        similarity_skipped = [s for s in skipped_lessons if "similar_to" in s]
        quality_skipped = [
            s for s in skipped_lessons if s.get("reason") == "low_quality"
        ]

        if similarity_skipped:
            click.echo(
                f"\n⏭️  Skipped {len(similarity_skipped)} lessons due to similarity with existing lessons"
            )
            click.echo("   Use --no-skip-duplicates to generate anyway")

        if quality_skipped:
            click.echo(
                f"\n⏭️  Skipped {len(quality_skipped)} lessons due to low quality scores"
            )
            for item in quality_skipped:
                click.echo(
                    f"   - {item['moment'].get('title', 'Untitled')}: {item['score']:.2f}"
                )
            click.echo(f"   Quality threshold: {judge_threshold:.2f}")

    return generated_lessons


@click.group()
def cli():
    """Lesson generation and evaluation tools.

    Commands:
      workflow  - End-to-end: analyze → generate → judge (recommended)
      generate  - Generate lessons from existing analysis
      judge     - Score an existing lesson
    """
    pass


@cli.command()
@click.argument("conversation_path")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default="knowledge/meta/lessons-draft",
    help="Output directory for draft lessons",
)
@click.option(
    "--min-confidence",
    "-c",
    type=float,
    default=0.6,
    help="Minimum confidence threshold for experiences",
)
@click.option(
    "--max-lessons",
    type=int,
    default=3,
    help="Maximum number of lessons to generate",
)
@click.option(
    "--no-check-existing",
    is_flag=True,
    help="Skip checking against existing lessons",
)
@click.option(
    "--existing-lessons-dir",
    type=click.Path(exists=True),
    default="lessons",
    help="Directory containing existing lessons",
)
@click.option(
    "--similarity-threshold",
    type=float,
    default=0.7,
    help="Similarity threshold for duplicate detection (0.0-1.0)",
)
@click.option(
    "--no-skip-duplicates",
    is_flag=True,
    help="Generate lessons even if similar ones exist (warn only)",
)
@click.option(
    "--judge-threshold",
    type=float,
    default=0.75,
    help="Minimum quality score (0.0-1.0) to save lessons (default: 0.75, set to 0 to disable)",
)
@click.option(
    "--no-judge",
    is_flag=True,
    help="Skip judging generated lessons in final summary",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def workflow(
    conversation_path: str,
    output_dir: str,
    min_confidence: float,
    max_lessons: int,
    no_check_existing: bool,
    existing_lessons_dir: str,
    similarity_threshold: float,
    no_skip_duplicates: bool,
    judge_threshold: float,
    no_judge: bool,
    verbose: bool,
):
    """End-to-end workflow: analyze → extract moments → generate lessons → judge.

    This is the recommended way to generate lessons from conversations.

    CONVERSATION_PATH can be:
      - A conversation directory (e.g., logs/2025-10-02-singing-blue-fish/)
      - The string 'latest' to analyze the most recent conversation

    Example:
        ./scripts/generate-lesson.py workflow latest
        ./scripts/generate-lesson.py workflow logs/2025-10-02-*/
    """
    from .analysis.conversations import analyze_conversation_log, save_analysis

    # Resolve conversation path
    conversation_path_obj = Path(conversation_path)

    if conversation_path == "latest":
        # Try multiple log locations in order of preference
        logs_dirs = [
            Path.home()
            / ".local"
            / "share"
            / "gptme"
            / "logs",  # Default gptme location
            Path("logs"),  # Local logs directory (run.sh)
        ]

        logs_dir = None
        for potential_dir in logs_dirs:
            if potential_dir.exists():
                logs_dir = potential_dir
                break

        if logs_dir is None:
            click.echo("Error: No logs directory found", err=True)
            click.echo("Tried: " + ", ".join(str(d) for d in logs_dirs), err=True)
            sys.exit(1)

        conversation_dirs = [d for d in logs_dir.iterdir() if d.is_dir()]
        if not conversation_dirs:
            click.echo("Error: No conversation directories found", err=True)
            sys.exit(1)

        conversation_path_obj = max(conversation_dirs, key=lambda d: d.stat().st_mtime)
        click.echo(f"Using latest conversation: {conversation_path_obj.name}")
        if logs_dir != Path("logs"):
            click.echo(f"From: {logs_dir}")

    if not conversation_path_obj.exists():
        click.echo(f"Error: Path not found: {conversation_path_obj}", err=True)
        sys.exit(1)

    # Step 1: Analyze conversation
    click.echo("\n=== Step 1: Analyzing conversation ===")
    try:
        analysis = analyze_conversation_log(conversation_path_obj)
        analysis_dir = Path("knowledge/meta/conversations")
        analysis_file = save_analysis(analysis, analysis_dir)
        click.echo(f"✓ Analysis saved: {analysis_file}")

        if verbose:
            click.echo(f"  Episodes: {analysis.metadata.get('episodes_count', 0)}")
            click.echo(
                f"  Experiences: {analysis.metadata.get('experiences_count', 0)}"
            )
    except Exception as e:
        click.echo(f"✗ Error analyzing conversation: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    # Step 2: Generate lessons
    click.echo("\n=== Step 2: Generating lessons ===")
    try:
        generated_files = generate_lessons_from_analysis(
            analysis_file,
            Path(output_dir),
            min_confidence=min_confidence,
            max_lessons=max_lessons,
            check_existing=not no_check_existing,
            existing_lessons_dir=Path(existing_lessons_dir)
            if existing_lessons_dir
            else None,
            similarity_threshold=similarity_threshold,
            skip_duplicates=not no_skip_duplicates,
            judge_threshold=judge_threshold if judge_threshold > 0 else None,
        )

        if not generated_files:
            click.echo("✗ No lessons generated (no high-confidence moments)")
            sys.exit(0)

        click.echo(f"✓ Generated {len(generated_files)} lesson drafts")
    except Exception as e:
        click.echo(f"✗ Error generating lessons: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    # Step 3: Judge lessons (optional)
    if not no_judge:
        click.echo("\n=== Step 3: Judging lessons ===")
        try:
            # Load analysis for context
            with open(analysis_file, "r", encoding="utf-8") as f:
                analysis_data = json.load(f)

            experiences = analysis_data.get("metadata", {}).get("experiences", [])
            conversation_id = analysis_data["conversation_id"]

            lesson_scores = []
            for i, lesson_file in enumerate(generated_files):
                click.echo(f"  Judging lesson {i + 1}/{len(generated_files)}...")

                with open(lesson_file, "r", encoding="utf-8") as f:
                    lesson_md = f.read()

                # Use corresponding experience for context
                moment = experiences[i] if i < len(experiences) else experiences[0]

                try:
                    scores = llm_judge_score(lesson_md, moment, conversation_id)
                    avg_score = sum(scores["scores"].values()) / len(scores["scores"])
                    lesson_scores.append((lesson_file.name, avg_score, scores))
                    click.echo(f"    ✓ Score: {avg_score:.2f}")
                except Exception as e:
                    click.echo(f"    ✗ Error: {e}", err=True)
                    continue

            # Print summary
            click.echo("\n=== Summary ===")
            click.echo(f"Conversation: {conversation_id}")
            click.echo(f"Analysis: {analysis_file}")
            click.echo(f"Lessons generated: {len(generated_files)}")
            click.echo(f"Lessons judged: {len(lesson_scores)}")

            if lesson_scores:
                click.echo("\nLesson scores:")
                for name, avg_score, scores in sorted(
                    lesson_scores, key=lambda x: -x[1]
                ):
                    click.echo(f"  {name}: {avg_score:.2f}")
                    if verbose:
                        for dim, score in scores["scores"].items():
                            click.echo(f"    - {dim}: {score:.2f}")
        except Exception as e:
            click.echo(f"✗ Error judging lessons: {e}", err=True)
            if verbose:
                import traceback

                traceback.print_exc()
    else:
        click.echo("\n=== Summary ===")
        click.echo(f"Conversation: {analysis.conversation_id}")
        click.echo(f"Analysis: {analysis_file}")
        click.echo(f"Lessons generated: {len(generated_files)}")

    # Next steps
    click.echo("\n=== Next Steps ===")
    click.echo("1. Review generated lessons in: knowledge/meta/lessons-draft/")
    click.echo("2. Refine lesson content and format")
    click.echo("3. Move reviewed lessons to lessons/{category}/")
    click.echo("4. Run: gptme-rag index lessons/")


@cli.command()
@click.argument("analysis_file", type=click.Path(exists=True))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default="knowledge/meta/lessons-draft",
    help="Output directory for draft lessons",
)
@click.option(
    "--min-confidence",
    "-c",
    type=float,
    default=0.6,
    help="Minimum confidence threshold (0.0-1.0)",
)
@click.option(
    "--max-lessons",
    type=int,
    default=None,
    help="Maximum number of lessons to generate (useful for testing)",
)
@click.option(
    "--no-check-existing",
    is_flag=True,
    help="Skip checking against existing lessons",
)
@click.option(
    "--existing-lessons-dir",
    type=click.Path(exists=True),
    default="lessons",
    help="Directory containing existing lessons",
)
@click.option(
    "--similarity-threshold",
    type=float,
    default=0.7,
    help="Similarity threshold for duplicate detection (0.0-1.0)",
)
@click.option(
    "--no-skip-duplicates",
    is_flag=True,
    help="Generate lessons even if similar ones exist (warn only)",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def generate(
    analysis_file: str,
    output_dir: str,
    min_confidence: float,
    max_lessons: Optional[int],
    no_check_existing: bool,
    existing_lessons_dir: str,
    similarity_threshold: float,
    no_skip_duplicates: bool,
    verbose: bool,
):
    """Generate lesson drafts from conversation analysis using LLM Author.

    Uses trajectory-first approach: extracts experiences from episodes
    and generates lessons following the new signals-first template format.

    ANALYSIS_FILE should be a JSON file produced by analyze-conversation.py

    Example:
        ./scripts/generate-lesson.py generate knowledge/meta/conversations/20250930-*.json
    """
    try:
        generated = generate_lessons_from_analysis(
            Path(analysis_file),
            Path(output_dir),
            min_confidence=min_confidence,
            max_lessons=max_lessons,
            check_existing=not no_check_existing,
            existing_lessons_dir=Path(existing_lessons_dir)
            if existing_lessons_dir
            else None,
            similarity_threshold=similarity_threshold,
            skip_duplicates=not no_skip_duplicates,
        )

        if generated:
            click.echo(f"✓ Generated {len(generated)} lesson drafts:")
            for filepath in generated:
                click.echo(f"  - {filepath}")

            click.echo(f"\nReview drafts in: {output_dir}")
            click.echo("To integrate into main lessons directory:")
            click.echo("  1. Review and refine the lesson content")
            click.echo("  2. Move to lessons/{category}/")
            click.echo("  3. Run: gptme-rag index lessons/")
        else:
            click.echo("No lessons generated (no insights or confidence too low)")

        if verbose:
            click.echo(f"\nAnalyzed: {Path(analysis_file).name}")
            click.echo(f"Confidence threshold: {min_confidence}")
            click.echo(f"Output directory: {output_dir}")

    except Exception as e:
        click.echo(f"Error generating lessons: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.argument("analysis_file", type=click.Path(exists=True))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default="knowledge/meta/lessons-draft",
    help="Output directory for draft lessons",
)
@click.option(
    "--min-confidence",
    "-c",
    type=float,
    default=0.6,
    help="Minimum confidence threshold for experiences",
)
@click.option(
    "--max-lessons",
    type=int,
    default=3,
    help="Maximum number of lessons to generate",
)
@click.option(
    "--num-variants",
    "-n",
    type=int,
    default=5,
    help="Number of variants to generate per experience",
)
@click.option(
    "--no-check-existing",
    is_flag=True,
    help="Skip checking against existing lessons",
)
@click.option(
    "--existing-lessons-dir",
    type=click.Path(exists=True),
    default="lessons",
    help="Directory containing existing lessons",
)
@click.option(
    "--similarity-threshold",
    type=float,
    default=0.7,
    help="Similarity threshold for duplicate detection (0.0-1.0)",
)
@click.option(
    "--no-skip-duplicates",
    is_flag=True,
    help="Generate even if similar lessons exist",
)
@click.option(
    "--judge-threshold",
    type=float,
    help="Minimum average judge score to keep lesson (0.0-1.0)",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def evolve(
    analysis_file: str,
    output_dir: str,
    min_confidence: float,
    max_lessons: int,
    num_variants: int,
    no_check_existing: bool,
    existing_lessons_dir: str,
    similarity_threshold: float,
    no_skip_duplicates: bool,
    judge_threshold: float,
    verbose: bool,
):
    """Generate lessons using GEPA-lite evolution loop.

    This command uses the GEPA-lite approach to generate high-quality lessons:

    1. Generate N variants per experience (exploration)
    2. Judge each variant on 6 quality dimensions
    3. Compute Pareto front (non-dominated variants)
    4. Select best variant (weighted score)

    The evolution loop produces more robust lessons than single-shot generation
    by exploring the quality space and selecting from diverse high-performers.

    Example:
        # Basic usage (5 variants per experience)
        ./scripts/generate-lesson.py evolve analysis.json

        # More exploration (10 variants)
        ./scripts/generate-lesson.py evolve analysis.json -n 10

        # With quality threshold
        ./scripts/generate-lesson.py evolve analysis.json --judge-threshold 0.7

        # Verbose output showing all scores
        ./scripts/generate-lesson.py evolve analysis.json -v
    """
    try:
        generated = generate_lessons_with_evolution(
            analysis_file=Path(analysis_file),
            output_dir=Path(output_dir),
            min_confidence=min_confidence,
            max_lessons=max_lessons,
            num_variants=num_variants,
            check_existing=not no_check_existing,
            existing_lessons_dir=Path(existing_lessons_dir)
            if existing_lessons_dir
            else None,
            similarity_threshold=similarity_threshold,
            skip_duplicates=not no_skip_duplicates,
            judge_threshold=judge_threshold,
            verbose=verbose,
        )

        if generated:
            click.echo(
                f"\n✅ Generated {len(generated)} lessons using GEPA-lite evolution"
            )
            for filepath in generated:
                click.echo(f"  - {filepath}")

            click.echo(f"\n📁 Review drafts in: {output_dir}")
            click.echo("To integrate into main lessons directory:")
            click.echo("  1. Review and refine the lesson content")
            click.echo("  2. Move to lessons/{category}/")
            click.echo("  3. Run: gptme-rag index lessons/")
        else:
            click.echo("No lessons generated (no insights or confidence too low)")

    except Exception as e:
        click.echo(f"❌ Error generating lessons: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.argument("analysis_file", type=click.Path(exists=True))
@click.argument("lesson_file", type=click.Path(exists=True))
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def judge(analysis_file: str, lesson_file: str, verbose: bool):
    """Score an existing lesson file using LLM-as-judge.

    ANALYSIS_FILE should be a JSON file with experiences
    LESSON_FILE should be a markdown lesson file

    Example:
        ./scripts/learn/generate.py judge knowledge/meta/conversations/20250930-*.json \\
            knowledge/meta/lessons-draft/patterns/celebrating-breakthrough-moments.md
    """
    try:
        with open(analysis_file, "r", encoding="utf-8") as f:
            analysis = json.load(f)

        conversation_id = analysis["conversation_id"]
        experiences = analysis.get("metadata", {}).get("experiences", [])

        if not experiences:
            click.echo("Error: No experiences found in analysis file", err=True)
            sys.exit(1)

        with open(lesson_file, "r", encoding="utf-8") as f:
            lesson_markdown = f.read()

        moment = experiences[0]

        click.echo(f"Scoring lesson: {lesson_file}")
        click.echo(f"Using context from: {conversation_id}")
        click.echo()

        click.echo("Calling LLM judge...")
        scores = llm_judge_score(lesson_markdown, moment, conversation_id)

        click.echo()
        click.echo("=== Judge Scores ===")
        click.echo()
        for dimension, score in scores["scores"].items():
            bar = "█" * int(score * 20)
            click.echo(f"{dimension:15s}: {score:.2f} {bar}")

        click.echo()
        click.echo("=== Rationale ===")
        click.echo(scores.get("rationale", "No rationale provided"))

        if scores.get("notes"):
            click.echo()
            click.echo("=== Notes ===")
            click.echo(scores["notes"])

        avg_score = sum(scores["scores"].values()) / len(scores["scores"])
        click.echo()
        click.echo(f"Average score: {avg_score:.2f}")

    except Exception as e:
        click.echo(f"Error scoring lesson: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.option(
    "--lesson-dir",
    type=click.Path(exists=True),
    default="knowledge/meta/lessons-draft",
    help="Directory containing lesson drafts",
)
@click.option(
    "--archive-dir",
    type=click.Path(),
    default="knowledge/meta/lessons-archive",
    help="Directory to move duplicates to",
)
@click.option(
    "--threshold",
    "-t",
    type=float,
    default=0.7,
    help="Similarity threshold (0.0-1.0)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be done without making changes",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def deduplicate(
    lesson_dir: str,
    archive_dir: str,
    threshold: float,
    dry_run: bool,
    verbose: bool,
):
    """Deduplicate similar lessons by archiving duplicates.

    Finds clusters of similar lessons (above threshold), selects the best
    from each cluster (by judge score or recency), and archives the rest.

    Example:
        ./scripts/generate-lesson.py deduplicate --dry-run
        ./scripts/generate-lesson.py deduplicate --threshold 0.8
    """
    lesson_dir_path = Path(lesson_dir)
    archive_dir_path = Path(archive_dir)

    # First, show what clusters exist
    click.echo(f"Scanning for similar lessons (threshold: {threshold})...")
    clusters = find_similar_lessons(lesson_dir_path, threshold)

    if not clusters:
        click.echo("✓ No similar lessons found. All lessons are unique!")
        return

    click.echo(f"\nFound {len(clusters)} similarity clusters:")
    for i, cluster in enumerate(clusters, 1):
        click.echo(f"\nCluster {i} ({len(cluster)} lessons):")
        for lesson in cluster:
            click.echo(f"  - {lesson['title']}")
            if verbose:
                click.echo(f"    File: {lesson['filepath'].name}")

    # Deduplicate
    if dry_run:
        click.echo("\n=== Dry Run ===")
        click.echo("Would archive the following duplicates:")
    else:
        click.echo("\n=== Deduplicating ===")

    result = deduplicate_lessons(
        lesson_dir_path,
        archive_dir_path,
        threshold=threshold,
        dry_run=dry_run,
    )

    # Report results
    for i, cluster_info in enumerate(result["clusters"], 1):
        click.echo(f"\nCluster {i}:")
        click.echo(f"  ✓ Kept: {cluster_info['kept']}")
        if cluster_info["archived"]:
            for archived in cluster_info["archived"]:
                if dry_run:
                    click.echo(f"  → Would archive: {archived}")
                else:
                    click.echo(f"  → Archived: {archived}")

    # Summary
    click.echo("\n=== Summary ===")
    click.echo(f"Clusters found: {result['clusters_found']}")
    click.echo(f"Lessons kept: {result['lessons_kept']}")
    if dry_run:
        click.echo(f"Lessons that would be archived: {result['lessons_archived']}")
    else:
        click.echo(f"Lessons archived: {result['lessons_archived']}")

    if not dry_run:
        click.echo(f"\nArchived lessons moved to: {archive_dir}")


if __name__ == "__main__":
    cli()
