"""Section attribution in the book chunker (#2362 W3)."""

from __future__ import annotations

from gptme_wisdom import parse_book_text


def _words(tag: str, n: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def test_empty_section_gets_no_chunk_of_previous_sections_overlap() -> None:
    # "1.2 Beta" has no body: a chunk labelled with it could only hold the
    # overlap carried over from "1.1 Alpha".
    text = (
        f"# Chapter One\n\n### 1.1 Alpha\n{_words('alpha', 200)}\n\n"
        f"### 1.2 Beta\n\n### 1.2.1 Gamma\n{_words('gamma', 200)}\n"
    )
    docs = parse_book_text(text, source="s", title="t", url="")
    assert not [d for d in docs if d.section == "1.2 Beta"]
    gamma = [d for d in docs if d.section == "1.2.1 Gamma"]
    assert gamma and any("gamma0" in d.content for d in gamma)


def test_skipped_exercises_section_emits_no_chunk() -> None:
    text = (
        f"# Chapter One\n\n### 1.1 Alpha\n{_words('alpha', 200)}\n\n"
        f"### Exercises\n1. Do x.\n\n# Chapter Two\n{_words('beta', 200)}\n"
    )
    docs = parse_book_text(text, source="s", title="t", url="")
    assert not [d for d in docs if d.section == "Exercises"]
    assert any("beta0" in d.content for d in docs)
