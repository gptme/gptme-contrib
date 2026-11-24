"""Class-method resolution in the CLI and MCP server (gptme-contrib#2374).

Methods are graph keys of the form ``module::Class.method``. Both the bare
method name (``start``) and the qualified ID (``app::Server.start``) must
resolve for ``impact``, ``callees`` and ``deps``, in the CLI and over MCP.
"""

import json
import sys
from pathlib import Path

import pytest
from gptme_codegraph.core import (
    _resolve_graph_key,
    _split_symbol_ref,
    build_index,
    find_symbol,
    lookup_index_entries,
    main,
    parse_file,
)

pytest.importorskip("tree_sitter_python")


@pytest.fixture
def method_project(tmp_path: Path) -> Path:
    """The two-file fixture from the issue: a class method calling across files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "from util import helper, retry_with_backoff\n"
        "\n"
        "class Server:\n"
        "    def start(self):\n"
        "        helper()\n"
        "        return self.listen()\n"
        "\n"
        "    def listen(self):\n"
        "        return retry_with_backoff(3)\n"
    )
    (src / "util.py").write_text(
        "def helper(): return leaf()\n"
        "def leaf(): return 1\n"
        "def retry_with_backoff(n):\n"
        "    for i in range(n): helper()\n"
    )
    return src


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    src: Path,
    *argv: str,
) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-codegraph",
            str(src / "app.py"),
            "--directory",
            str(src),
            *argv,
            "--json",
        ],
    )
    main()
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def test_split_symbol_ref():
    assert _split_symbol_ref("app::Server.start") == ("app", "Server", "start")
    assert _split_symbol_ref("Server.start") == (None, "Server", "start")
    assert _split_symbol_ref("::helper") == ("", None, "helper")
    assert _split_symbol_ref("helper") == (None, None, "helper")


def test_resolve_graph_key_methods():
    graph = {
        "app::Server.start": {"util::helper"},
        "util::helper": set(),
    }
    assert _resolve_graph_key("app::Server.start", graph) == "app::Server.start"
    assert _resolve_graph_key("Server.start", graph) == "app::Server.start"
    # Bare method name falls back to the ``Class.method`` key.
    assert _resolve_graph_key("start", graph) == "app::Server.start"
    # A longer module path than the key has does not match.
    assert _resolve_graph_key("src.app::Server.start", graph) is None
    assert _resolve_graph_key("::helper", graph) == "util::helper"
    assert _resolve_graph_key("other::Server.start", graph) is None
    assert _resolve_graph_key("app::Client.start", graph) is None


def test_resolve_graph_key_prefers_top_level_over_method():
    graph = {"app::Server.start": set(), "util::start": set()}
    assert _resolve_graph_key("start", graph) == "util::start"


def test_resolve_graph_key_module_suffix():
    graph = {"src.app::Server.start": set()}
    assert _resolve_graph_key("app::Server.start", graph) == "src.app::Server.start"


def test_lookup_index_entries_qualified(method_project: Path):
    index = build_index(method_project)
    (entry,) = lookup_index_entries(index, "app::Server.start")
    assert (entry.name, entry.parent_class) == ("start", "Server")
    assert lookup_index_entries(index, "Server.start") == [entry]
    assert lookup_index_entries(index, "start") == [entry]
    assert len(lookup_index_entries(index, "util::helper")) == 1
    assert lookup_index_entries(index, "nope::Server.start") == []
    assert lookup_index_entries(index, "app::Client.start") == []
    # ``module::name`` is a top-level qualified ID; it does not match methods.
    assert lookup_index_entries(index, "app::start") == []


def test_find_symbol_qualified(method_project: Path):
    symbols = parse_file(method_project / "app.py").symbols
    found = find_symbol(symbols, "Server.start")
    assert found is not None and found.parent_class == "Server"
    assert find_symbol(symbols, "app::Server.listen") is not None
    assert find_symbol(symbols, "Client.start") is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["app::Server.start", "Server.start", "start"])
def test_cli_callees_and_deps_on_method(method_project, monkeypatch, capsys, name):
    out = _cli(monkeypatch, capsys, method_project, "callees", name)
    assert out["callees"] == ["util::helper"]
    out = _cli(monkeypatch, capsys, method_project, "deps", name)
    assert out["deps"] == {
        "depth_0": ["app::Server.start"],
        "depth_1": ["util::helper"],
        "depth_2": ["util::leaf"],
    }


@pytest.mark.parametrize("name", ["app::Server.listen", "Server.listen", "listen"])
def test_cli_impact_on_method(method_project, monkeypatch, capsys, name):
    # The README's headline example used to exit with "Symbol not found".
    out = _cli(monkeypatch, capsys, method_project, "impact", name)
    assert "depth_0" in out["impact_radius"]


def test_cli_impact_reaches_method_callers(method_project, monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, method_project, "impact", "util::helper")
    assert out["impact_radius"]["depth_1"] == [
        "app::Server.start",
        "util::retry_with_backoff",
    ]
    assert out["impact_radius"]["depth_2"] == ["app::Server.listen"]


def test_cli_def_qualified_method(method_project, monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, method_project, "def", "app::Server.start")
    assert out["name"] == "start"
    assert out["parent_class"] == "Server"


def test_cli_unknown_qualified_method_not_found(method_project, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="Symbol not found"):
        _cli(monkeypatch, capsys, method_project, "impact", "nope::Server.start")


def test_cli_qualified_callees_are_not_external(method_project, monkeypatch, capsys):
    # Text output resolves qualified graph keys back to their definitions.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-codegraph",
            str(method_project / "app.py"),
            "--directory",
            str(method_project),
            "callees",
            "retry_with_backoff",
        ],
    )
    main()
    out = capsys.readouterr().out
    assert "util::helper()" in out
    assert "(external)" not in out


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp(monkeypatch):
    mod = pytest.importorskip("gptme_codegraph.mcp_server")
    monkeypatch.setattr(mod, "_index_cache", {})
    return mod


@pytest.mark.parametrize("name", ["app::Server.listen", "Server.listen", "listen"])
def test_mcp_impact_on_method_without_callers(mcp, method_project, name):
    out = json.loads(mcp.codegraph_impact(name=name, directory=str(method_project)))
    assert "error" not in out
    assert out["defined_in"] == [str(method_project / "app.py")]


@pytest.mark.parametrize("name", ["app::Server.start", "Server.start", "start"])
def test_mcp_callees_and_blast_on_method(mcp, method_project, name):
    out = json.loads(mcp.codegraph_callees(name=name, directory=str(method_project)))
    assert out["callees"] == ["util::helper"]
    out = json.loads(mcp.codegraph_blast(name=name, directory=str(method_project)))
    assert out["depth_breakdown"]["depth_0"] == ["app::Server.start"]
    assert out["depth_breakdown"]["depth_2"] == ["util::leaf"]


def test_mcp_graph_keys_relative_to_relative_directory(
    mcp, method_project, monkeypatch
):
    # A relative directory must not leak the absolute path into graph keys.
    monkeypatch.chdir(method_project.parent)
    out = json.loads(mcp.codegraph_callers(name="helper", directory="src"))
    assert out["callers"] == ["app::Server.start", "util::retry_with_backoff"]
    assert out["search_directory"] == "src"


def test_mcp_def_qualified_method(mcp, method_project):
    out = json.loads(
        mcp.codegraph_def(name="app::Server.start", directory=str(method_project))
    )
    assert out["found"] is True
    assert out["matches"][0]["parent_class"] == "Server"
    out = json.loads(
        mcp.codegraph_def(name="Server.start", filepath=str(method_project / "app.py"))
    )
    assert out["found"] is True


def test_mcp_unknown_qualified_method(mcp, method_project):
    out = json.loads(
        mcp.codegraph_impact(name="nope::Server.start", directory=str(method_project))
    )
    assert "not found" in out["error"]
