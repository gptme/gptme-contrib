"""Book chunker — splits raw text into BM25-searchable BookDocument passages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["BookDocument", "estimate_tokens", "parse_book_text"]


@dataclass
class BookDocument:
    """A chunked passage from a reference book.

    Each instance is one BM25-searchable chunk of ~800-1200 tokens carrying
    enough provenance (chapter/section/url/license) to cite the source.
    """

    source: str  # short slug, e.g. "sicp", "ostep"
    title: str  # full book title
    chapter: str  # nearest chapter/heading, "" if unknown
    section: str  # nearest sub-section heading, "" if unknown
    content: str  # the chunk text (~800-1200 tokens)
    url: str  # source URL for citation
    license: str = "unknown"  # e.g. "CC BY-SA 4.0"
    page: int | None = None  # page number if known
    metadata: dict = field(default_factory=dict)


# English prose averages ~0.75 words per token, so tokens ≈ words / 0.75.
_WORDS_PER_TOKEN = 0.75

# Headings that introduce retrieval noise — skip their bodies.
_BOOK_NOISE_RE = re.compile(
    r"^\s*(exercises?|bibliography|references|index|acknowledg)", re.IGNORECASE
)
# Markdown ATX headers: capture level + text.
_BOOK_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
# Numbered section headers like "2.1.3 Foo" (chapter-style "1 Foo" excluded —
# too easily confused with ordinary numbered prose).
_BOOK_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+\S.*$")
# Prose chapter markers ("Chapter 2: ...", "Part I ...").
_BOOK_CHAPTER_RE = re.compile(r"^(chapter|part)\b", re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """Estimate token count using a words-per-token heuristic."""
    words = len(text.split())
    return max(1, round(words / _WORDS_PER_TOKEN)) if words else 0


def parse_book_text(
    text: str,
    *,
    source: str,
    title: str,
    url: str,
    license: str = "unknown",
    target_tokens: int = 1000,
    overlap_tokens: int = 100,
    min_chunk_tokens: int = 50,
) -> list[BookDocument]:
    """Chunk raw book text into overlapping BookDocument passages.

    Strategy:
    - Track the active chapter/section from markdown or numbered headers.
    - Hard-split at chapter boundaries; size-split long runs at ~target_tokens
      with overlap_tokens of carry-over to preserve boundary context.
    - Skip exercise/bibliography/index bodies (noise for retrieval).
    - Drop trailing fragments below min_chunk_tokens.
    """
    target_words = max(1, round(target_tokens * _WORDS_PER_TOKEN))
    overlap_words = max(0, round(overlap_tokens * _WORDS_PER_TOKEN))
    min_words = max(1, round(min_chunk_tokens * _WORDS_PER_TOKEN))

    docs: list[BookDocument] = []
    buf: list[str] = []
    buf_chapter = ""
    buf_section = ""
    chapter = ""
    section = ""
    skipping = False

    def emit(*, keep_overlap: bool) -> None:
        nonlocal buf
        if len(buf) >= min_words:
            docs.append(
                BookDocument(
                    source=source,
                    title=title,
                    chapter=buf_chapter,
                    section=buf_section,
                    content=" ".join(buf),
                    url=url,
                    license=license,
                )
            )
            buf = buf[-overlap_words:] if (keep_overlap and overlap_words) else []
        elif not keep_overlap:
            buf = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        header_text: str | None = None
        is_chapter = False
        hm = _BOOK_HEADER_RE.match(line)
        if hm:
            header_text = hm.group(2).strip()
            is_chapter = len(hm.group(1)) <= 2
        elif _BOOK_CHAPTER_RE.match(line):
            header_text, is_chapter = line, True
        elif _BOOK_NUMBERED_RE.match(line):
            header_text, is_chapter = line, False

        if header_text is not None:
            skipping = bool(_BOOK_NOISE_RE.match(header_text))
            if is_chapter:
                emit(keep_overlap=False)
                chapter, section = header_text, ""
            else:
                emit(keep_overlap=True)
                section = header_text
            buf_chapter, buf_section = chapter, section
            continue

        if skipping:
            continue

        if not buf:
            buf_chapter, buf_section = chapter, section
        for word in line.split():
            buf.append(word)
            if len(buf) >= target_words:
                emit(keep_overlap=True)
                if not buf:
                    buf_chapter, buf_section = chapter, section

    emit(keep_overlap=False)
    return docs
