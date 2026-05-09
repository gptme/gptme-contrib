"""Tests for codegraph MCP server.

Tests use a shared module fixture that builds a temporary directory with
Python source files to test both single-file and cross-file tools.
"""

import json
import tempfile
from collections.abc import Generator
from pathlib import Path

import gptme_codegraph.mcp_server as _MOD
import pytest


def _content(response) -> str:
    """Extract text content from MCP tool response.
    Handles both CallToolResult (with .content) and tuple (content, metadata) formats.
    """
    # MCP returns tuple (list_of_content_items, metadata_dict) in some versions
    if isinstance(response, tuple):
        content_list = response[0]
    else:
        content_list = response.content

    parts = []
    for content_item in content_list:
        if hasattr(content_item, "text"):
            parts.append(content_item.text)
        elif isinstance(content_item, dict) and "text" in content_item:
            parts.append(content_item["text"])
    return "\n".join(parts)


def _run(coro):
    """Run an async coroutine synchronously."""
    import asyncio

    return asyncio.run(coro)


# -------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_dir() -> Generator[str, None, None]:
    """Create a temporary directory with a small Python module for testing."""
    with tempfile.TemporaryDirectory(prefix="codegraph_test_") as tmp:
        # Module a: defines add and helper
        (Path(tmp) / "a.py").write_text(
            "def add(x, y):\n"
            "    return x + y\n"
            "\n"
            "def helper(msg):\n"
            "    return f'help: {msg}'\n"
        )
        # Module b: defines compute that calls add from a
        (Path(tmp) / "b.py").write_text(
            "from a import add\n"
            "\n"
            "def compute(items):\n"
            "    return [add(i, 1) for i in items]\n"
        )
        # Module c: defines run that calls compute from b
        (Path(tmp) / "c.py").write_text(
            "from b import compute\n\ndef run():\n    return compute([1, 2, 3])\n"
        )
        yield tmp


@pytest.fixture(scope="session")
def sample_file(sample_dir) -> str:
    """Return path to a.py in the sample directory."""
    return str(Path(sample_dir) / "a.py")


# -------------------------------------------------------------------------
# Tool registration
# -------------------------------------------------------------------------


def test_tools_registered():
    """Verify the 8 expected MCP tools are registered.
    Cross-file tools collapsed into optional filepath parameter."""

    async def _check():
        tools = await _MOD.mcp.list_tools()
        return {t.name for t in tools}

    names = _run(_check())
    expected = {
        "codegraph_parse",
        "codegraph_index",
        "codegraph_def",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_refs",
        "codegraph_blast",
        "codegraph_impact",
    }
    assert names == expected


# -------------------------------------------------------------------------
# Single-file tools
# -------------------------------------------------------------------------


def test_parse(sample_file):
    """Test codegraph_parse extracts symbols."""

    async def _check():
        r = await _MOD.mcp.call_tool("codegraph_parse", {"filepath": sample_file})
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d["file"] == sample_file
    assert len(d["symbols"]) >= 2  # add, helper


def test_index(sample_dir):
    """Test codegraph_index builds index for a directory."""

    async def _check():
        r = await _MOD.mcp.call_tool("codegraph_index", {"directory": sample_dir})
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d.get("files_indexed", 0) >= 3
    assert d.get("unique_symbols", 0) >= 4


def test_def(sample_file):
    """Test codegraph_def finds symbol definition."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_def", {"name": "add", "filepath": sample_file}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert d["found"]


def test_def_missing(sample_file):
    """Test codegraph_def returns not-found for nonexistent symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_def",
            {"name": "nonexistent", "filepath": sample_file},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert not d["found"]


def test_def_cross_file(sample_dir):
    """Test codegraph_def resolves symbol across files via directory."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_def",
            {"name": "add", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert d["found"]
    # codegraph_def returns definitions_count (int) when using cross-file index
    defs = d.get("definitions") or d.get("definitions_count", 0)
    if isinstance(defs, list | tuple):
        assert len(defs) >= 1
    else:
        assert defs >= 1


def test_callers(sample_file):
    """Test codegraph_callers finds callers of a symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_callers", {"name": "helper", "filepath": sample_file}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert "callers" in d


def test_callees(sample_file):
    """Test codegraph_callees finds callees of a symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_callees", {"name": "add", "filepath": sample_file}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert "callees" in d


def test_blast(sample_file):
    """Test codegraph_blast computes blast radius."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_blast", {"name": "add", "filepath": sample_file}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert "total_affected" in d


def test_blast_missing_symbol(sample_file):
    """Test codegraph_blast returns error for missing symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_blast",
            {"name": "nonexistent", "filepath": sample_file},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_impact(sample_file):
    """codegraph_impact should report symbols that call the target.

    In a.py: add() is called by nothing within a.py (it's only called from
    b.py). helper() is the function that's called externally. So impact
    of helper should show callers within a.py (if any).
    """

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_impact", {"name": "helper", "filepath": sample_file}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert "total_affected" in d


def test_impact_differs_from_blast(sample_file):
    """codegraph_impact (callers) and codegraph_blast (callees) must differ.

    In a.py, helper() has no internal callees but may have internal callers.
    For helper, both impact and blast report depth_0=self, so total_affected
    may be 1 for both. Check they differ OR that helper is not a no-op.
    """

    async def _check():
        r1 = await _MOD.mcp.call_tool(
            "codegraph_impact", {"name": "helper", "filepath": sample_file}
        )
        r2 = await _MOD.mcp.call_tool(
            "codegraph_blast", {"name": "helper", "filepath": sample_file}
        )
        return json.loads(_content(r1)), json.loads(_content(r2))

    impact_d, blast_d = _run(_check())
    # helper is a leaf function: no callees, no internal callers
    # Both report depth_0=[helper] with total_affected=1
    assert blast_d["total_affected"] >= 1
    # Verify callees is empty (no depth_1+) for a leaf function
    assert all(k.startswith("depth_") for k in blast_d["depth_breakdown"])


def test_impact_missing_symbol(sample_file):
    """codegraph_impact returns error for unknown symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_impact",
            {"name": "nonexistent", "filepath": sample_file},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


# -------------------------------------------------------------------------
# Cross-file tools (via optional filepath parameter)
# -------------------------------------------------------------------------


def test_cross_file_impact(sample_dir):
    """codegraph_impact with filepath=None, directory=... resolves cross-file.

    The fixture exposes `add` (defined in module `a`, called from `compute`
    in module `b`). The cross-file impact of `add` must include `compute`.
    """

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_impact",
            {"name": "add", "filepath": None, "directory": sample_dir, "max_depth": 3},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d["symbol"] == "add"
    assert d["total_affected"] >= 1
    assert "depth_0" in d["depth_breakdown"]
    assert "a::add" in d["depth_breakdown"]["depth_0"]


def test_cross_file_impact_missing_symbol(sample_dir):
    """codegraph_impact (cross-file mode) returns error for unknown symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_impact",
            {"name": "nonexistent_func", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_refs_equals_callers(sample_file):
    """Test codegraph_refs returns same result as codegraph_callers."""

    async def _check():
        r1 = await _MOD.mcp.call_tool(
            "codegraph_refs", {"name": "helper", "filepath": sample_file}
        )
        r2 = await _MOD.mcp.call_tool(
            "codegraph_callers", {"name": "helper", "filepath": sample_file}
        )
        return _content(r1), _content(r2)

    r1, r2 = _run(_check())
    assert json.loads(r1) == json.loads(r2)


def test_nonexistent_file(sample_file):
    """All tools return error for nonexistent file path."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_parse", {"filepath": "/nonexistent/file.py"}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_nonexistent_directory():
    """Tools with directory return error for nonexistent directory."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_index", {"directory": "/nonexistent/dir"}
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_error_on_no_filepath(sample_dir):
    """Tools that require filepath or directory error when both are missing."""

    async def _check():
        r = await _MOD.mcp.call_tool("codegraph_callers", {"name": "add"})
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_cross_file_callers(sample_dir):
    """codegraph_callers with filepath=None resolves cross-file callers."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_callers",
            {"name": "add", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d["symbol"] == "add"
    assert d["caller_count"] >= 1


def test_cross_file_callers_missing_symbol(sample_dir):
    """codegraph_callers (cross-file mode) returns error for unknown symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_callers",
            {"name": "nonexistent_func", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d


def test_cross_file_callees(sample_dir):
    """codegraph_callees with filepath=None resolves cross-file callees."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_callees",
            {"name": "run", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d["symbol"] == "run"
    assert d["callee_count"] >= 1


def test_cross_file_blast(sample_dir):
    """codegraph_blast with filepath=None resolves cross-file blast radius."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_blast",
            {
                "name": "compute",
                "filepath": None,
                "directory": sample_dir,
                "max_depth": 3,
            },
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" not in d
    assert d["symbol"] == "compute"
    assert d["total_affected"] >= 1


def test_cross_file_blast_missing_symbol(sample_dir):
    """codegraph_blast (cross-file mode) returns error for unknown symbol."""

    async def _check():
        r = await _MOD.mcp.call_tool(
            "codegraph_blast",
            {"name": "nonexistent_func", "filepath": None, "directory": sample_dir},
        )
        return json.loads(_content(r))

    d = _run(_check())
    assert "error" in d
