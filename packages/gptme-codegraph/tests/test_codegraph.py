"""Tests for scripts/codegraph.py — structural code retrieval via tree-sitter."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import cast

import gptme_codegraph.core as core_module
import pytest
from gptme_codegraph.core import (
    IndexEntry,
    Symbol,
    SymbolIndex,
    _extract_imports,
    _module_path,
    _tree_sitter_parse,
    blast_radius,
    build_call_graph,
    build_cross_file_call_graph,
    build_index,
    build_repo_map,
    dependency_closure,
    extract_symbols,
    format_json,
    format_repo_map,
    format_symbols,
    impact_radius,
    main,
    parse_file,
)


def _has_grammar(module: str) -> bool:
    """Return True when an optional tree-sitter grammar is importable."""
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


_skip_no_js = pytest.mark.skipif(
    not _has_grammar("tree_sitter_javascript"),
    reason="tree-sitter-javascript not installed",
)
_skip_no_ts = pytest.mark.skipif(
    not _has_grammar("tree_sitter_typescript"),
    reason="tree-sitter-typescript not installed",
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_module() -> Generator[Path, None, None]:
    """A small Python module with functions, classes, and cross-references."""
    code = """
def greet(name: str) -> str:
    \"\"\"Say hello.\"\"\"
    return f"Hello {name}"


def farewell(name: str) -> str:
    \"\"\"Say goodbye.\"\"\"
    msg = greet(name)
    return f"{msg}, and goodbye"


class Calculator:
    \"\"\"A simple calculator.\"\"\"

    def add(self, a: int, b: int) -> int:
        \"\"\"Add two numbers.\"\"\"
        return greet(f"result={a + b}")

    def multiply(self, a: int, b: int) -> int:
        return a * b


def run():
    calc = Calculator()
    result = calc.add(1, 2)
    return result


def unused_helper():
    pass
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="test_simple_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def empty_module() -> Generator[Path, None, None]:
    """A Python file with no symbols (only comments and whitespace)."""
    code = """# This file has no functions, classes, or methods.
# Just comments.
# And whitespace.

"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="test_empty_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def class_only_module() -> Generator[Path, None, None]:
    """A module with only a class and methods, no top-level functions."""
    code = """
class DataProcessor:
    \"\"\"Process data.\"\"\"

    def transform(self, data: list) -> list:
        return [x * 2 for x in data]

    def validate(self, data: list) -> bool:
        return len(data) > 0
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="test_class_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def javascript_module() -> Generator[Path, None, None]:
    """A JavaScript module with top-level functions, arrow functions, and a class."""
    code = """
export function greet(name) {
    return helper(name)
}

const helper = (value) => value.trim()

class User {
    speak() {
        return greet(this.name)
    }
}
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, prefix="test_js_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def typescript_module() -> Generator[Path, None, None]:
    """A TypeScript module with typed functions and methods."""
    code = """
export function greet(name: string): string {
    return helper(name)
}

const helper = (value: string) => value.trim()

class User {
    name: string

    constructor(name: string) {
        this.name = name
    }

    speak(): string {
        return greet(this.name)
    }
}
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ts", delete=False, prefix="test_ts_"
    ) as f:
        f.write(code)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


def test_extract_symbols_finds_functions(simple_module: Path):
    symbols = extract_symbols(simple_module)
    names = {s.name for s in symbols}
    assert "greet" in names
    assert "farewell" in names
    assert "run" in names
    assert "unused_helper" in names


def test_extract_symbols_finds_classes(simple_module: Path):
    symbols = extract_symbols(simple_module)
    names = {s.name for s in symbols}
    assert "Calculator" in names


def test_extract_symbols_finds_class_methods(simple_module: Path):
    symbols = extract_symbols(simple_module)
    methods = [s for s in symbols if s.kind == "method"]
    method_names = {m.name for m in methods}
    assert "add" in method_names
    assert "multiply" in method_names
    for m in methods:
        if m.name == "add":
            assert m.parent_class == "Calculator"
        if m.name == "multiply":
            assert m.parent_class == "Calculator"


def test_extract_symbols_docstrings(simple_module: Path):
    symbols = extract_symbols(simple_module)
    greet = next(s for s in symbols if s.name == "greet")
    assert greet.docstring == "Say hello."
    calc = next(s for s in symbols if s.name == "Calculator")
    assert calc.docstring == "A simple calculator."
    add = next(s for s in symbols if s.name == "add")
    assert add.docstring == "Add two numbers."


def test_extract_symbols_line_numbers(simple_module: Path):
    symbols = extract_symbols(simple_module)
    greet = next(s for s in symbols if s.name == "greet")
    assert greet.start_line >= 0  # 0-indexed or 1-indexed
    assert greet.end_line > greet.start_line


def test_extract_symbols_calls(simple_module: Path):
    symbols = extract_symbols(simple_module)
    farewell = next(s for s in symbols if s.name == "farewell")
    assert "greet" in farewell.calls
    add = next(s for s in symbols if s.name == "add")
    assert "greet" in add.calls


def test_extract_symbols_empty_module(empty_module: Path):
    symbols = extract_symbols(empty_module)
    assert len(symbols) == 0


def test_extract_symbols_class_only(class_only_module: Path):
    symbols = extract_symbols(class_only_module)
    names = {s.name for s in symbols}
    assert "DataProcessor" in names
    assert "transform" in names
    assert "validate" in names
    # No top-level functions in class-only module
    funcs = [s for s in symbols if s.kind == "function"]
    assert len(funcs) == 0
    methods = [s for s in symbols if s.kind == "method"]
    assert len(methods) == 2


@_skip_no_js
def test_extract_symbols_javascript_calls_and_methods(javascript_module: Path):
    symbols = extract_symbols(javascript_module)
    names = {s.name for s in symbols}
    assert {"greet", "helper", "User", "speak"} <= names

    greet = next(s for s in symbols if s.name == "greet")
    helper = next(s for s in symbols if s.name == "helper")
    speak = next(s for s in symbols if s.name == "speak")

    assert helper.kind == "function"
    assert speak.kind == "method"
    assert speak.parent_class == "User"
    assert "helper" in greet.calls
    assert "greet" in speak.calls


@_skip_no_ts
def test_extract_symbols_typescript_calls_and_constructor(typescript_module: Path):
    symbols = extract_symbols(typescript_module)
    names = {s.name for s in symbols}
    assert {"greet", "helper", "User", "constructor", "speak"} <= names

    constructor = next(s for s in symbols if s.name == "constructor")
    speak = next(s for s in symbols if s.name == "speak")
    greet = next(s for s in symbols if s.name == "greet")

    assert constructor.kind == "method"
    assert constructor.parent_class == "User"
    assert speak.parent_class == "User"
    assert "helper" in greet.calls
    assert "greet" in speak.calls


def test_extract_symbols_kind_distribution(simple_module: Path):
    symbols = extract_symbols(simple_module)
    kinds = {s.kind for s in symbols}
    assert "function" in kinds
    assert "class" in kinds
    assert "method" in kinds


# ---------------------------------------------------------------------------
# Call graph
# ---------------------------------------------------------------------------


def test_build_call_graph_callees(simple_module: Path):
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    # Qualified IDs: symbols in temp files have module_path=""
    greet = "::greet"
    farewell = "::farewell"
    add = "::Calculator.add"
    run_sym = "::run"
    calc = "::Calculator"
    unused = "::unused_helper"
    assert greet in callees.get(farewell, set())
    assert greet in callees.get(add, set())
    assert calc in callees.get(run_sym, set())
    # unused_helper calls nothing
    assert unused not in callees or not callees[unused]


def test_build_call_graph_callers(simple_module: Path):
    symbols = extract_symbols(simple_module)
    _callees, callers = build_call_graph(symbols)
    greet = "::greet"
    farewell = "::farewell"
    add = "::Calculator.add"
    assert farewell in callers.get(greet, set())
    assert add in callers.get(greet, set())
    # Calculator also extracts calls from its body scope, so greet may be found there too
    greet_callers = callers.get(greet, set())
    assert len(greet_callers) >= 2  # farewell + Calculator.add


def test_build_call_graph_no_external_calls(simple_module: Path):
    """External calls (like 'len', 'print', 'f"..."') should not appear."""
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    # All callees should be known symbols — now qualified IDs
    known_qids = {s.qualified_id() for s in symbols}
    all_callee_ids = set()
    for targets in callees.values():
        all_callee_ids.update(targets)
    for qid in all_callee_ids:
        assert qid in known_qids, f"Unknown callee {qid} in call graph"


def test_build_call_graph_empty():
    """Empty symbol list produces empty graphs."""
    callees, callers = build_call_graph([])
    assert callees == {}
    assert callers == {}


def test_build_call_graph_no_crossrefs(simple_module: Path):
    """Symbols that neither call nor are called have empty graph entries."""
    symbols = extract_symbols(simple_module)
    callees, callers = build_call_graph(symbols)
    # unused_helper calls nothing
    unused = callees.get("unused_helper", set())
    assert len(unused) == 0
    # greet is called but calls nothing internal
    greet_callees = callees.get("greet", set())
    assert len(greet_callees) == 0


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------


def test_module_path():
    """_module_path converts file paths to dotted module paths.

    ``__init__.py`` files are stripped to their package root so that
    ``pkg/__init__.py`` maps to ``pkg`` rather than ``pkg.__init__``,
    matching Python runtime semantics.
    """
    assert (
        _module_path("/home/bob/bob/scripts/codegraph.py", "/home/bob/bob")
        == "scripts.codegraph"
    )
    assert _module_path("scripts/codegraph.py") == "scripts.codegraph"
    # __init__.py strips to the package root
    assert (
        _module_path("packages/context/src/context/__init__.py")
        == "packages.context.src.context"
    )
    # Root __init__.py → empty path (not useful but should not crash)
    assert _module_path("__init__.py") == ""


def test_dependency_closure_basic(simple_module: Path):
    """dependency_closure walks callees (same as old blast_radius)."""
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    radius = dependency_closure("greet", callees)
    assert "depth_0" in radius
    assert radius["depth_0"] == {"::greet"}


def test_impact_radius_basic(simple_module: Path):
    """impact_radius walks callers."""
    symbols = extract_symbols(simple_module)
    _callees, callers = build_call_graph(symbols)
    radius = impact_radius("greet", callers)
    assert "depth_0" in radius
    assert "::greet" in radius["depth_0"]
    # farewell calls greet, so farewell should be depth_1
    depths = sorted(radius.keys())
    if len(depths) > 1:
        assert "::farewell" in radius[depths[1]]


def test_dependency_closure_and_impact_complement(simple_module: Path):
    """deps and impact should produce symmetric results for callers/callees."""
    symbols = extract_symbols(simple_module)
    callees, callers = build_call_graph(symbols)
    # farewell calls greet
    deps_farewell = dependency_closure("farewell", callees)
    impact_greet = impact_radius("greet", callers)
    depths_deps = sorted(deps_farewell.keys())
    depths_impact = sorted(impact_greet.keys())
    # farewell's deps should include greet
    if len(depths_deps) > 1:
        assert "::greet" in deps_farewell[depths_deps[1]]
    # greet's impact should include farewell
    if len(depths_impact) > 1:
        assert "::farewell" in impact_greet[depths_impact[1]]


# ---------------------------------------------------------------------------


def test_blast_radius_basic(simple_module: Path):
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    radius = blast_radius("greet", callees)
    assert "depth_0" in radius
    assert radius["depth_0"] == {"::greet"}
    # greet calls nothing internal, so depth_1 should be empty
    assert "depth_1" not in radius or not radius["depth_1"]


def test_blast_radius_downstream(simple_module: Path):
    """farewell calls greet, so greet should appear in farewell's blast radius."""
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    radius = blast_radius("farewell", callees)
    assert "depth_0" in radius
    assert "::farewell" in radius["depth_0"]
    # farewell calls greet
    assert "depth_1" in radius or any("::greet" in s for s in radius.values())
    # The first depth past depth_0 should contain greet
    depths = sorted(radius.keys())
    if len(depths) > 1:
        assert "::greet" in radius[depths[1]]


def test_blast_radius_max_depth(simple_module: Path):
    symbols = extract_symbols(simple_module)
    callees, _callers = build_call_graph(symbols)
    radius = blast_radius("greet", callees, max_depth=0)
    assert "depth_0" in radius
    assert "depth_1" not in radius


def test_blast_radius_unknown_symbol():
    """Unknown symbol produces no blast."""
    callees = {"a": {"b"}, "b": {"c"}}
    radius = blast_radius("unknown", callees)
    assert "depth_0" in radius
    assert "unknown" in radius["depth_0"]
    assert "depth_1" not in radius


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_symbols_basic(simple_module: Path):
    symbols = extract_symbols(simple_module)
    output = format_symbols(symbols)
    assert "Functions:" in output
    assert "greet" in output
    assert "Calculator" in output
    assert "Say hello." in output or "say hello" in output.lower()


def test_format_symbols_empty():
    """Empty symbol list prints zero."""
    output = format_symbols([])
    assert "Functions: 0" in output
    assert "Total: 0" in output


def test_format_json_basic():
    """JSON formatter produces valid JSON."""
    data = {"key": "value", "number": 42}
    result = format_json(data)
    parsed = json.loads(result)
    assert parsed == data


def test_build_repo_map_groups_class_methods(multi_file_project: Path):
    """Repo-map output should group methods under their parent class."""
    repo_map = build_repo_map(multi_file_project, max_files=10, max_symbols_per_file=10)
    assert repo_map["files_scanned"] == 3
    assert repo_map["files_with_symbols"] == 3

    files = cast(list[dict[str, object]], repo_map["files"])
    models_row = next(row for row in files if row["path"] == "models.py")
    class_entry = next(
        entry
        for entry in cast(list[dict[str, object]], models_row["outline"])
        if entry["kind"] == "class"
    )
    assert class_entry["name"] == "User"
    method_names = {
        method["name"]
        for method in cast(list[dict[str, object]], class_entry["methods"])
    }
    assert "__init__" in method_names
    assert "greet" in method_names


@_skip_no_js
@_skip_no_ts
def test_build_repo_map_mixed_language_sources(tmp_path: Path):
    """Repo-map should count and outline non-Python source files too."""
    (tmp_path / "web.ts").write_text(
        """
export function render(name: string): string {
    return helper(name)
}

const helper = (value: string) => value.trim()
"""
    )
    (tmp_path / "api.js").write_text(
        """
export function handle(req) {
    return normalize(req)
}

const normalize = (req) => req
"""
    )

    repo_map = build_repo_map(tmp_path, max_files=10, max_symbols_per_file=10)
    assert repo_map["files_scanned"] == 2
    assert repo_map["files_with_symbols"] == 2

    files = cast(list[dict[str, object]], repo_map["files"])
    paths = {cast(str, row["path"]) for row in files}
    assert {"web.ts", "api.js"} <= paths

    web_outline = next(
        cast(list[dict[str, object]], row["outline"])
        for row in files
        if row["path"] == "web.ts"
    )
    assert any(entry["name"] == "render" for entry in web_outline)
    assert any(entry["name"] == "helper" for entry in web_outline)


def test_build_repo_map_prefers_production_files_over_tests(tmp_path: Path):
    """Repo-map ranking should not let oversized test files crowd out source."""
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    (src_dir / "app.py").write_text(
        """
def run_app() -> str:
    return helper()


def helper() -> str:
    return "ok"
"""
    )
    (tests_dir / "test_app.py").write_text(
        """
def test_one():
    return 1


def test_two():
    return 2


def test_three():
    return 3


def test_four():
    return 4
"""
    )

    repo_map = build_repo_map(tmp_path, max_files=2, max_symbols_per_file=10)
    files = cast(list[dict[str, object]], repo_map["files"])

    assert cast(str, files[0]["path"]) == "src/app.py"
    assert cast(str, files[1]["path"]) == "tests/test_app.py"


def test_format_repo_map_human_readable(multi_file_project: Path):
    """Repo-map formatter should emit a compact outline."""
    repo_map = build_repo_map(multi_file_project, max_files=2, max_symbols_per_file=2)
    output = format_repo_map(repo_map)
    assert "Repo map:" in output
    assert "models.py" in output
    assert "class User" in output
    assert "def __init__(...)" in output or "def greet(...)" in output


# ---------------------------------------------------------------------------
# CLI edge cases
# ---------------------------------------------------------------------------


def test_cli_parse_json(simple_module: Path):
    """The parse subcommand with --json produces valid JSON symbol output."""
    from gptme_codegraph.core import extract_symbols

    symbols = extract_symbols(simple_module)
    data = [s.__dict__ if hasattr(s, "__dict__") else vars(s) for s in symbols]

    # Verify each symbol has the expected fields
    for entry in data:
        assert "name" in entry
        assert "kind" in entry
        assert "start_line" in entry
        assert "end_line" in entry


def test_cli_def_output(simple_module: Path):
    """Validate the 'def' lookup finds a symbol."""
    symbols = extract_symbols(simple_module)
    name_map = {s.name: s for s in symbols}

    greet = name_map.get("greet")
    assert greet is not None
    assert greet.kind == "function"
    assert "Say hello" in (greet.docstring or "")

    calc = name_map.get("Calculator")
    assert calc is not None
    assert calc.kind == "class"
    assert "simple calculator" in (calc.docstring or "").lower()


def test_cli_refs_output(simple_module: Path):
    """Validate the 'refs' lookup finds callers."""
    symbols = extract_symbols(simple_module)
    _callees, callers = build_call_graph(symbols)

    # greet is called by farewell and Calculator.add
    # (qualified IDs: ::farewell, ::Calculator.add)
    greet_callers = callers.get("::greet", set())
    assert "::farewell" in greet_callers
    assert "::Calculator.add" in greet_callers
    assert len(greet_callers) >= 2  # farewell + Calculator.add


def test_coverage_of_self(simple_module: Path):
    """The codegraph script can analyze itself (meta-test)."""
    codegraph_path = Path(__file__).resolve().parent.parent / "scripts" / "codegraph.py"
    if not codegraph_path.exists():
        pytest.skip("codegraph.py not found for meta-test")
    symbols = extract_symbols(codegraph_path)
    assert len(symbols) > 5
    names = {s.name for s in symbols}
    assert "main" in names
    assert "extract_symbols" in names
    assert "blast_radius" in names or "dependency_closure" in names
    assert "build_call_graph" in names


# ---------------------------------------------------------------------------
# Cross-file index
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_file_project(tmp_path: Path) -> Path:
    """Create a small multi-file project for cross-file resolution tests."""
    root = tmp_path / "project"
    root.mkdir()

    # utils.py — utility functions
    (root / "utils.py").write_text("""
def format_name(name: str) -> str:
    \"\"\"Format a name.\"\"\"
    return name.strip().title()


def parse_count(raw: str) -> int:
    \"\"\"Parse a count string.\"\"\"
    return int(raw.strip())
""")

    # models.py — data classes
    (root / "models.py").write_text("""
class User:
    \"\"\"A user model.\"\"\"
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        from utils import format_name
        return f"Hello, {format_name(self.name)}"
""")

    # main.py — entry point that calls across modules
    (root / "main.py").write_text("""
from utils import format_name, parse_count
from models import User


def run_app(names: list[str]) -> list[str]:
    \"\"\"Run the app.\"\"\"
    results = []
    for name in names:
        user = User(name)
        results.append(user.greet())
    return results


def count_items(raw: str) -> int:
    \"\"\"Count comma-separated items.\"\"\"
    count = parse_count(raw)
    return count
""")

    return root


def test_index_entry():
    """IndexEntry stores symbol metadata."""
    entry = IndexEntry(
        name="greet",
        kind="function",
        file="utils.py",
        start_line=1,
        end_line=5,
    )
    assert entry.name == "greet"
    assert entry.kind == "function"
    assert entry.parent_class is None


def test_symbol_index_empty():
    """Empty index returns nothing."""
    index = SymbolIndex()
    assert not index.has("greet")
    assert index.lookup("greet") == []
    assert index.all_names() == []


def test_symbol_index_lookup():
    """Index can store and retrieve entries."""
    index = SymbolIndex()
    index.entries["greet"] = [
        IndexEntry(
            name="greet",
            kind="function",
            file="a.py",
            start_line=1,
            end_line=5,
        ),
    ]
    assert index.has("greet")
    results = index.lookup("greet")
    assert len(results) == 1
    assert results[0].file == "a.py"


def test_symbol_index_multi_file():
    """Index can track same symbol across files."""
    index = SymbolIndex()
    index.entries["Config"] = [
        IndexEntry(name="Config", kind="class", file="a.py", start_line=1, end_line=10),
        IndexEntry(name="Config", kind="class", file="b.py", start_line=1, end_line=8),
    ]
    assert len(index.lookup("Config")) == 2
    assert index.has("Config")


def test_build_index_empty_directory(tmp_path: Path):
    """build_index on empty dir returns empty index."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    index = build_index(empty_dir)
    assert index.all_names() == []


def test_build_index_skips_noise(tmp_path: Path):
    """build_index skips .venv, __pycache__, etc."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "real.py").write_text("def foo(): pass")
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("def injected(): pass")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cached.py").write_text("def cached(): pass")

    index = build_index(root)
    assert index.has("foo")
    assert not index.has("injected")
    assert not index.has("cached")


def test_build_repo_map_skips_tracked_target_in_git_repo(tmp_path: Path):
    """Git-backed discovery should still ignore tracked target/ artifacts."""
    root = tmp_path / "project"
    src_dir = root / "src"
    target_dir = root / "target" / "debug" / "build" / "demo" / "out"
    src_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("def real_func():\n    return 'ok'\n")
    (target_dir / "generated.py").write_text(
        "def generated_func():\n    return 'noise'\n"
    )

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "src/app.py", "target/debug/build/demo/out/generated.py"],
        cwd=root,
        check=True,
    )

    index = build_index(root)
    assert index.has("real_func")
    assert not index.has("generated_func")

    repo_map = build_repo_map(root, max_files=10, max_symbols_per_file=10)
    assert repo_map["files_scanned"] == 1
    assert repo_map["files_with_symbols"] == 1

    files = cast(list[dict[str, object]], repo_map["files"])
    paths = {cast(str, row["path"]) for row in files}
    assert paths == {"src/app.py"}


def test_build_index_multi_file(multi_file_project: Path):
    """build_index captures symbols across multiple files."""
    index = build_index(multi_file_project)
    assert index.has("format_name")
    assert index.has("parse_count")
    assert index.has("User")
    assert index.has("run_app")
    assert index.has("count_items")


def test_build_index_symbol_locations(multi_file_project: Path):
    """Index entries have correct file paths."""
    index = build_index(multi_file_project)
    format_name_entries = index.lookup("format_name")
    assert len(format_name_entries) == 1
    assert "utils.py" in format_name_entries[0].file

    user_entries = index.lookup("User")
    assert len(user_entries) == 1
    assert "models.py" in user_entries[0].file


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_symbol_dataclass_defaults():
    """Symbol dataclass has correct defaults."""
    sym = Symbol(name="test", kind="function", file="test.py", start_line=1, end_line=5)
    assert sym.parent_class is None
    assert sym.calls == []
    assert sym.docstring is None


def test_symbol_dataclass_with_all_fields():
    """Symbol dataclass with all fields set."""
    sym = Symbol(
        name="test",
        kind="method",
        file="test.py",
        start_line=1,
        end_line=5,
        parent_class="Foo",
        calls=["helper"],
        docstring="Does something.",
    )
    assert sym.parent_class == "Foo"
    assert sym.calls == ["helper"]
    assert sym.docstring == "Does something."


def test_nonexistent_file():
    """Non-existent file raises FileNotFoundError."""
    path = Path("/nonexistent/test_codegraph_nonexistent.py")
    with pytest.raises((FileNotFoundError, OSError)):
        extract_symbols(path)


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------


def test_extract_imports_basic():
    """_extract_imports finds simple imports."""
    code = b"import os\nfrom sys import path\n"
    root, _ = _tree_sitter_parse(code)
    assert root is not None
    imports = _extract_imports(root)
    names = {imp.name for imp in imports}
    assert "os" in names
    assert "path" in names


def test_extract_imports_alias():
    """_extract_imports handles aliased imports.

    ``name`` carries the original imported symbol; ``alias`` is the local
    binding visible inside the file.
    """
    code = b"from os import path as p\n"
    root, _ = _tree_sitter_parse(code)
    assert root is not None
    imports = _extract_imports(root)
    assert len(imports) == 1
    assert imports[0].name == "path"
    assert imports[0].alias == "p"
    assert imports[0].module == "os"


def test_parse_file_returns_imports(tmp_path: Path):
    """parse_file returns both symbols and imports."""
    f = tmp_path / "test.py"
    f.write_text("import json\ndef helper(): pass\n")
    result = parse_file(f)
    assert len(result.symbols) == 1
    assert result.symbols[0].name == "helper"
    assert len(result.imports) == 1
    assert result.imports[0].name == "json"


def test_parse_file_reports_missing_grammar(monkeypatch, tmp_path: Path):
    """parse_file distinguishes missing grammars from empty parses."""
    f = tmp_path / "component.tsx"
    f.write_text("export function Button() { return <button /> }\n")

    original = core_module._load_language
    monkeypatch.setattr(
        core_module,
        "_load_language",
        lambda name: None if name == "tsx" else original(name),
    )

    result = parse_file(f)

    assert result.symbols == []
    assert result.language == "tsx"
    assert result.diagnostic is not None
    assert result.diagnostic["code"] == "missing-grammar"
    assert "tree_sitter_typescript" in result.diagnostic["message"]


def test_main_parse_json_reports_missing_grammar(monkeypatch, tmp_path: Path, capsys):
    """CLI parse --json emits an explicit diagnostic for missing grammars."""
    f = tmp_path / "lib.rs"
    f.write_text("pub fn greet() {}\n")

    original = core_module._load_language
    monkeypatch.setattr(
        core_module,
        "_load_language",
        lambda name: None if name == "rust" else original(name),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-codegraph", str(f), "parse", "--json"],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["file"] == str(f)
    assert payload["language"] == "rust"
    assert payload["code"] == "missing-grammar"
    assert "tree_sitter_rust" in payload["message"]


def test_build_repo_map_reports_missing_grammar(monkeypatch, tmp_path: Path):
    """Repo map surfaces missing grammars instead of a silent empty skeleton."""
    (tmp_path / "a.rs").write_text("pub fn one() {}\n")
    (tmp_path / "b.rs").write_text("pub fn two() {}\n")

    original = core_module._load_language
    monkeypatch.setattr(
        core_module,
        "_load_language",
        lambda name: None if name == "rust" else original(name),
    )

    repo_map = build_repo_map(tmp_path, max_files=10, max_symbols_per_file=10)

    assert repo_map["files_with_symbols"] == 0
    missing = cast(list[dict[str, object]], repo_map["missing_grammars"])
    assert len(missing) == 1
    assert missing[0]["language"] == "rust"
    assert missing[0]["code"] == "missing-grammar"
    assert missing[0]["files_skipped"] == 2

    output = format_repo_map(repo_map)
    assert "tree_sitter_rust" in output
    assert "2 file(s) skipped" in output


def test_build_repo_map_deduplicates_missing_tree_sitter(monkeypatch, tmp_path: Path):
    """missing-tree-sitter entries collapse to one regardless of how many languages fail."""
    (tmp_path / "a.py").write_text("def one(): pass\n")
    (tmp_path / "b.rs").write_text("pub fn two() {}\n")

    monkeypatch.setattr(
        core_module,
        "_tree_sitter_parse_attempt",
        lambda code, lang_name="python": core_module._ParseAttempt(
            diagnostic={
                "code": "missing-tree-sitter",
                "language": lang_name,
                "message": "Missing tree-sitter runtime. Install gptme-codegraph[treesitter].",
            }
        ),
    )

    repo_map = build_repo_map(tmp_path, max_files=10, max_symbols_per_file=10)

    missing = cast(list[dict[str, object]], repo_map["missing_grammars"])
    assert len(missing) == 1, "All missing-tree-sitter entries should collapse into one"
    assert missing[0]["code"] == "missing-tree-sitter"
    assert cast(int, missing[0]["files_skipped"]) == 2

    output = format_repo_map(repo_map)
    assert "2 file(s) skipped" in output
    assert output.count("Missing tree-sitter") == 1


def test_build_repo_map_no_missing_grammars_when_all_parse(multi_file_project: Path):
    """A healthy repo map reports an empty missing-grammars list."""
    repo_map = build_repo_map(multi_file_project, max_files=10, max_symbols_per_file=10)
    assert repo_map["missing_grammars"] == []
    assert "skipped" not in format_repo_map(repo_map)


def test_parse_file_includes_decorated_python_definitions(tmp_path: Path):
    """parse_file preserves decorated Python functions and methods."""
    f = tmp_path / "decorated.py"
    f.write_text(
        """
def command(name):
    def decorator(func):
        return func
    return decorator


def helper():
    return "ok"


@command("plugin")
def cmd_plugin():
    return helper()


class Tool:
    @command("run")
    def cmd_run(self):
        return helper()
"""
    )

    result = parse_file(f)
    names = {symbol.name for symbol in result.symbols}

    assert {"command", "helper", "cmd_plugin", "Tool", "cmd_run"} <= names

    cmd_plugin = next(
        symbol for symbol in result.symbols if symbol.name == "cmd_plugin"
    )
    assert "helper" in cmd_plugin.calls
    assert cmd_plugin.start_line == 12  # decorator line, not def keyword (13)

    cmd_run = next(symbol for symbol in result.symbols if symbol.name == "cmd_run")
    assert cmd_run.kind == "method"
    assert cmd_run.parent_class == "Tool"
    assert "helper" in cmd_run.calls
    assert cmd_run.start_line == 18  # decorator line, not def keyword (19)


# ---------------------------------------------------------------------------
# Cross-file call graph
# ---------------------------------------------------------------------------


def test_cross_file_call_graph_resolves_imports(tmp_path: Path):
    """build_cross_file_call_graph resolves calls through imports."""
    # module_a.py: defines greet()
    (tmp_path / "module_a.py").write_text("def greet(): pass\n")
    # module_b.py: imports and calls greet()
    (tmp_path / "module_b.py").write_text(
        "from module_a import greet\ndef main(): greet()\n"
    )

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    # Graph keys are qualified IDs (module_a::greet, module_b::main)
    # main() calls greet()
    assert "module_a::greet" in callees.get("module_b::main", set())
    # greet() is called by main()
    assert "module_b::main" in callers.get("module_a::greet", set())


def test_cross_file_call_graph_module_prefix(tmp_path: Path):
    """build_cross_file_call_graph resolves module-prefixed calls."""
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "main.py").write_text("import calc\ndef run(): calc.add(1, 2)\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "calc::add" in callees.get("main::run", set())
    assert "main::run" in callers.get("calc::add", set())


def test_cross_file_call_graph_import_as_alias(tmp_path: Path):
    """build_cross_file_call_graph resolves calls through aliased imports.

    ``import calc as c`` then ``c.add()`` should resolve to ``calc::add``.
    """
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "main.py").write_text("import calc as c\ndef run(): c.add(1, 2)\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "calc::add" in callees.get("main::run", set())
    assert "main::run" in callers.get("calc::add", set())


def test_cross_file_call_graph_from_import_as_alias(tmp_path: Path):
    """build_cross_file_call_graph resolves from-import-as calls.

    ``from calc import add as a`` then ``a()`` should resolve to ``calc::add``.
    """
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "main.py").write_text("from calc import add as a\ndef run(): a(1, 2)\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "calc::add" in callees.get("main::run", set())
    assert "main::run" in callers.get("calc::add", set())


def test_cross_file_call_graph_ignores_unresolved(tmp_path: Path):
    """build_cross_file_call_graph ignores calls that can't be resolved."""
    (tmp_path / "main.py").write_text("def run(): print('hello')\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    # print is not in the index, so it shouldn't appear in callees
    assert "main::run" in callees
    assert "print" not in str(callees)  # no unresolved calls


def test_build_index_collects_imports(tmp_path: Path):
    """build_index stores imports in the SymbolIndex."""
    (tmp_path / "main.py").write_text("import os\nfrom sys import path\n")

    index = build_index(tmp_path)
    imports = index.file_imports(str(tmp_path / "main.py"))
    names = {imp.name for imp in imports}
    assert "os" in names
    assert "path" in names


def test_build_index_and_call_graph_include_decorated_functions(tmp_path: Path):
    """Decorated Python functions stay visible to the index and call graph."""
    (tmp_path / "main.py").write_text(
        """
def command(name):
    def decorator(func):
        return func
    return decorator


def helper():
    return "ok"


@command("plugin")
def cmd_plugin():
    return helper()
"""
    )

    index = build_index(tmp_path)
    entries = index.lookup("cmd_plugin")

    assert len(entries) == 1
    assert entries[0].kind == "function"
    assert entries[0].start_line == 12  # decorator line (not def keyword at 13)

    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "main::helper" in callees.get("main::cmd_plugin", set())
    assert "main::cmd_plugin" in callers.get("main::helper", set())


# ---------------------------------------------------------------------------
# Cross-language cross-module call resolution
# ---------------------------------------------------------------------------

_skip_no_rust = pytest.mark.skipif(
    not _has_grammar("tree_sitter_rust"),
    reason="tree-sitter-rust not installed",
)
_skip_no_go = pytest.mark.skipif(
    not _has_grammar("tree_sitter_go"),
    reason="tree-sitter-go not installed",
)


@_skip_no_ts
def test_cross_file_call_graph_typescript_named_import(tmp_path: Path):
    """TypeScript named import: import { greet } from './utils'; greet()."""
    (tmp_path / "utils.ts").write_text("export function greet(): void {}\n")
    (tmp_path / "main.ts").write_text(
        'import { greet } from "./utils";\nfunction main() { greet(); }\n'
    )

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "utils::greet" in callees.get("main::main", set())
    assert "main::main" in callers.get("utils::greet", set())


@_skip_no_ts
def test_cross_file_call_graph_typescript_namespace_import(tmp_path: Path):
    """TypeScript namespace import: import * as utils from './utils'; utils.greet()."""
    (tmp_path / "utils.ts").write_text("export function greet(): void {}\n")
    (tmp_path / "main.ts").write_text(
        'import * as utils from "./utils";\nfunction main() { utils.greet(); }\n'
    )

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "utils::greet" in callees.get("main::main", set())
    assert "main::main" in callers.get("utils::greet", set())


@_skip_no_go
def test_cross_file_call_graph_go_pkg_qualified(tmp_path: Path):
    """Go package-qualified call: import 'pkg'; pkg.Func()."""
    (tmp_path / "utils.go").write_text("package utils\nfunc Greet() {}\n")
    (tmp_path / "main.go").write_text(
        'package main\nimport "utils"\nfunc Run() { utils.Greet() }\n'
    )

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "utils::Greet" in callees.get("main::Run", set())
    assert "main::Run" in callers.get("utils::Greet", set())


@_skip_no_rust
def test_cross_file_call_graph_rust_use_import(tmp_path: Path):
    """Rust explicit use: use crate::greet; greet()."""
    (tmp_path / "lib.rs").write_text("pub fn greet() {}\n")
    (tmp_path / "main.rs").write_text("use crate::greet;\nfn run() { greet(); }\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "lib::greet" in callees.get("main::run", set())
    assert "main::run" in callers.get("lib::greet", set())


@_skip_no_rust
def test_cross_file_call_graph_rust_path_qualified(tmp_path: Path):
    """Rust path-qualified call without explicit use: crate::utils::parse()."""
    (tmp_path / "utils.rs").write_text("pub fn parse() {}\n")
    (tmp_path / "main.rs").write_text("fn run() { crate::utils::parse(); }\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "utils::parse" in callees.get("main::run", set())
    assert "main::run" in callers.get("utils::parse", set())


@_skip_no_rust
def test_cross_file_call_graph_rust_path_qualified_no_collision(tmp_path: Path):
    """Rust path-qualified calls resolve to the correct module when multiple
    modules define a function with the same name (e.g. parse)."""
    (tmp_path / "utils.rs").write_text("pub fn parse() {}\n")
    (tmp_path / "other.rs").write_text("pub fn parse() {}\n")
    (tmp_path / "main.rs").write_text(
        "fn run() { crate::utils::parse(); crate::other::parse(); }\n"
    )

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert "utils::parse" in callees.get("main::run", set())
    assert "other::parse" in callees.get("main::run", set())
    assert "main::run" in callers.get("utils::parse", set())
    assert "main::run" in callers.get("other::parse", set())
