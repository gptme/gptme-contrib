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
# Markdown ATX headers: capture level + text. CommonMark allows up to three
# leading spaces; four or more is an indented code block, where a leading "#"
# is a comment, not a heading.
_BOOK_HEADER_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*$")
# Numbered section headers like "2.1.3 Foo" (chapter-style "1 Foo" excluded —
# too easily confused with ordinary numbered prose).
_BOOK_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+\S.*$")
# Number words accepted as chapter numbers, so "Chapter One" and "Part Two"
# stay chapters. A number (digit, Roman, or word) is required so prose like
# "Part of the problem ..." is not treated as a chapter marker.
_CHAPTER_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    "fifty|sixty|seventy|eighty|ninety|hundred"
)
# Prose chapter markers ("Chapter 2: ...", "Part I ...", "Chapter One").
_BOOK_CHAPTER_RE = re.compile(
    rf"^(chapter|part)\s+(\d+|[ivxlc]+|(?:{_CHAPTER_NUMBER_WORDS}))\b", re.IGNORECASE
)
# Fenced code blocks: ``` or ~~~ (three or more). Content inside is code, so a
# leading "#" there is a comment, not a heading.
_BOOK_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
# Characters that may introduce a chapter title after the number ("Chapter 2: X").
_CHAPTER_TITLE_SEPARATORS = ":.-–—|"


def _chapter_marker(line: str) -> str | None:
    """Return ``line`` if it looks like a prose chapter heading, else None.

    A number token after "chapter"/"part" is not enough: hard-wrapped prose
    such as "Chapter 3 showed how lists are mutable, so" or "Part one of the
    solution is to copy the list" also starts that way. Require the remainder
    to be empty, start with a title separator, or start with a non-lowercase
    character (i.e. a title, not a sentence continuation).
    """
    m = _BOOK_CHAPTER_RE.match(line)
    if not m:
        return None
    rest = line[m.end() :].strip()
    if rest and rest[0] not in _CHAPTER_TITLE_SEPARATORS:
        first_alpha = next((c for c in rest if c.isalpha()), "")
        if first_alpha.islower():
            return None
    return line


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

    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _BOOK_FENCE_RE.match(line):
            in_fence = not in_fence
            continue

        header_text: str | None = None
        is_chapter = False
        # Inside a fenced code block nothing is a heading: a column-0
        # "# comment" in a listing is code, not an H1 chapter.
        if not in_fence:
            # Markdown headers may be indented by up to three spaces; deeper
            # indentation means a code listing, where "# ..." is a comment.
            hm = _BOOK_HEADER_RE.match(raw_line)
            if hm:
                header_text = hm.group(2).strip()
                is_chapter = len(hm.group(1)) <= 2
            elif _chapter_marker(line):
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
