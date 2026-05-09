"""gptme-codegraph — structural code retrieval via tree-sitter.

Complementary to gptme-rag (text chunks), this retrieves code *structure*:
function/class definitions, call graphs, and blast radius.

v0: In-memory, single-file Python only. Tree-sitter grammars are the
bottleneck (one per language). This prototype proves the concept before
committing to SQLite + multi-language + persistent indexing.

Usage:
    gptme-codegraph <file.py> parse           # Extract symbols
    gptme-codegraph <file.py> callers <name>  # Who calls X?
    gptme-codegraph <file.py> callees <name>  # What does X call?
    python3 scripts/codegraph.py <file.py> deps <name>     # Dependency closure (what X needs)
    python3 scripts/codegraph.py <file.py> impact <name>   # Impact radius (what needs X)
    python3 scripts/codegraph.py <file.py> blast <name>    # [deprecated] alias for deps
    python3 scripts/codegraph.py <file.py> def <name>      # Where is X defined?
    python3 scripts/codegraph.py <file.py> refs <name>     # Where is X referenced?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _module_path(filepath: str, directory: str | None = None) -> str:
    """Convert a file path to a dotted module path (e.g. 'scripts/codegraph').

    Strips ``__init__`` from package-roots so ``pkg/__init__.py`` maps to
    ``pkg`` rather than ``pkg.__init__``, matching Python runtime semantics.
    """
    p = Path(filepath)
    if directory:
        try:
            rel = p.relative_to(directory)
        except ValueError:
            # Fallback: use filepath as-is
            rel = p
    else:
        # No directory context: use the path as given, strip .py
        rel = p
    # Remove .py suffix and replace / with dot
    parts = str(rel.with_suffix("")).replace("\\", "/").split("/")
    # Strip trailing ``__init__`` so packages get the correct module path
    if parts and parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    return module


@dataclass
class IndexEntry:
    """A lightweight cross-file symbol registry entry."""

    name: str
    kind: str  # function, method, class
    file: str
    start_line: int
    end_line: int
    parent_class: str | None = None
    module_path: str = ""  # dotted module path, set by build_index

    def qualified_id(self) -> str:
        """Return a stable qualified ID for this symbol.

        Format: ``module_path::Class.method`` for methods,
        ``module_path::name`` for top-level symbols.
        """
        if self.parent_class:
            return f"{self.module_path}::{self.parent_class}.{self.name}"
        return f"{self.module_path}::{self.name}"


@dataclass
class ImportInfo:
    """A single import in a Python file.

    Naming conventions:
    - ``name`` is the *original imported name* as written in the source.
      For ``import os`` it is ``"os"``; for ``from os import path`` it is
      ``"path"``; for ``from os import path as p`` it is still ``"path"``.
    - ``alias`` is the local binding when the source uses ``as``. For
      ``import calc as c`` it is ``"c"``; for ``from utils import add`` it is
      ``None``.
    - The local binding visible in the importing file is
      ``alias or name``; use :func:`local_binding` to compute it.
    """

    file: str
    name: str
    module: str | None = None  # source module (e.g. 'os.path')
    is_from: bool = False
    alias: str | None = None


def local_binding(imp: ImportInfo) -> str:
    """Return the local name an import binds in its file.

    Equal to ``alias`` when present, otherwise ``name``. For
    ``from os import path as p`` returns ``"p"``; for ``import calc`` returns
    ``"calc"``.
    """
    return imp.alias or imp.name


@dataclass
class SymbolIndex:
    """Cross-file symbol registry.

    Maps symbol names to their definitions across a directory tree.
    Also tracks imports per file for cross-file call resolution.
    """

    entries: dict[str, list[IndexEntry]] = field(default_factory=dict)
    imports: dict[str, list[ImportInfo]] = field(default_factory=dict)

    def lookup(self, name: str) -> list[IndexEntry]:
        """Return all definitions of ``name`` across the index."""
        return self.entries.get(name, [])

    def has(self, name: str) -> bool:
        """Return True if ``name`` exists anywhere in the index."""
        return name in self.entries and bool(self.entries[name])

    def all_names(self) -> list[str]:
        """Return all indexed symbol names."""
        return sorted(self.entries.keys())

    def file_imports(self, filepath: str) -> list[ImportInfo]:
        """Return all imports in the given file."""
        return self.imports.get(filepath, [])


# ---------------------------------------------------------------------------
# SQLite-backed index cache (persists across server restarts)
# ---------------------------------------------------------------------------

_CODEGRAPH_STATE_DIR = Path.home() / ".local" / "state" / "codegraph"


def _ensure_state_dir() -> Path:
    """Create and return the state directory for codegraph caches."""
    _CODEGRAPH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _CODEGRAPH_STATE_DIR


def _db_path(directory: str) -> str:
    """Return the SQLite database path for a directory (sanitized).

    Uses a hash of the absolute path to avoid filesystem-unsafe characters
    in the filename, and appends the directory basename for readability.
    """
    raw = str(Path(directory).resolve())
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    name = Path(raw).name or "root"
    return str(_ensure_state_dir() / f"index_{name}_{h}.db")


def _file_mtime(path: Path) -> float:
    """Return file mtime, or 0 if file doesn't exist."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class SqliteIndexCache:
    """Persistent SQLite-backed cache for SymbolIndex data.

    Stores:
    - IndexEntry rows (name, kind, file, start_line, end_line, parent_class, module_path)
    - File mtimes so stale directories are detected on rebuild
    - A metadata row for cache version tracking

    This is an optional enhancement over the in-memory cache. When available,
    the MCP server loads from SQLite instead of re-parsing on every startup.
    """

    SCHEMA_VERSION = 4

    def __init__(self, directory: str):
        self.db_path = _db_path(directory)
        self._directory = str(Path(directory).resolve())
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_schema(self) -> None:
        conn = self._connect()
        # Ensure the metadata table exists before reading schema_version. We
        # drop stale data tables when the persisted schema is older than the
        # current code expects, which prevents silent corruption on upgrade.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        existing_version = int(row[0]) if row else None
        if existing_version is not None and existing_version != self.SCHEMA_VERSION:
            for tbl in ("entries", "file_mtimes", "imports"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.commit()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                file TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                parent_class TEXT,
                module_path TEXT NOT NULL DEFAULT '',
                directory TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entries_name ON entries(name);
            CREATE INDEX IF NOT EXISTS idx_entries_dir ON entries(directory);
            CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file);
            CREATE TABLE IF NOT EXISTS file_mtimes (
                filepath TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                directory TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imports (
                file TEXT NOT NULL,
                name TEXT NOT NULL,
                module TEXT,
                is_from INTEGER NOT NULL DEFAULT 0,
                alias TEXT,
                directory TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file);
            CREATE INDEX IF NOT EXISTS idx_imports_dir ON imports(directory);
        """)
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("schema_version", str(self.SCHEMA_VERSION)),
        )
        conn.commit()

    def is_fresh(self) -> bool:
        """Check if the cached index is up-to-date with the filesystem.

        Returns True if:
        - The database has entries for this directory
        - All indexed files still exist and have the same mtime
        - No new Python files exist in the directory
        """
        conn = self._connect()
        self._init_schema()

        count = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE directory = ?",
            (self._directory,),
        ).fetchone()[0]
        if count == 0:
            return False

        dir_path = Path(self._directory)
        if not dir_path.is_dir():
            return False

        rows = conn.execute(
            "SELECT filepath, mtime FROM file_mtimes WHERE directory = ?",
            (self._directory,),
        ).fetchall()
        tracked = {row[0]: row[1] for row in rows}

        for filepath, cached_mtime in tracked.items():
            fp = Path(filepath)
            if not fp.exists():
                return False
            if _file_mtime(fp) != cached_mtime:
                return False

        # Check that no new Python files appeared
        for fp in dir_path.rglob("*.py"):
            if str(fp.resolve()) not in tracked:
                return False

        return True

    def save(self, index: SymbolIndex) -> None:
        """Persist a SymbolIndex to SQLite."""
        conn = self._connect()
        self._init_schema()

        conn.execute("DELETE FROM entries WHERE directory = ?", (self._directory,))
        conn.execute("DELETE FROM imports WHERE directory = ?", (self._directory,))
        conn.execute("DELETE FROM file_mtimes WHERE directory = ?", (self._directory,))

        seen_files: set[str] = set()
        for name, entries in index.entries.items():
            for entry in entries:
                conn.execute(
                    """INSERT INTO entries (name, kind, file, start_line, end_line, parent_class, module_path, directory)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.name,
                        entry.kind,
                        entry.file,
                        entry.start_line,
                        entry.end_line,
                        entry.parent_class,
                        entry.module_path,
                        self._directory,
                    ),
                )
                seen_files.add(entry.file)

        for file_path, imp_list in index.imports.items():
            for imp in imp_list:
                conn.execute(
                    """INSERT INTO imports (file, name, module, is_from, alias, directory)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        imp.file,
                        imp.name,
                        imp.module,
                        1 if imp.is_from else 0,
                        imp.alias,
                        self._directory,
                    ),
                )
                seen_files.add(imp.file)

        for fp_str in seen_files:
            fp = Path(fp_str)
            conn.execute(
                "INSERT OR REPLACE INTO file_mtimes (filepath, mtime, directory) VALUES (?, ?, ?)",
                (fp_str, _file_mtime(fp), self._directory),
            )

        # Also track all Python files to detect new files on freshness check
        dir_path = Path(self._directory)
        for fp in dir_path.rglob("*.py"):
            fp_str = str(fp.resolve())
            if fp_str not in seen_files:
                conn.execute(
                    "INSERT OR IGNORE INTO file_mtimes (filepath, mtime, directory) VALUES (?, ?, ?)",
                    (fp_str, _file_mtime(fp), self._directory),
                )

        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("last_updated", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def load(self) -> SymbolIndex | None:
        """Load a persisted SymbolIndex from SQLite, or None if empty or stale."""
        if not self.is_fresh():
            return None

        conn = self._connect()
        self._init_schema()

        rows = conn.execute(
            "SELECT name, kind, file, start_line, end_line, parent_class, module_path "
            "FROM entries WHERE directory = ? ORDER BY name",
            (self._directory,),
        ).fetchall()
        if not rows:
            return None

        entries: dict[str, list[IndexEntry]] = {}
        for name, kind, file, start_line, end_line, parent_class, module_path in rows:
            entry = IndexEntry(
                name=str(name),
                kind=str(kind),
                file=str(file),
                start_line=int(start_line),
                end_line=int(end_line),
                parent_class=str(parent_class) if parent_class else None,
                module_path=str(module_path) if module_path else "",
            )
            entries.setdefault(str(name), []).append(entry)

        imports: dict[str, list[ImportInfo]] = {}
        imp_rows = conn.execute(
            "SELECT file, name, module, is_from, alias "
            "FROM imports WHERE directory = ?",
            (self._directory,),
        ).fetchall()
        for file, name, module, is_from, alias in imp_rows:
            imp = ImportInfo(
                file=str(file),
                name=str(name),
                module=str(module) if module else None,
                is_from=bool(is_from),
                alias=str(alias) if alias else None,
            )
            imports.setdefault(str(file), []).append(imp)

        return SymbolIndex(entries=entries, imports=imports)

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


@dataclass
class Symbol:
    """A parsed symbol with its definition and location."""

    name: str
    kind: str  # function, class, method
    file: str
    start_line: int
    end_line: int
    parent_class: str | None = None
    docstring: str | None = None
    calls: list[str] = field(default_factory=list)  # symbols called by this one
    module_path: str = ""  # dotted module path, set by build_index

    def qualified_id(self) -> str:
        """Return a stable qualified ID for this symbol.

        Format: ``module_path::Class.method`` for methods,
        ``module_path::name`` for top-level symbols.
        """
        if self.parent_class:
            return f"{self.module_path}::{self.parent_class}.{self.name}"
        return f"{self.module_path}::{self.name}"


@dataclass
class FileParseResult:
    """Result of parsing a single Python file."""

    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)


def _tree_sitter_parse(code: bytes, lang_name: str = "python"):
    """Parse source code with tree-sitter and return the root node.

    Falls back gracefully if tree-sitter is not installed or the grammar
    is unavailable. Returns (root_node, parser) on success.
    """
    try:
        import tree_sitter_python as tspython  # type: ignore[import-not-found]
        from tree_sitter import Language, Parser  # type: ignore[import-untyped]

        parser = Parser()
        language = Language(tspython.language())
        parser.language = language
        tree = parser.parse(code)
        if tree is None:
            return None, None
        return tree.root_node, parser
    except ImportError:
        return None, None


def _text(node) -> str:
    """Get the text of a tree-sitter node."""
    try:
        return str(node.text.decode("utf-8"))
    except Exception:
        return ""


def _extract_docstring(body_node) -> str | None:
    """Extract docstring from a function/class body node."""
    try:
        for child in body_node.named_children:
            if child.type == "expression_statement":
                expr = child.child(0)
                if expr and expr.type == "string":
                    raw = _text(expr)
                    if raw.startswith(('"""', "'''")):
                        return raw[3:-3].strip()
                    elif raw.startswith(('"', "'")):
                        return raw[1:-1].strip()
        return None
    except Exception:
        return None


def _extract_calls(node) -> list[str]:
    """Extract all called function names from a tree-sitter node subtree."""
    calls = []
    try:
        cursor = node.walk()
        reached_root = False
        while not reached_root:
            if cursor.node.type == "call":
                func_node = cursor.node.child_by_field_name("function")
                if func_node:
                    calls.append(_text(func_node))
            if cursor.goto_first_child():
                continue
            if cursor.goto_next_sibling():
                continue
            cursor.goto_parent()
            if cursor.goto_next_sibling():
                continue
            while not cursor.goto_next_sibling():
                if not cursor.goto_parent():
                    reached_root = True
                    break
    except Exception:
        pass
    return calls


def _extract_imports(root) -> list[ImportInfo]:
    """Extract all import statements from a parsed Python file.

    Handles:
    - import os
    - import os.path
    - from os import path
    - from os import path as p
    - from os import *
    """
    imports: list[ImportInfo] = []
    try:
        for node in root.named_children:
            if node.type == "import_statement":
                # import foo, foo.bar
                # import numpy as np
                for child in node.children:
                    if child.type == "dotted_name":
                        module = _text(child)
                        imports.append(
                            ImportInfo(
                                file="",
                                name=module.split(".")[0],
                                module=module,
                                is_from=False,
                            )
                        )
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        name = _text(name_node) if name_node else ""
                        alias = _text(alias_node) if alias_node else None
                        # ``import calc as c`` -> name="calc", alias="c".
                        # ``module`` mirrors the original dotted name so
                        # consumers can still recover the source path.
                        imports.append(
                            ImportInfo(
                                file="",
                                name=name,
                                module=name,
                                is_from=False,
                                alias=alias,
                            )
                        )
            elif node.type == "import_from_statement":
                # from module import name [as alias], ...
                module_node = node.child_by_field_name("module_name")
                from_module: str | None = _text(module_node) if module_node else None
                # Find all imported names (skip keywords like 'from', 'import')
                for child in node.children:
                    if child.type == "dotted_name":
                        # This could be the module name or an imported name
                        if child == module_node:
                            continue
                        name = _text(child)
                        imports.append(
                            ImportInfo(
                                file="",
                                name=name,
                                module=from_module,
                                is_from=True,
                            )
                        )
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        name = _text(name_node) if name_node else ""
                        alias = _text(alias_node) if alias_node else None
                        # ``from os import path as p`` -> name="path", alias="p".
                        # The original imported symbol is preserved in ``name``
                        # so cross-file resolution can find it.
                        imports.append(
                            ImportInfo(
                                file="",
                                name=name,
                                module=from_module,
                                is_from=True,
                                alias=alias,
                            )
                        )
                    elif child.type == "wildcard_import":
                        # ``from os import *`` — record module only, can't
                        # enumerate actual names without runtime import.
                        imports.append(
                            ImportInfo(
                                file="",
                                name="*",
                                module=from_module,
                                is_from=True,
                            )
                        )
    except Exception:
        pass
    return imports


def parse_file(filepath: Path) -> FileParseResult:
    """Extract all symbols and imports from a Python file."""
    code = filepath.read_bytes()
    root, _parser = _tree_sitter_parse(code)
    if root is None:
        return FileParseResult()

    filename = str(filepath)
    symbols: list[Symbol] = []

    def walk(node, parent_class: str | None = None):
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node:
                name = _text(name_node)
                calls = _extract_calls(body_node) if body_node else []
                docstring = _extract_docstring(body_node) if body_node else None
                kind = "method" if parent_class else "function"
                symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file=filename,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_class,
                        docstring=docstring,
                        calls=list(set(calls)),
                    )
                )
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node:
                name = _text(name_node)
                docstring = _extract_docstring(body_node) if body_node else None
                symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        file=filename,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        docstring=docstring,
                    )
                )
                if body_node:
                    for child in body_node.named_children:
                        walk(child, parent_class=name)

    for child in root.named_children:
        walk(child)

    imports = _extract_imports(root)
    for imp in imports:
        imp.file = filename

    return FileParseResult(symbols=symbols, imports=imports)


def extract_symbols(filepath: Path) -> list[Symbol]:
    """Extract all symbols (functions, classes) from a Python file.

    Returns a list of Symbol objects with names, locations, docstrings,
    and call graphs. (Convenience wrapper around parse_file.)
    """
    return parse_file(filepath).symbols


def build_index(directory: str | Path) -> SymbolIndex:
    """Build a cross-file symbol index for all Python files in a directory.

    Scans all ``.py`` files recursively, extracts symbols and imports, and
    creates an ``IndexEntry`` for each symbol and ``ImportInfo`` for each import.
    """
    index = SymbolIndex()
    directory = Path(directory)
    dir_str = str(directory)
    for fp in sorted(directory.rglob("*.py")):
        # Skip common non-source directories
        parts = fp.relative_to(directory).parts
        if any(
            p.startswith(".") or p in ("__pycache__", "node_modules", ".git")
            for p in parts
        ):
            continue
        result = parse_file(fp)
        mp = _module_path(str(fp), dir_str)
        for sym in result.symbols:
            entry = IndexEntry(
                name=sym.name,
                kind=sym.kind,
                file=sym.file,
                start_line=sym.start_line,
                end_line=sym.end_line,
                parent_class=sym.parent_class,
                module_path=mp,
            )
            index.entries.setdefault(sym.name, []).append(entry)
        if result.imports:
            index.imports[result.imports[0].file] = result.imports
    return index


def build_call_graph(
    symbols: list[Symbol],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build bidirectional call graph from extracted symbols.

    Returns (callees, callers) where:
    - callees[X] = set of symbol qualified IDs that symbol X calls
    - callers[Y] = set of symbol qualified IDs that call symbol Y

    Graph keys are stable qualified IDs (``module_path::Class.method``)
    rather than bare names, preventing collisions across files.
    """
    known_names = {s.name for s in symbols}
    # Map bare names → qualified IDs for the single-file case.
    # Within one file, name collisions don't occur, so this is 1:1.
    name_to_qid: dict[str, str] = {}
    for s in symbols:
        qid = s.qualified_id()
        name_to_qid[s.name] = qid

    callees: dict[str, set[str]] = {}
    callers: dict[str, set[str]] = defaultdict(set)

    for sym in symbols:
        if sym.kind == "class":
            continue
        qid = name_to_qid.get(sym.name, sym.qualified_id())
        # Filter to known symbols only: excludes external builtins and dotted
        # attribute calls like 'calc.add' that can't be resolved locally.
        filtered_calls = {name_to_qid[c] for c in sym.calls if c in known_names}
        callees[qid] = filtered_calls
        for called_qid in filtered_calls:
            callers[called_qid].add(qid)

    return callees, callers


def _resolve_imported_symbol(
    call: str, file_imports: list[ImportInfo], directory: Path
) -> str | None:
    """Try to resolve a dotted call like 'module.func' to a symbol name.

    Uses import map to find which file defines the called symbol.
    Returns the resolved symbol name if found, None otherwise.

    Honors aliases on both forms:
    - ``import calc as c`` then ``c.add()`` -> ``"add"``
    - ``from utils import add as a`` then ``a()`` -> ``"add"``
    """
    if not call:
        return None

    parts = call.split(".")
    if not parts:
        return None

    # Check direct from-import: ``from module import func`` -> ``func()``.
    # Also handles aliased from-imports: ``from m import f as g; g()``.
    for imp in file_imports:
        if imp.is_from and len(parts) == 1 and local_binding(imp) == parts[0]:
            # Return the original symbol name, not the alias, so the call links
            # to the actual definition rather than a non-existent alias entry.
            return imp.name

    # Check module import: ``import module; module.func()`` and aliased forms
    # like ``import module.sub as ms; ms.func()``.
    for imp in file_imports:
        if not imp.is_from and imp.module and len(parts) > 1:
            local = local_binding(imp)
            mod_first = imp.module.split(".")[0]
            # Match either the local binding (alias-aware) or the raw module
            # head. The latter keeps backward compatibility with imports that
            # don't actually have an alias.
            if parts[0] == local or parts[0] == mod_first:
                return parts[-1]

    # Check from-import where the imported name is itself a module:
    # ``from pkg import sub`` then ``sub.func()`` -> ``func``.
    for imp in file_imports:
        if (
            imp.is_from
            and imp.module
            and len(parts) > 1
            and parts[0] == local_binding(imp)
        ):
            return parts[-1]

    return None


def build_cross_file_call_graph(
    index: SymbolIndex,
    directory: Path,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build a unified call graph across all files in the index.

    Resolves calls through imports to link symbols across files.
    Graph keys are stable qualified IDs (``module_path::Class.method``)
    rather than bare names, preventing collisions across files.
    Returns (callees, callers) dictionaries.
    """
    # Re-parse all indexed files to get symbols with their calls
    # (IndexEntry does not store calls, so we need the full Symbol)
    all_symbols: list[Symbol] = []
    file_symbols: dict[str, list[Symbol]] = {}
    seen_files: set[str] = set()
    for entries in index.entries.values():
        for entry in entries:
            seen_files.add(entry.file)

    for file_path in seen_files:
        fp = Path(file_path)
        if fp.exists():
            result = parse_file(fp)
            # Set module_path from the file path and indexed directory
            mp = _module_path(str(fp), str(directory))
            for sym in result.symbols:
                sym.module_path = mp
                all_symbols.append(sym)
                file_symbols.setdefault(file_path, []).append(sym)

    known_names = {s.name for s in all_symbols}
    # Build name → qualified ID map for cross-file resolution.
    # When multiple files define the same bare name, the first entry is
    # the default (one-to-many names are rare and the index handles them
    # separately via ``index.lookup()``).
    name_to_qid: dict[str, str] = {}
    for sym in all_symbols:
        qid = sym.qualified_id()
        if sym.name not in name_to_qid:
            name_to_qid[sym.name] = qid

    callees: dict[str, set[str]] = {}
    callers: dict[str, set[str]] = defaultdict(set)

    for file_path, symbols in file_symbols.items():
        imports = index.file_imports(file_path)
        for sym in symbols:
            if sym.kind == "class":
                continue
            resolved_calls: set[str] = set()
            for call in sym.calls:
                if call in known_names:
                    # Local or same-file call
                    resolved_calls.add(call)
                else:
                    # Try import resolution for both dotted (``mod.func``)
                    # and plain (``alias_local``) calls. Plain calls matter for
                    # ``from m import f as g; g()`` — the local binding is not
                    # in ``known_names`` but resolution maps it back to ``f``.
                    resolved = _resolve_imported_symbol(call, imports, directory)
                    if resolved and resolved in known_names:
                        resolved_calls.add(resolved)
                    # else: external builtin/library — ignore
            qid = name_to_qid.get(sym.name, sym.qualified_id())
            qid_calls = {name_to_qid.get(c, c) for c in resolved_calls}
            callees[qid] = qid_calls
            for called in qid_calls:
                callers[called].add(qid)

    return callees, callers


def _resolve_graph_key(name: str, graph: dict[str, set[str]]) -> str | None:
    """Resolve a user-provided bare name to a qualified graph key.

    Tries exact match first, then ``::name`` (common for symbols from
    temp/single files with empty module_path), then scans all keys
    for a ``::name`` suffix.
    Returns the resolved key or None.
    """
    if name in graph:
        return name
    flat = "::" + name
    if flat in graph:
        return flat
    # Scan for ::name suffix (handles module_path::name qualified IDs)
    for key in graph:
        if key.endswith("::" + name):
            return key
    return None


def dependency_closure(
    name: str,
    callees: dict[str, set[str]],
    max_depth: int = 10,
) -> dict[str, set[str]]:
    """Compute the dependency closure of a symbol.

    Walks callees (downstream): what does this symbol depend on?
    Returns dict mapping depth level to set of symbols.
    ``name`` can be a bare name or a qualified ID.
    """
    resolved = _resolve_graph_key(name, callees)
    if resolved is None:
        return {"depth_0": {name}}

    visited: set[str] = set()
    radius: dict[str, set[str]] = {}
    queue: list[tuple[str, int]] = [(resolved, 0)]

    while queue:
        current, depth = queue.pop(0)
        if current in visited or depth > max_depth:
            continue
        visited.add(current)
        radius.setdefault(f"depth_{depth}", set()).add(current)
        for callee in callees.get(current, set()):
            if callee not in visited:
                queue.append((callee, depth + 1))

    return radius


def impact_radius(
    name: str,
    callers: dict[str, set[str]],
    max_depth: int = 10,
) -> dict[str, set[str]]:
    """Compute the impact radius of a symbol.

    Walks callers (upstream): what breaks if I change this symbol?
    Returns dict mapping depth level to set of affected symbols.
    ``name`` can be a bare name or a qualified ID.
    """
    resolved = _resolve_graph_key(name, callers)
    if resolved is None:
        return {"depth_0": {name}}

    visited: set[str] = set()
    radius: dict[str, set[str]] = {}
    queue: list[tuple[str, int]] = [(resolved, 0)]

    while queue:
        current, depth = queue.pop(0)
        if current in visited or depth > max_depth:
            continue
        visited.add(current)
        radius.setdefault(f"depth_{depth}", set()).add(current)
        for caller in callers.get(current, set()):
            if caller not in visited:
                queue.append((caller, depth + 1))

    return radius


# Keep backward-compatible alias
blast_radius = dependency_closure


def format_json(data) -> str:
    """Format data as pretty-printed JSON."""
    return json.dumps(data, indent=2, default=str)


def format_symbols(symbols: list[Symbol]) -> str:
    """Format extracted symbols as human-readable text."""
    funcs = [s for s in symbols if s.kind == "function"]
    classes = [s for s in symbols if s.kind == "class"]
    methods = [s for s in symbols if s.kind == "method"]
    total = len(symbols)

    lines = [
        f"Functions: {len(funcs)}  Classes: {len(classes)}  Methods: {len(methods)}",
        f"Total: {total}",
    ]
    if not symbols:
        return "\n".join(lines)

    lines.append("")
    for s in symbols:
        qual = f" (class {s.parent_class})" if s.parent_class else ""
        lines.append(f"{s.kind} {s.name}{qual}: L{s.start_line}-{s.end_line}")
        if s.docstring:
            lines.append(f"  {s.docstring[:200]}")
        if s.calls:
            lines.append(f"  calls: {', '.join(sorted(s.calls))}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Structural code retrieval via tree-sitter"
    )
    parser.add_argument("file", help="Python file to analyze")
    parser.add_argument(
        "--directory",
        help="Cross-file index directory (enables cross-file lookups)",
    )
    parser.add_argument(
        "--use-sqlite",
        action="store_true",
        help="Use SQLite cache for cross-file index (persists across runs)",
    )
    sub = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    p_parse = sub.add_parser("parse", help="Extract symbols")
    p_parse.add_argument("--json", action="store_true", help="Output as JSON")

    p_callers = sub.add_parser("callers", help="Find callers of a symbol")
    p_callers.add_argument("name", help="Symbol name to find callers for")
    p_callers.add_argument("--json", action="store_true", help="Output as JSON")

    p_callees = sub.add_parser("callees", help="Find symbols called by a symbol")
    p_callees.add_argument("name", help="Symbol name")
    p_callees.add_argument("--json", action="store_true", help="Output as JSON")

    p_blast = sub.add_parser("blast", help="[deprecated] Use 'impact' or 'deps'")
    p_blast.add_argument("name", help="Symbol name")
    p_blast.add_argument(
        "--max-depth", type=int, default=10, help="Maximum depth (default: 10)"
    )
    p_blast.add_argument("--json", action="store_true", help="Output as JSON")

    p_deps = sub.add_parser(
        "deps", help="Compute dependency closure (what this symbol depends on)"
    )
    p_deps.add_argument("name", help="Symbol name")
    p_deps.add_argument(
        "--max-depth", type=int, default=10, help="Maximum depth (default: 10)"
    )
    p_deps.add_argument("--json", action="store_true", help="Output as JSON")

    p_impact = sub.add_parser(
        "impact", help="Compute impact radius (what depends on this symbol)"
    )
    p_impact.add_argument("name", help="Symbol name")
    p_impact.add_argument(
        "--max-depth", type=int, default=10, help="Maximum depth (default: 10)"
    )
    p_impact.add_argument("--json", action="store_true", help="Output as JSON")

    p_def = sub.add_parser("def", help="Find where a symbol is defined")
    p_def.add_argument("name", help="Symbol name")
    p_def.add_argument("--json", action="store_true", help="Output as JSON")

    p_refs = sub.add_parser("refs", help="Find references to a symbol")
    p_refs.add_argument("name", help="Symbol name")
    p_refs.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    filepath = Path(args.file)

    if not filepath.exists():
        sys.exit(f"File not found: {filepath}")

    symbols = extract_symbols(filepath)
    if not symbols:
        print("No symbols found.")
        return

    name_map = {s.name: s for s in symbols}

    # Cross-file index: build if --directory is given
    index: SymbolIndex | None = None
    if args.directory is not None:
        index_dir = Path(args.directory)
        if not index_dir.exists():
            sys.exit(f"Directory not found: {index_dir}")
        if args.use_sqlite:
            cache = SqliteIndexCache(str(index_dir))
            cached = cache.load()
            if cached is not None:
                index = cached
            else:
                index = build_index(index_dir)
                cache.save(index)
            cache.close()
        else:
            index = build_index(index_dir)

    # Build call graph: cross-file when directory is given, local-only otherwise
    if index is not None:
        callees_graph, callers_graph = build_cross_file_call_graph(
            index, Path(args.directory)
        )
    else:
        callees_graph, callers_graph = build_call_graph(symbols)

    # Helper: resolve a symbol locally or from the cross-file index
    def resolve(name: str) -> Symbol | None:
        found = name_map.get(name)
        if found is not None:
            return found
        if index is not None and index.has(name):
            entry = index.lookup(name)[0]
            return Symbol(
                name=entry.name,
                kind=entry.kind,
                file=entry.file,
                start_line=entry.start_line,
                end_line=entry.end_line,
                parent_class=entry.parent_class,
            )
        return None

    if args.command == "parse":
        if args.json:
            print(format_json([asdict(s) for s in symbols]))
        else:
            print(format_symbols(symbols))

    elif args.command == "callers":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        if index is not None and index.has(args.name):
            entries = index.lookup(args.name)
            cross_file_info = f"\n  Defined in {len(entries)} file(s):" + "".join(
                f"\n    {e.file}:L{e.start_line}" for e in entries[:10]
            )
        else:
            cross_file_info = ""
        resolved_key = _resolve_graph_key(args.name, callers_graph)
        callers = sorted(callers_graph.get(resolved_key, set())) if resolved_key else []
        if args.json:
            output: dict = {"symbol": args.name, "callers": callers}
            if index and index.has(args.name):
                output["definitions"] = [
                    {"file": e.file, "line": e.start_line}
                    for e in index.lookup(args.name)
                ]
            print(format_json(output))
        else:
            print(f"Callers of {args.name}():{cross_file_info}")
            if callers:
                for c in callers:
                    s = resolve(c)
                    if s:
                        print(
                            f"  {c}()  L{s.start_line} ({s.file})"
                            if index
                            else f"  {c}()  L{s.start_line}-{s.end_line}"
                        )
                    else:
                        print(f"  {c}()")
            else:
                print("  (none found)")

    elif args.command == "callees":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        resolved_key = _resolve_graph_key(args.name, callees_graph)
        callees = sorted(callees_graph.get(resolved_key, set())) if resolved_key else []
        if args.json:
            output = {"symbol": args.name, "callees": callees}
            if index and index.has(args.name):
                output["definitions"] = [
                    {"file": e.file, "line": e.start_line}
                    for e in index.lookup(args.name)
                ]
            print(format_json(output))
        else:
            print(f"{args.name}() calls:")
            if callees:
                for c in callees:
                    s = resolve(c)
                    if s:
                        print(
                            f"  {c}()  L{s.start_line} ({s.file})"
                            if index
                            else f"  {c}()  L{s.start_line}-{s.end_line}"
                        )
                    else:
                        print(f"  {c}()  (external)")
            else:
                print("  (none)")

    elif args.command == "blast":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        # backward compat: walk callees
        radius = dependency_closure(args.name, callees_graph, max_depth=args.max_depth)
        if args.json:
            print(
                format_json(
                    {
                        "symbol": args.name,
                        "blast_radius": {k: sorted(v) for k, v in radius.items()},
                    }
                )
            )
        else:
            print(f"Blast radius of {args.name}() (dependency closure):")
            total = 0
            for depth_key in sorted(radius.keys()):
                deps = sorted(radius[depth_key])
                if deps:
                    print(f"  {depth_key}: {', '.join(deps[:10])}")
                    if len(deps) > 10:
                        print(f"    ... and {len(deps) - 10} more")
                    total += len(deps)
            print(f"  Total downstream: {total} symbols reachable")

    elif args.command == "deps":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        radius = dependency_closure(args.name, callees_graph, max_depth=args.max_depth)
        if args.json:
            print(
                format_json(
                    {
                        "symbol": args.name,
                        "deps": {k: sorted(v) for k, v in radius.items()},
                    }
                )
            )
        else:
            print(f"Dependency closure of {args.name}():")
            total = 0
            for depth_key in sorted(radius.keys()):
                deps = sorted(radius[depth_key])
                if deps:
                    print(f"  {depth_key}: {', '.join(deps[:10])}")
                    if len(deps) > 10:
                        print(f"    ... and {len(deps) - 10} more")
                    total += len(deps)
            print(f"  Total dependencies: {total} symbols reachable")

    elif args.command == "impact":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        radius = impact_radius(args.name, callers_graph, max_depth=args.max_depth)
        if args.json:
            print(
                format_json(
                    {
                        "symbol": args.name,
                        "impact_radius": {k: sorted(v) for k, v in radius.items()},
                    }
                )
            )
        else:
            print(f"Impact radius of {args.name}():")
            total = 0
            for depth_key in sorted(radius.keys()):
                affected = sorted(radius[depth_key])
                if affected:
                    print(f"  {depth_key}: {', '.join(affected[:10])}")
                    if len(affected) > 10:
                        print(f"    ... and {len(affected) - 10} more")
                    total += len(affected)
            print(f"  Total affected: {total} symbols that could break")

    elif args.command == "def":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        if args.json:
            if (
                index
                and index.has(args.name)
                and (not found or found.file != str(filepath))
            ):
                all_defs = [
                    {
                        "name": args.name,
                        "kind": e.kind,
                        "file": e.file,
                        "start_line": e.start_line,
                        "end_line": e.end_line,
                        "parent_class": e.parent_class,
                    }
                    for e in index.lookup(args.name)
                ]
                print(format_json(all_defs if len(all_defs) > 1 else all_defs[0]))
            else:
                print(format_json(asdict(found)))
        else:
            qual = f" (in class {found.parent_class})" if found.parent_class else ""
            print(
                f"{found.kind} {found.name}{qual}: "
                f"lines {found.start_line}-{found.end_line}"
            )
            if found.docstring:
                print(f"  {found.docstring[:200]}")
            if index and index.has(args.name):
                entries = [e for e in index.lookup(args.name) if e.file != found.file]
                if entries:
                    print(f"\n  Also defined in {len(entries)} other file(s):")
                    for e in entries[:10]:
                        print(f"    {e.file}:L{e.start_line}")

    elif args.command == "refs":
        found = resolve(args.name)
        if not found:
            sys.exit(f"Symbol not found: {args.name}")
        if index is not None and index.has(args.name):
            entries = index.lookup(args.name)
            cross_file_info = f"\n  Defined in {len(entries)} file(s):" + "".join(
                f"\n    {e.file}:L{e.start_line}" for e in entries[:10]
            )
        else:
            cross_file_info = ""
        resolved_key = _resolve_graph_key(args.name, callers_graph)
        callers = sorted(callers_graph.get(resolved_key, set())) if resolved_key else []
        if args.json:
            output = {
                "symbol": args.name,
                "references": callers,
            }
            if index and index.has(args.name):
                output["definitions"] = [
                    {"file": e.file, "line": e.start_line}
                    for e in index.lookup(args.name)
                ]
            print(format_json(output))
        else:
            print(f"References to {args.name}():{cross_file_info}")
            if callers:
                print(f"  {len(callers)} call site(s):")
                for c in callers:
                    s = resolve(c)
                    if s:
                        print(f"    {c}()  line {s.start_line}")
            else:
                print("  No callers found")


if __name__ == "__main__":
    main()
