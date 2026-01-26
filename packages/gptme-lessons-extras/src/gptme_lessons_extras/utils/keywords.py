"""Keyword extraction utilities for lesson generation."""

import re
from typing import List, Set


def extract_keywords_from_lesson(lesson_content: str) -> List[str]:
    """Extract meaningful keywords from lesson content.

    Extracts keywords from:
    - Title (after # heading)
    - Rule section
    - Context section

    Returns list of lowercase keywords sorted by relevance.
    """
    keywords: Set[str] = set()

    # Extract title keywords (after first #)
    title_match = re.search(r"^#\s+(.+)$", lesson_content, re.MULTILINE)
    if title_match:
        title = title_match.group(1)
        # Remove common words and extract meaningful terms
        title_words = extract_meaningful_words(title)
        keywords.update(title_words[:3])  # Top 3 from title

    # Extract from Rule section
    rule_match = re.search(
        r"^##\s+Rule\s*\n(.+?)(?=\n##|\Z)", lesson_content, re.MULTILINE | re.DOTALL
    )
    if rule_match:
        rule_text = rule_match.group(1).strip()
        rule_words = extract_meaningful_words(rule_text)
        keywords.update(rule_words[:2])  # Top 2 from rule

    # Extract from Context section
    context_match = re.search(
        r"^##\s+Context\s*\n(.+?)(?=\n##|\Z)", lesson_content, re.MULTILINE | re.DOTALL
    )
    if context_match:
        context_text = context_match.group(1).strip()
        context_words = extract_meaningful_words(context_text)
        keywords.update(context_words[:2])  # Top 2 from context

    # Return sorted keywords (max 7)
    return sorted(list(keywords))[:7]


def extract_meaningful_words(text: str) -> List[str]:
    """Extract meaningful words from text, filtering out common words.

    Returns lowercase words sorted by frequency.
    """
    # Common words to ignore
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "that",
        "the",
        "to",
        "was",
        "will",
        "with",
        "when",
        "where",
        "which",
        "who",
        "this",
        "these",
        "those",
        "they",
        "them",
        "their",
        "should",
        "must",
        "can",
        "could",
        "would",
        "always",
        "never",
        "not",
    }

    # Extract words (alphanumeric, keep hyphens)
    words = re.findall(r"\b[a-z][a-z0-9\-]*\b", text.lower())

    # Filter out stopwords and short words
    meaningful = [w for w in words if len(w) >= 3 and w not in stopwords]

    # Count frequency and return sorted by frequency
    from collections import Counter

    word_counts = Counter(meaningful)

    return [word for word, _ in word_counts.most_common()]


def strip_preamble_before_frontmatter(lesson_content: str) -> str:
    """Remove any preamble text before the YAML frontmatter.

    The lesson should start directly with '---' for the YAML frontmatter.
    This strips anything that appears before it.
    """
    # Find the first occurrence of '---' (start of YAML frontmatter)
    frontmatter_start = lesson_content.find("---")

    if frontmatter_start > 0:
        # There's text before the frontmatter, strip it
        return lesson_content[frontmatter_start:]

    # No preamble found, return as-is
    return lesson_content


def replace_placeholder_keywords(lesson_content: str, keywords: List[str]) -> str:
    """Replace placeholder keywords in YAML frontmatter with actual keywords.

    Replaces [keyword1, keyword2, keyword3] with actual keywords.
    """
    # Find and replace keywords in frontmatter
    pattern = r"(---\s*\nmatch:\s*\nkeywords:\s*)\[keyword1, keyword2, keyword3\]"
    replacement = r"\1[" + ", ".join(keywords) + "]"

    updated_content = re.sub(
        pattern, replacement, lesson_content, count=1, flags=re.MULTILINE
    )

    return updated_content
