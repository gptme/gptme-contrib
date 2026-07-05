"""conftest.py for gptme-codegraph tests.

Skips the entire test suite when tree-sitter is not installed (it is an
optional extra: ``pip install gptme-codegraph[treesitter]``).
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Redirect _CACHE_DIR so tests never write to the real ~/.cache."""
    from gptme_codegraph import commit_map

    monkeypatch.setattr(commit_map, "_CACHE_DIR", tmp_path / "gptme-codegraph-cache")


_TREESITTER_FREE = {"test_search_cache"}


def pytest_collection_modifyitems(config, items):
    try:
        import tree_sitter  # type: ignore[import-untyped]  # noqa: F401
        import tree_sitter_python  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        skip_mark = pytest.mark.skip(
            reason="tree-sitter not installed — run: uv sync --all-extras"
        )
        for item in items:
            if "codegraph" in str(item.path) and item.path.stem not in _TREESITTER_FREE:
                item.add_marker(skip_mark)
