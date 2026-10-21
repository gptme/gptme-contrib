"""Dependency contracts for lightweight hooks and existing dense consumers."""

from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib


METADATA = Path(__file__).parents[1] / "pyproject.toml"


def test_base_install_is_lesson_matcher_only():
    project = tomllib.loads(METADATA.read_text())["project"]
    assert project["dependencies"] == ["regex>=2024.11.6"]


def test_dense_and_existing_extras_keep_their_runtime():
    extras = tomllib.loads(METADATA.read_text())["project"]["optional-dependencies"]
    assert "chromadb>=1.0" in extras["full"]
    assert "sentence-transformers>=3.3.1" in extras["full"]
    assert "tiktoken>=0.7" in extras["full"]
    for consumer in ("mcp", "test", "dev"):
        assert "gptme-rag[full]" in extras[consumer]
    # Document imports DocumentProcessor at module scope, which needs tiktoken.
    assert "tiktoken>=0.7" in extras["lexical"]
    assert "scikit-learn>=1.3" in extras["lexical"]
