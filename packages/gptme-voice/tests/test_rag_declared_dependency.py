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
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_requirements() -> list[Requirement]:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return [Requirement(req) for req in data["project"]["dependencies"]]


def test_voice_declares_its_retrieval_dependency() -> None:
    requirements = _declared_requirements()
    rag = [req for req in requirements if canonicalize_name(req.name) == "gptme-rag"]
    assert rag, (
        "gptme-voice must declare gptme-rag: rag.py imports gptme_rag and will "
        "silently disable workspace_search when it is absent"
    )


def test_retrieval_dependency_requests_the_lexical_extra() -> None:
    requirements = _declared_requirements()
    rag = next(
        (req for req in requirements if canonicalize_name(req.name) == "gptme-rag"),
        None,
    )
    assert rag is not None, "gptme-voice must declare gptme-rag"
    assert "lexical" in rag.extras, (
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


@pytest.mark.parametrize(
    "requirement, has_lexical",
    [
        ("gptme-rag[lexical]~=0.5", True),
        ("gptme_rag[lexical]>=0.5", True),
        ("gptme-rag [lexical] >= 0.5", True),
        ("gptme-rag==0.6", False),
        ("gptme-rag >=0.5", False),
        ("gptme-rag[nonlexical]>=0.5", False),
        ('gptme-rag>=0.5; platform_release == "lexical"', False),
    ],
)
def test_dependency_guard_parses_requirement_syntax(
    requirement: str,
    has_lexical: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f"[project]\ndependencies = ['{requirement}']\n", encoding="utf-8"
    )
    monkeypatch.setitem(globals(), "_PYPROJECT", pyproject)
    test_voice_declares_its_retrieval_dependency()
    if has_lexical:
        test_retrieval_dependency_requests_the_lexical_extra()
    else:
        with pytest.raises(AssertionError, match="lexical.*extra"):
            test_retrieval_dependency_requests_the_lexical_extra()


def test_missing_retrieval_requirement_has_readable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["click>=8"]\n', encoding="utf-8")
    monkeypatch.setitem(globals(), "_PYPROJECT", pyproject)
    with pytest.raises(AssertionError, match="must declare gptme-rag"):
        test_retrieval_dependency_requests_the_lexical_extra()
