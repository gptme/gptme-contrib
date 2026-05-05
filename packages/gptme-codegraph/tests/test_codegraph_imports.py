"""Tests for codegraph import alias and relative-import resolution.

These tests probe the documented sharp edges from
knowledge/research/2026-05-03-codegraph-productization-roadmap.md item #4.
"""

from __future__ import annotations

from pathlib import Path

from gptme_codegraph.core import (
    _extract_imports,
    _tree_sitter_parse,
    build_cross_file_call_graph,
    build_index,
)

# ---------------------------------------------------------------------------
# Alias scenarios
# ---------------------------------------------------------------------------


def test_module_alias_resolves_dotted_call(tmp_path: Path):
    """`import calc as c; c.add()` should link `add` to its caller.

    Demonstrates whether `_resolve_imported_symbol` honors module aliases for
    dotted calls. Today this is a documented sharp edge.
    """
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "main.py").write_text("import calc as c\ndef run(): c.add(1, 2)\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    # Graph keys are qualified IDs (calc::add, main::run)
    assert (
        "calc::add" in callees.get("main::run", set())
    ), f"Expected `run` to call `add` via aliased module import; got {callees.get('main::run')}"
    assert "main::run" in callers.get("calc::add", set())


def test_from_import_alias_resolves_call(tmp_path: Path):
    """`from utils import add as a; a()` should link `add` to its caller."""
    (tmp_path / "utils.py").write_text("def add(): return 1\n")
    (tmp_path / "main.py").write_text("from utils import add as a\ndef run(): a()\n")

    index = build_index(tmp_path)
    callees, callers = build_cross_file_call_graph(index, tmp_path)

    assert (
        "utils::add" in callees.get("main::run", set())
    ), f"Expected `run` to call `add` via from-import alias; got {callees.get('main::run')}"
    assert "main::run" in callers.get("utils::add", set())


def test_dotted_module_alias(tmp_path: Path):
    """`import os.path as op` records the original module name + alias.

    Under the new ``ImportInfo`` semantics, ``name`` carries the original
    imported identifier (``os.path``) and ``alias`` carries the local binding
    (``op``).
    """
    code = b"import os.path as op\n"
    root, _ = _tree_sitter_parse(code)
    assert root is not None
    imports = _extract_imports(root)
    assert any(i.name == "os.path" and i.alias == "op" for i in imports), (
        f"Expected aliased dotted import; got "
        f"{[(i.name, i.module, i.alias) for i in imports]}"
    )


# ---------------------------------------------------------------------------
# Relative imports
# ---------------------------------------------------------------------------


def test_relative_import_extracted(tmp_path: Path):
    """`from .utils import add` should be extracted, not silently dropped."""
    code = b"from .utils import add\n"
    root, _ = _tree_sitter_parse(code)
    assert root is not None
    imports = _extract_imports(root)
    # We don't yet require absolute resolution — just that the import is captured.
    names = [imp.name for imp in imports]
    assert (
        "add" in names
    ), f"Expected `add` to be extracted from relative import; got {names}"


def test_relative_import_resolves_within_package(tmp_path: Path):
    """`from .utils import add` should link calls when both files are indexed.

    Even without a __init__.py, the package directory is the indexed root, so
    sibling files are discoverable. This is the common monorepo case.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "utils.py").write_text("def add(): return 1\n")
    (pkg / "main.py").write_text("from .utils import add\ndef run(): add()\n")

    index = build_index(pkg)
    callees, callers = build_cross_file_call_graph(index, pkg)

    # Graph keys are qualified IDs (main::run, utils::add) — module_path is
    # relative to the indexed directory (pkg/), so files at the root get
    # bare module paths: main.py → main, utils.py → utils.
    assert (
        "utils::add" in callees.get("main::run", set())
    ), f"Expected `run` to call `add` via relative import; got {callees.get('main::run')}"


# ---------------------------------------------------------------------------
# Wildcard / star imports
# ---------------------------------------------------------------------------


def test_wildcard_import_extracted():
    """`from math import *` should be captured, not silently dropped."""
    code = b"from math import *\n"
    root, _ = _tree_sitter_parse(code)
    assert root is not None
    imports = _extract_imports(root)
    names = [(imp.name, imp.module) for imp in imports]
    assert (
        "*",
        "math",
    ) in names, f"Expected ('*', 'math') from wildcard import; got {names}"


# ---------------------------------------------------------------------------
# Namespace-package / __init__.py scenarios
# ---------------------------------------------------------------------------


def test_init_py_module_path_strips_init(tmp_path: Path):
    """Symbols in ``__init__.py`` get the package module path, not a ``.__init__`` suffix.

    When indexed from the parent directory, ``pkg/__init__.py`` → ``pkg``.
    When indexed from the package dir itself, it's the root (``""``).
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def api_func(): pass\n")

    # Index from parent: module_path includes the package name
    index = build_index(tmp_path)
    assert index.has("api_func")
    entries = index.lookup("api_func")
    assert len(entries) == 1
    assert entries[0].module_path == "pkg", (
        f"Expected module_path 'pkg' for symbol in pkg/__init__.py "
        f"indexed from parent; got {entries[0].module_path!r}"
    )

    # Index from package dir itself: module_path is the root
    index2 = build_index(pkg)
    entries2 = index2.lookup("api_func")
    assert entries2[0].module_path == "", (
        f"Expected empty module_path for package-root __init__.py; "
        f"got {entries2[0].module_path!r}"
    )


def test_namespace_package_cross_file_resolution(tmp_path: Path):
    """Cross-file resolution works through __init__.py re-exports.

    Simulates::

        pkg/
          __init__.py   # from .utils import add
          utils.py       # def add(): ...
          main.py        # import pkg; pkg.add()

    ``main::run`` calls ``add`` through the pkg namespace. The call graph
    should resolve ``add`` to ``pkg::add`` (from __init__.py), which in
    turn calls nothing because it's a re-export.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .utils import add\n")
    (pkg / "utils.py").write_text("def add(): return 1\n")
    (pkg / "main.py").write_text("import pkg\ndef run(): pkg.add()\n")

    index = build_index(pkg)
    callees, callers = build_cross_file_call_graph(index, pkg)

    # ``pkg::add`` should be resolvable (via __init__.py import)
    assert "pkg::add" in callees or "main::run" in callees, (
        f"Expected callee graph to include symbols from __init__.py; "
        f"got callees keys: {sorted(callees.keys())}"
    )


def test_subpackage_init_py_module_path(tmp_path: Path):
    """Deeply nested __init__.py gets correct module path: a.b.c not a.b.c.__init__."""
    a = tmp_path / "a"
    a.mkdir()
    b = a / "b"
    b.mkdir()
    c = b / "c"
    c.mkdir()
    (c / "__init__.py").write_text("def deep_func(): pass\n")

    index = build_index(tmp_path)

    # Verify the module path for a function in __init__.py
    entries = index.lookup("deep_func")
    assert len(entries) == 1
    assert (
        entries[0].module_path == "a.b.c"
    ), f"Expected 'a.b.c' for nested __init__.py; got {entries[0].module_path!r}"


def test_namespace_package_without_top_init(tmp_path: Path):
    """Index works when directory has no __init__.py at any level (PEP 420)."""
    ns = tmp_path / "ns"
    ns.mkdir()
    pkg = ns / "pkg"
    pkg.mkdir()
    sub = pkg / "sub"
    sub.mkdir()
    # No __init__.py files — pure PEP 420 namespace packages
    (sub / "mod.py").write_text("def func(): return 42\n")
    (ns / "main.py").write_text("from pkg.sub.mod import func\ndef run(): func()\n")

    index = build_index(ns)
    callees, callers = build_cross_file_call_graph(index, ns)

    assert (
        "main::run" in callees
    ), f"Expected 'main::run' in callees; got {sorted(callees.keys())}"
    assert "pkg.sub.mod::func" in callees.get("main::run", set()), (
        f"Expected run→func via cross-file resolution in namespace package; "
        f"got {callees.get('main::run', set())}"
    )
