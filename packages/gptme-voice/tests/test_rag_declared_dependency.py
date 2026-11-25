"""Guard the retrieval dependency that ``rag.py`` requires.

``gptme_voice.rag`` imports ``gptme_rag`` and ranks the recency corpus with its
lexical TF-IDF backend. That backend lives behind the ``lexical`` extra. When
the extra is missing, ``_search_sync`` catches ``LexicalDependencyMissing`` and
falls back to pure recency order, so ``workspace_search`` still answers "ok"
while being unable to match a query by content.

That failure is silent: the tool reports ``backend: "recency"`` exactly as it
does for a query with no usable terms, so nothing downstream can tell the
difference. It cost a live call on 2026-11-25, where a journal written 25
minutes earlier was present in the corpus but ranked 53rd of the 80-file
recency window and never surfaced.

This test keeps the declaration honest so a fresh install cannot reproduce it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_requirements() -> list[str]:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data["project"]["dependencies"])


def test_voice_declares_its_retrieval_dependency() -> None:
    requirements = _declared_requirements()
    rag = [
        req for req in requirements if req.split(">=")[0].split("[")[0] == "gptme-rag"
    ]
    assert rag, (
        "gptme-voice must declare gptme-rag: rag.py imports gptme_rag and will "
        "silently disable workspace_search when it is absent"
    )


def test_retrieval_dependency_requests_the_lexical_extra() -> None:
    requirements = _declared_requirements()
    rag = next(
        req for req in requirements if req.split(">=")[0].split("[")[0] == "gptme-rag"
    )
    assert "lexical" in rag, (
        "gptme-rag must be declared with the `lexical` extra; without "
        "scikit-learn the lexical ranker raises LexicalDependencyMissing and "
        "workspace_search degrades to newest-first recency order"
    )


@pytest.mark.parametrize("extra", ["lexical"])
def test_rag_package_exposes_the_extra(extra: str) -> None:
    """The requested extra must exist upstream, or the pin is unsatisfiable."""
    rag_pyproject = _PYPROJECT.parents[1] / "gptme-rag" / "pyproject.toml"
    if not rag_pyproject.is_file():
        pytest.skip("gptme-rag is not a sibling package in this checkout")
    with rag_pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    extras = data.get("project", {}).get("optional-dependencies", {})
    assert extra in extras, f"gptme-rag does not define a `{extra}` extra"
