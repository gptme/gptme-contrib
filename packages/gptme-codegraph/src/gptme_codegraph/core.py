"""gptme-codegraph — structural code retrieval via tree-sitter.

Complementary to gptme-rag (text chunks), this retrieves code *structure*:
function/class definitions, call graphs, and blast radius.

Supports Python, TypeScript/JavaScript, Rust, Go, Java, C#, Ruby, C, C++, PHP,
and Kotlin via tree-sitter grammars.
To add a new language: add grammar dep to pyproject.toml, extend _LANG_MAP
and _load_language, then add extractor functions.

Usage:
    gptme-codegraph <file> parse           # Extract symbols
    gptme-codegraph <file> callers <name>  # Who calls X?
    gptme-codegraph <file> callees <name>  # What does X call?
    python3 scripts/codegraph.py <file> deps <name>     # Dependency closure
    python3 scripts/codegraph.py <file> impact <name>   # Impact radius
    python3 scripts/codegraph.py <file> def <name>      # Where is X defined?
    python3 scripts/codegraph.py <file> refs <name>     # Where is X referenced?
    gptme-codegraph <repo-or-file> map                  # Token-cheap repo skeleton
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

# ---------------------------------------------------------------------------
# Language mapping
# ---------------------------------------------------------------------------

_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
}

_GRAMMAR_MODULES: dict[str, str] = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "jsx": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "tsx": "tree_sitter_typescript",
    "rust": "tree_sitter_rust",
    "go": "tree_sitter_go",
    "java": "tree_sitter_java",
    "csharp": "tree_sitter_c_sharp",
    "cpp": "tree_sitter_cpp",
    "ruby": "tree_sitter_ruby",
    "php": "tree_sitter_php",
    "kotlin": "tree_sitter_kotlin",
    "c": "tree_sitter_c",
}

_SOURCE_EXTENSIONS: tuple[str, ...] = tuple(_LANG_MAP.keys())
_NOISE_PATH_PARTS: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", ".git", "target", "vendor"}
)


def _detect_language(filepath: Path) -> str | None:
    """Detect the tree-sitter language name from a file extension."""
    suffix = filepath.suffix
    if suffix in _LANG_MAP:
        return _LANG_MAP[suffix]
    # Try double extension (.d.ts → .ts)
    if len(filepath.suffixes) >= 2:
        double = filepath.suffixes[-2] + suffix
        if double in _LANG_MAP:
            return _LANG_MAP[double]
    return None


def _is_noise_path(filepath: Path, directory: Path) -> bool:
    """Return True when ``filepath`` lives under a junk/generated path."""
    try:
        parts = filepath.relative_to(directory).parts
    except ValueError:
        parts = filepath.parts
    return any(part.startswith(".") or part in _NOISE_PATH_PARTS for part in parts)


def _load_language(name: str):
    """Load a tree-sitter Language for the given name.

    Returns None if the grammar is not installed.
    """
    from tree_sitter import Language  # type: ignore[import-untyped,unused-ignore]

    # Lazy import each grammar; missing grammars silently return None
    grammar_map: dict[str, object] = {}
    try:
        import tree_sitter_python as tspython  # type: ignore[import-not-found,unused-ignore]

        grammar_map["python"] = tspython.language()
    except ImportError:
        pass
    try:
        import tree_sitter_typescript as tstypescript  # type: ignore[import-not-found,unused-ignore]

        grammar_map["typescript"] = tstypescript.language_typescript()
        grammar_map["tsx"] = tstypescript.language_tsx()
    except ImportError:
        pass
    try:
        import tree_sitter_javascript as tsjavascript  # type: ignore[import-not-found,unused-ignore]

        grammar_map["javascript"] = tsjavascript.language()
    except ImportError:
        pass
    try:
        import tree_sitter_rust as tsrust  # type: ignore[import-not-found,unused-ignore]

        grammar_map["rust"] = tsrust.language()
    except ImportError:
        pass
    try:
        import tree_sitter_go as tsgo  # type: ignore[import-not-found,unused-ignore]

        grammar_map["go"] = tsgo.language()
    except ImportError:
        pass
    try:
        import tree_sitter_java as tsjava  # type: ignore[import-not-found,unused-ignore]

        grammar_map["java"] = tsjava.language()
    except ImportError:
        pass
    try:
        import tree_sitter_c_sharp as tscsharp  # type: ignore[import-not-found,unused-ignore]

        grammar_map["csharp"] = tscsharp.language()
    except ImportError:
        pass
    try:
        import tree_sitter_cpp as tscpp  # type: ignore[import-not-found,unused-ignore]

        grammar_map["cpp"] = tscpp.language()
    except ImportError:
        pass
    try:
        import tree_sitter_ruby as tsruby  # type: ignore[import-not-found,unused-ignore]

        grammar_map["ruby"] = tsruby.language()
    except ImportError:
        pass
    try:
        import tree_sitter_c as tsc  # type: ignore[import-not-found,unused-ignore]

        grammar_map["c"] = tsc.language()
    except ImportError:
        pass
    try:
        import tree_sitter_php as tsphp  # type: ignore[import-not-found,unused-ignore]

        grammar_map["php"] = tsphp.language_php()
    except ImportError:
        pass
    try:
        import tree_sitter_kotlin as tskotlin  # type: ignore[import-not-found,unused-ignore]

        grammar_map["kotlin"] = tskotlin.language()
    except ImportError:
        pass

    lang = grammar_map.get(name)
    if lang is None:
        # Fallback: tsx → typescript
        if name == "tsx":
            lang = grammar_map.get("typescript")
        elif name == "jsx":
            lang = grammar_map.get("javascript")
    if lang is None:
        return None
    return Language(lang)


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
    """A single import in a source file.

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


def _iter_source_files(directory: Path) -> list[Path]:
    """Return source files under ``directory``, skipping junk dirs.

    Uses ``git ls-files`` when the directory is a git repository, falling
    back to ``rglob`` for non-git directories or when git is unavailable.
    Supports Python, TypeScript/JavaScript, and Rust.
    """
    # When directory is a git repo, use git ls-files — faster and avoids
    # vendored deps (node_modules, .venv, etc.) and generated files.
    git_dir = directory / ".git"
    if git_dir.exists() and git_dir.is_dir():
        try:
            all_files: set[Path] = set()
            for pattern in (f"*{ext}" for ext in _SOURCE_EXTENSIONS):
                result = subprocess.run(
                    ["git", "ls-files", pattern],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    all_files.update(
                        directory / p for p in result.stdout.strip().split("\n") if p
                    )
            if all_files:
                return sorted(
                    fp for fp in all_files if not _is_noise_path(fp, directory)
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    files: list[Path] = []
    for fp in sorted(directory.rglob("*")):
        if not fp.suffix:
            continue
        if _detect_language(fp) is None:
            continue
        if _is_noise_path(fp, directory):
            continue
        files.append(fp)
    return files


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
        - No new supported source files exist in the directory
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

        # Check that no new supported source files appeared
        for fp in _iter_source_files(dir_path):
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

        # Also track all supported source files to detect new files on freshness check
        dir_path = Path(self._directory)
        for fp in _iter_source_files(dir_path):
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
    """Result of parsing a single source file."""

    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    language: str | None = None
    diagnostic: dict[str, str] | None = None


@dataclass
class _ParseAttempt:
    """Internal parse result that preserves failure diagnostics."""

    root: object | None = None
    parser: object | None = None
    diagnostic: dict[str, str] | None = None


def _missing_grammar_diagnostic(lang_name: str) -> dict[str, str]:
    """Describe how to fix a missing tree-sitter grammar."""
    module_name = _GRAMMAR_MODULES.get(lang_name, "tree_sitter_<unknown>")
    package_name = module_name.replace("_", "-")
    return {
        "code": "missing-grammar",
        "language": lang_name,
        "message": (
            f"Missing tree-sitter grammar for {lang_name} ({module_name}). "
            f"Install gptme-codegraph[treesitter] or add {package_name} to the environment."
        ),
    }


def _tree_sitter_parse_attempt(code: bytes, lang_name: str = "python") -> _ParseAttempt:
    """Parse source code with tree-sitter and preserve missing-dependency diagnostics."""
    try:
        from tree_sitter import Parser  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        return _ParseAttempt(
            diagnostic={
                "code": "missing-tree-sitter",
                "language": lang_name,
                "message": (
                    "Missing tree-sitter runtime. Install gptme-codegraph[treesitter] "
                    "or add tree-sitter plus the relevant language grammar to the environment."
                ),
            }
        )

    parser = Parser()
    language = _load_language(lang_name)
    if language is None:
        diagnostic = None
        if lang_name in _GRAMMAR_MODULES:
            diagnostic = _missing_grammar_diagnostic(lang_name)
        return _ParseAttempt(diagnostic=diagnostic)

    parser.language = language
    tree = parser.parse(code)
    if tree is None:
        return _ParseAttempt(
            diagnostic={
                "code": "parse-failed",
                "language": lang_name,
                "message": f"tree-sitter failed to parse {lang_name} source.",
            }
        )
    return _ParseAttempt(root=tree.root_node, parser=parser)


def _tree_sitter_parse(code: bytes, lang_name: str = "python"):
    """Parse source code with tree-sitter and return the root node.

    Falls back gracefully if tree-sitter or the grammar is unavailable.
    Returns (root_node, parser) on success.
    """
    attempt = _tree_sitter_parse_attempt(code, lang_name)
    return attempt.root, attempt.parser


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
            if cursor.node.type in {"call", "call_expression", "invocation_expression"}:
                # Python "call", JS/TS/Rust/Go "call_expression", C#
                # "invocation_expression" all expose a "function" field.
                func_node = cursor.node.child_by_field_name("function")
                if func_node:
                    calls.append(_text(func_node))
                elif cursor.node.type == "call_expression" and any(
                    child.type == "value_arguments"
                    for child in cursor.node.named_children
                ):
                    # Kotlin call_expression: no named "function" field; the
                    # callee is the first named child that is not value_arguments.
                    # Guarded by value_arguments presence (Kotlin-specific type)
                    # so JS/TS/Go/Rust call_expression edge cases fall through
                    # to the Ruby else branch instead of applying this path.
                    callee = next(
                        (
                            child
                            for child in cursor.node.named_children
                            if child.type != "value_arguments"
                        ),
                        None,
                    )
                    if callee:
                        calls.append(_text(callee))
                else:
                    # Ruby: call nodes carry the callee in a "method" field
                    method_node = cursor.node.child_by_field_name("method")
                    if method_node:
                        calls.append(_text(method_node))
            elif cursor.node.type == "method_invocation":
                # Java: method calls use method_invocation with a "name" field
                name_node = cursor.node.child_by_field_name("name")
                if name_node:
                    calls.append(_text(name_node))
            elif cursor.node.type == "function_call_expression":
                # PHP: callee is the "function" field (not "name")
                name_node = cursor.node.child_by_field_name("function")
                if name_node:
                    calls.append(_text(name_node))
            elif cursor.node.type == "member_call_expression":
                # PHP: $obj->method() — object is variable_name, method is the "name" field
                name_node = cursor.node.child_by_field_name("name")
                if name_node:
                    calls.append(_text(name_node))
            elif cursor.node.type == "scoped_call_expression":
                # PHP: Class::method() — method name is the second "name" child
                names = [c for c in cursor.node.named_children if c.type == "name"]
                if names:
                    calls.append(_text(names[-1]))
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


def _strip_string_quotes(text: str) -> str:
    """Return a string literal without its outer quote characters."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _strip_include_delimiters(text: str) -> str:
    """Return an include path without ``""`` or ``<>`` delimiters."""
    if len(text) >= 2 and (
        (text[0] == text[-1] and text[0] in {'"', "'"})
        or (text[0] == "<" and text[-1] == ">")
    ):
        return text[1:-1]
    return text


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


def _extract_imports_javascript(root) -> list[ImportInfo]:
    """Extract import statements from JavaScript/TypeScript code.

    Phase 1 keeps the mapping intentionally simple:
    - named imports preserve original names and aliases
    - namespace imports resolve dotted calls like ``util.foo()``
    - default imports map the local binding directly as a best-effort guess
    """
    imports: list[ImportInfo] = []
    try:
        for node in root.named_children:
            if node.type != "import_statement":
                continue

            source_node = next(
                (child for child in node.named_children if child.type == "string"),
                None,
            )
            clause_node = next(
                (
                    child
                    for child in node.named_children
                    if child.type == "import_clause"
                ),
                None,
            )
            if source_node is None or clause_node is None:
                continue

            module = _strip_string_quotes(_text(source_node))
            for child in clause_node.named_children:
                if child.type == "identifier":
                    local_name = _text(child)
                    imports.append(
                        ImportInfo(
                            file="",
                            name=local_name,
                            module=module,
                            is_from=True,
                        )
                    )
                elif child.type == "named_imports":
                    for specifier in child.named_children:
                        if specifier.type != "import_specifier":
                            continue
                        name_node = specifier.child_by_field_name("name")
                        alias_node = specifier.child_by_field_name("alias")
                        name = _text(name_node) if name_node else ""
                        alias = _text(alias_node) if alias_node else None
                        imports.append(
                            ImportInfo(
                                file="",
                                name=name,
                                module=module,
                                is_from=True,
                                alias=alias,
                            )
                        )
                elif child.type == "namespace_import":
                    alias = next(
                        (
                            _text(grandchild)
                            for grandchild in child.named_children
                            if grandchild.type == "identifier"
                        ),
                        None,
                    )
                    imports.append(
                        ImportInfo(
                            file="",
                            name=module.split("/")[-1] or module,
                            module=module,
                            is_from=False,
                            alias=alias,
                        )
                    )
    except Exception:
        pass
    return imports


def _extract_imports_rust(root) -> list[ImportInfo]:
    """Extract `use` statements from a Rust parse tree.

    Handles:
    - use std::collections::HashMap;
    - use crate::utils;
    - use super::*;
    - use foo::bar as baz;
    - use std::io::{self, BufRead};
    """
    imports: list[ImportInfo] = []

    def _text_rs(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    def _scoped_ident_text(node) -> str:
        """Reconstruct the full path from a scoped_identifier node."""
        if node.type in ("identifier", "crate", "self", "super"):
            return _text_rs(node)
        parts = []
        for child in node.named_children:
            if child.type in ("identifier", "crate", "self", "super"):
                parts.append(_text_rs(child))
            elif child.type == "scoped_identifier":
                parts.append(_scoped_ident_text(child))
            elif child.type == "scoped_type_identifier":
                parts.append(_scoped_ident_text(child))
        return "::".join(parts)

    def _extract_from_use_decl(node):
        """Walk a use_declaration node and yield (name, module, alias) tuples."""
        for child in node.named_children:
            if child.type == "scoped_identifier":
                # use std::collections::HashMap;  → name=HashMap, module=std::collections
                # use crate::utils;               → name=utils,   module=crate
                full_path = _scoped_ident_text(child)
                name = full_path.split("::")[-1]
                module = "::".join(full_path.split("::")[:-1]) or None
                yield name, module, None, True
            elif child.type == "use_as_clause":
                # use foo::bar as baz;  → name=bar, module=foo, alias=baz
                # use serde as s;       → name=serde, module=None, alias=s
                path_node = child.child_by_field_name("path")
                alias_node = child.child_by_field_name("alias")
                alias = _text_rs(alias_node) if alias_node else None
                if path_node:
                    full_path = _scoped_ident_text(path_node)
                    name = full_path.split("::")[-1]
                    module = "::".join(full_path.split("::")[:-1]) or None
                    yield name, module, alias, True
            elif child.type == "use_wildcard":
                # use super::*;  → name=*, module=super
                path_part = None
                for sub in child.named_children:
                    if sub.type in ("super", "crate", "self"):
                        path_part = _text_rs(sub)
                    elif sub.type == "scoped_identifier":
                        path_part = _scoped_ident_text(sub)
                    elif sub.type == "identifier":
                        path_part = _text_rs(sub)
                yield "*", path_part, None, True
            elif child.type == "scoped_use_list":
                # use std::io::{self, BufRead};  → module=std::io, names={self, BufRead}
                # First child of scoped_use_list is the scoped_identifier for module
                module_node = child.named_children[0] if child.named_children else None
                module_path = _scoped_ident_text(module_node) if module_node else None
                # Walk the use_list child
                for sub in child.named_children:
                    if sub.type == "use_list":
                        for item in sub.named_children:
                            if item.type == "identifier":
                                # Simple name: std::io::BufRead
                                name = _text_rs(item)
                                yield name, module_path, None, True
                            elif item.type == "self":
                                # self: std::io::{self, ...} → name=self, module=std::io
                                yield "self", module_path, None, True
                            elif item.type == "scoped_identifier":
                                full_path = _scoped_ident_text(item)
                                name = full_path.split("::")[-1]
                                yield name, module_path, None, True

    try:
        for node in root.named_children:
            if node.type == "use_declaration":
                for name, module, alias, is_from in _extract_from_use_decl(node):
                    imports.append(
                        ImportInfo(
                            file="",
                            name=name,
                            module=module,
                            is_from=is_from,
                            alias=alias,
                        )
                    )
    except Exception:
        pass
    return imports


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------


def _unwrap_python_definition(node):
    """Return the underlying Python definition for decorated nodes."""
    if node.type != "decorated_definition":
        return node

    for child in node.named_children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return None


def _extract_symbols_python(root, filepath: str) -> list[Symbol]:
    """Extract functions, classes, and methods from a Python parse tree."""
    symbols: list[Symbol] = []

    def walk(node, parent_class: str | None = None):
        unwrapped = _unwrap_python_definition(node)
        if unwrapped is None:
            return

        # Use the outer (decorated_definition) node for start_line so
        # decorated symbols report their full extent (first decorator),
        # not just the def/class keyword line.
        start_node = node if node.type == "decorated_definition" else unwrapped

        if unwrapped.type == "function_definition":
            name_node = unwrapped.child_by_field_name("name")
            body_node = unwrapped.child_by_field_name("body")
            if name_node:
                name = _text(name_node)
                calls = _extract_calls(body_node) if body_node else []
                docstring = _extract_docstring(body_node) if body_node else None
                kind = "method" if parent_class else "function"
                symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file=filepath,
                        start_line=start_node.start_point[0] + 1,
                        end_line=unwrapped.end_point[0] + 1,
                        parent_class=parent_class,
                        docstring=docstring,
                        calls=list(set(calls)),
                    )
                )
            return

        if unwrapped.type == "class_definition":
            name_node = unwrapped.child_by_field_name("name")
            body_node = unwrapped.child_by_field_name("body")
            if name_node:
                name = _text(name_node)
                docstring = _extract_docstring(body_node) if body_node else None
                symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        file=filepath,
                        start_line=start_node.start_point[0] + 1,
                        end_line=unwrapped.end_point[0] + 1,
                        docstring=docstring,
                    )
                )
                if body_node:
                    for child in body_node.named_children:
                        walk(child, parent_class=name)
            return

    for child in root.named_children:
        walk(child)

    return symbols


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ---------------------------------------------------------------------------


def _extract_symbols_typescript(root, filepath: str) -> list[Symbol]:
    """Extract functions, classes, and methods from a TS/JS parse tree."""
    symbols: list[Symbol] = []

    def _text_ts(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    def walk(node, parent_class: str | None = None):
        if node.type in ("function_declaration", "method_definition"):
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node:
                name = _text_ts(name_node)
                kind = "method" if parent_class else "function"
                calls = _extract_calls(body_node) if body_node else []
                symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_class,
                        calls=list(set(calls)),
                    )
                )
            return
        elif node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text_ts(name_node)
                symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.named_children:
                        walk(child, parent_class=name)
            return
        elif node.type in ("lexical_declaration", "variable_declaration"):
            # Handle arrow functions assigned to const/let/var:
            # const foo = (x) => { ... }
            for child in node.named_children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    value_node = child.child_by_field_name("value")
                    if (
                        name_node
                        and value_node
                        and value_node.type
                        in (
                            "arrow_function",
                            "function_expression",
                        )
                    ):
                        name = _text_ts(name_node)
                        body_node = value_node.child_by_field_name("body")
                        calls = _extract_calls(body_node) if body_node else []
                        symbols.append(
                            Symbol(
                                name=name,
                                kind="function",
                                file=filepath,
                                start_line=child.start_point[0] + 1,
                                end_line=child.end_point[0] + 1,
                                calls=list(set(calls)),
                            )
                        )
            return
        elif node.type == "export_statement":
            # export function / class / const
            for child in node.named_children:
                walk(child, parent_class)
            return

        # Recurse into children
        for child in node.named_children:
            walk(child, parent_class)

    walk(root)
    return symbols


# ---------------------------------------------------------------------------
# Rust extraction
# ---------------------------------------------------------------------------


def _extract_symbols_rust(root, filepath: str) -> list[Symbol]:
    """Extract functions, structs, enums, traits, and impl blocks from a Rust parse tree."""
    symbols: list[Symbol] = []

    def _text_rs(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    def walk(node, parent_scope: str | None = None, parent_kind: str | None = None):
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node:
                name = _text_rs(name_node)
                # Determine kind: methods are inside impl_item
                kind = "method" if parent_kind == "impl" else "function"
                parent_class = parent_scope if kind == "method" else None
                calls = _extract_calls(body_node) if body_node else []
                symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_class,
                        calls=list(set(calls)),
                    )
                )
            return
        elif node.type == "function_signature_item" and parent_kind == "trait":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(
                    Symbol(
                        name=_text_rs(name_node),
                        kind="method",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_scope,
                    )
                )
            return
        elif node.type == "struct_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(
                    Symbol(
                        name=_text_rs(name_node),
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
            return
        elif node.type == "enum_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(
                    Symbol(
                        name=_text_rs(name_node),
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
            return
        elif node.type == "trait_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                trait_name = _text_rs(name_node)
                symbols.append(
                    Symbol(
                        name=trait_name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
                for child in node.named_children:
                    walk(child, parent_scope=trait_name, parent_kind="trait")
            return
        elif node.type == "impl_item":
            # impl Foo { fn method() ... } → methods get parent_class="Foo"
            type_node = node.child_by_field_name("type")
            scope_name = None
            if type_node:
                scope_name = _text_rs(type_node)
            for child in node.named_children:
                walk(child, parent_scope=scope_name, parent_kind="impl")
            return

        for child in node.named_children:
            walk(child, parent_scope, parent_kind)

    walk(root)
    return symbols


# ---------------------------------------------------------------------------
# Go extraction
# ---------------------------------------------------------------------------


def _receiver_type_go(receiver_node) -> str | None:
    """Extract the base type name from a Go receiver parameter_list.

    Handles plain, pointer, and generic receivers (Go 1.18+):
      (r Foo)       → "Foo"
      (r *Foo)      → "Foo"
      (r Foo[T])    → "Foo"
      (r *Foo[T])   → "Foo"
    """

    def _base_type(type_node) -> str | None:
        if type_node.type == "type_identifier":
            return _text(type_node)
        if type_node.type == "pointer_type":
            for sub in type_node.named_children:
                result = _base_type(sub)
                if result:
                    return result
        if type_node.type == "generic_type":
            base = type_node.child_by_field_name("type")
            if base is not None:
                return _base_type(base)
        return None

    for child in receiver_node.named_children:
        if child.type == "parameter_declaration":
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            result = _base_type(type_node)
            if result:
                return result
    return None


def _extract_symbols_go(root, filepath: str) -> list[Symbol]:
    """Extract functions, methods, and named types from a Go parse tree."""
    symbols: list[Symbol] = []

    for node in root.named_children:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                body_node = node.child_by_field_name("body")
                calls = _extract_calls(body_node) if body_node else []
                symbols.append(
                    Symbol(
                        name=_text(name_node),
                        kind="function",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        calls=list(set(calls)),
                    )
                )
        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            receiver_node = node.child_by_field_name("receiver")
            if name_node:
                parent_class = (
                    _receiver_type_go(receiver_node) if receiver_node else None
                )
                body_node = node.child_by_field_name("body")
                calls = _extract_calls(body_node) if body_node else []
                symbols.append(
                    Symbol(
                        name=_text(name_node),
                        kind="method",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_class,
                        calls=list(set(calls)),
                    )
                )
        elif node.type == "type_declaration":
            for type_spec in node.named_children:
                if type_spec.type == "type_spec":
                    name_node = type_spec.child_by_field_name("name")
                    type_node = type_spec.child_by_field_name("type")
                    if (
                        name_node
                        and type_node
                        and type_node.type
                        in (
                            "struct_type",
                            "interface_type",
                        )
                    ):
                        symbols.append(
                            Symbol(
                                name=_text(name_node),
                                kind="class",
                                file=filepath,
                                start_line=type_spec.start_point[0] + 1,
                                end_line=type_spec.end_point[0] + 1,
                            )
                        )

    return symbols


def _extract_imports_go(root) -> list[ImportInfo]:
    """Extract import statements from a Go parse tree.

    Handles:
    - import "fmt"                  → name="fmt", module="fmt"
    - import m "math"               → name="math", alias="m"
    - import . "os"                 → name="os", is_from=True
    - import _ "unused"             → name="unused", alias="_"
    - import "github.com/foo/bar"   → name="bar", module="github.com/foo/bar"
    """
    imports: list[ImportInfo] = []

    def _parse_import_spec(spec_node) -> None:
        path_node = spec_node.child_by_field_name("path")
        if path_node is None:
            return
        path_text = _strip_string_quotes(_text(path_node))
        default_name = path_text.split("/")[-1]

        alias = None
        is_from = False
        name_node = spec_node.child_by_field_name("name")
        if name_node is not None:
            alias_text = _text(name_node)
            if alias_text == ".":
                is_from = True
            elif alias_text == "_":
                alias = "_"
            else:
                alias = alias_text

        imports.append(
            ImportInfo(
                file="",
                name=default_name,
                module=path_text,
                is_from=is_from,
                alias=alias,
            )
        )

    try:
        for node in root.named_children:
            if node.type == "import_declaration":
                for child in node.named_children:
                    if child.type == "import_spec_list":
                        for spec in child.named_children:
                            if spec.type == "import_spec":
                                _parse_import_spec(spec)
                    elif child.type == "import_spec":
                        _parse_import_spec(child)
    except Exception:
        pass
    return imports


# ---------------------------------------------------------------------------
# Java extraction
# ---------------------------------------------------------------------------


def _extract_symbols_java(root, filepath: str) -> list[Symbol]:
    """Extract classes, interfaces, enums, and methods from a Java parse tree."""
    symbols: list[Symbol] = []

    def _text(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    def _extract_members(body_node, parent_class: str) -> None:
        for member in body_node.named_children:
            if member.type == "method_declaration":
                name_node = member.child_by_field_name("name")
                if name_node:
                    body = member.child_by_field_name("body")
                    calls = _extract_calls(body) if body else []
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="method",
                            file=filepath,
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            parent_class=parent_class,
                            calls=list(set(calls)),
                        )
                    )
            elif member.type == "constructor_declaration":
                name_node = member.child_by_field_name("name")
                if name_node:
                    body = member.child_by_field_name("body")
                    calls = _extract_calls(body) if body else []
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="method",
                            file=filepath,
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            parent_class=parent_class,
                            calls=list(set(calls)),
                        )
                    )
            elif member.type in (
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
            ):
                _extract_type_decl(member)

    def _extract_type_decl(node) -> None:
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = _text(name_node)
                symbols.append(
                    Symbol(
                        name=class_name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    _extract_members(body, parent_class=class_name)
        elif node.type == "interface_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                iface_name = _text(name_node)
                symbols.append(
                    Symbol(
                        name=iface_name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    for member in body.named_children:
                        if member.type == "method_declaration":
                            mn = member.child_by_field_name("name")
                            if mn:
                                symbols.append(
                                    Symbol(
                                        name=_text(mn),
                                        kind="method",
                                        file=filepath,
                                        start_line=member.start_point[0] + 1,
                                        end_line=member.end_point[0] + 1,
                                        parent_class=iface_name,
                                    )
                                )
        elif node.type == "enum_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(
                    Symbol(
                        name=_text(name_node),
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )

    for child in root.named_children:
        _extract_type_decl(child)

    return symbols


def _extract_imports_java(root) -> list[ImportInfo]:
    """Extract import declarations from a Java parse tree."""
    imports: list[ImportInfo] = []

    def _text(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    for child in root.named_children:
        if child.type == "import_declaration":
            is_wildcard = any(c.type == "asterisk" for c in child.children)
            qualified = next(
                (
                    _text(c)
                    for c in child.named_children
                    if c.type == "scoped_identifier"
                ),
                None,
            ) or next(
                (_text(c) for c in child.named_children if c.type == "identifier"),
                None,
            )
            if not qualified:
                continue
            if is_wildcard:
                module, name = qualified, "*"
            elif "." in qualified:
                dot = qualified.rfind(".")
                module, name = qualified[:dot], qualified[dot + 1 :]
            else:
                module, name = "", qualified
            imports.append(
                ImportInfo(file="", module=module, name=name, alias=None, is_from=True)
            )

    return imports


def _extract_symbols_ruby(root, filepath: str) -> list[Symbol]:
    """Extract modules, classes, and methods from a Ruby parse tree.

    Ruby modules and classes both map to ``kind="class"`` (namespacing
    containers); ``def`` and ``def self.`` map to ``kind="method"`` when nested
    in a class/module and ``kind="function"`` at the top level.
    """
    symbols: list[Symbol] = []

    def _text(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    def _walk(node, parent: str | None) -> None:
        for child in node.named_children:
            if child.type in ("class", "module"):
                name_node = child.child_by_field_name("name")
                name = _text(name_node) if name_node else ""
                if name:
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="class",
                            file=filepath,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            parent_class=parent,
                        )
                    )
                body = child.child_by_field_name("body")
                if body is not None:
                    _walk(body, parent=name or parent)
            elif child.type in ("method", "singleton_method"):
                name_node = child.child_by_field_name("name")
                if name_node:
                    body = child.child_by_field_name("body")
                    calls = _extract_calls(body) if body is not None else []
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="method" if parent else "function",
                            file=filepath,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            parent_class=parent,
                            calls=list(set(calls)),
                        )
                    )
            elif child.type == "body_statement":
                _walk(child, parent)

    _walk(root, parent=None)
    return symbols


def _extract_imports_ruby(root) -> list[ImportInfo]:
    """Extract ``require`` / ``require_relative`` calls from a Ruby parse tree.

    Ruby has no static import syntax; dependencies are loaded at runtime via
    ``require``/``require_relative``/``load`` with a string path argument. The
    loaded path is recorded as the module, with name ``*`` (whole-file load).
    """
    imports: list[ImportInfo] = []

    def _text(node) -> str:
        try:
            return node.text.decode("utf-8") if node.text else ""
        except Exception:
            return ""

    require_methods = {"require", "require_relative", "load"}

    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call":
            method_node = node.child_by_field_name("method")
            receiver = node.child_by_field_name("receiver")
            if (
                method_node is not None
                and receiver is None
                and _text(method_node) in require_methods
            ):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for arg in args.named_children:
                        if arg.type == "string":
                            path = _strip_string_quotes(_text(arg))
                            if path:
                                imports.append(
                                    ImportInfo(
                                        file="",
                                        module=path,
                                        name="*",
                                        alias=None,
                                        is_from=False,
                                    )
                                )
        stack.extend(node.children)
    return imports


# ---------------------------------------------------------------------------
# C# extraction
# ---------------------------------------------------------------------------


_CSHARP_TYPE_NODES = frozenset(
    {
        "class_declaration",
        "struct_declaration",
        "record_declaration",
        "interface_declaration",
        "enum_declaration",
    }
)
_CSHARP_NAMESPACE_NODES = frozenset(
    {"namespace_declaration", "file_scoped_namespace_declaration"}
)


def _csharp_body(node):
    """Return a C# type's member-list node (the ``body`` field or a child
    ``declaration_list``/``enum_member_declaration_list``)."""
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.named_children:
        if child.type in ("declaration_list", "enum_member_declaration_list"):
            return child
    return None


def _extract_symbols_csharp(root, filepath: str) -> list[Symbol]:
    """Extract classes, structs, records, interfaces, enums, and their methods
    from a C# parse tree.

    C# declarations may be nested inside ``namespace`` blocks (block-scoped or
    file-scoped), so containers are walked recursively.
    """
    symbols: list[Symbol] = []

    def _members(body_node, parent_class: str) -> None:
        for member in body_node.named_children:
            if member.type in ("method_declaration", "constructor_declaration"):
                name_node = member.child_by_field_name("name")
                if name_node:
                    body = member.child_by_field_name("body")
                    calls = _extract_calls(body) if body else []
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="method",
                            file=filepath,
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            parent_class=parent_class,
                            calls=list(set(calls)),
                        )
                    )
            elif member.type in _CSHARP_TYPE_NODES:
                _type_decl(member)

    def _type_decl(node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        type_name = _text(name_node)
        symbols.append(
            Symbol(
                name=type_name,
                kind="class",
                file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        )
        body = _csharp_body(node)
        if body is not None:
            _members(body, parent_class=type_name)

    def _walk_container(node) -> None:
        for child in node.named_children:
            if child.type in _CSHARP_NAMESPACE_NODES:
                body = _csharp_body(child)
                _walk_container(body if body is not None else child)
            elif child.type in _CSHARP_TYPE_NODES:
                _type_decl(child)

    _walk_container(root)
    return symbols


def _extract_imports_csharp(root) -> list[ImportInfo]:
    """Extract ``using`` directives from a C# parse tree.

    Handles ``using System;``, ``using System.Collections.Generic;``,
    ``using static System.Math;``, and ``using Foo = System.Bar;`` (alias).
    """
    imports: list[ImportInfo] = []

    def _walk(node) -> None:
        for child in node.named_children:
            if child.type == "using_directive":
                # For `using Alias = Some.Namespace;` the "name" field holds the
                # alias identifier; plain/`static` usings have no "name" field and
                # carry the namespace as an unnamed qualified_name/identifier child.
                alias_node = child.child_by_field_name("name")
                alias = _text(alias_node) if alias_node is not None else None
                ns_node = next(
                    (c for c in child.named_children if c.type == "qualified_name"),
                    None,
                )
                if ns_node is None:
                    ns_node = next(
                        (
                            c
                            for c in child.named_children
                            if c.type == "identifier"
                            and (alias is None or _text(c) != alias)
                        ),
                        None,
                    )
                qualified = _text(ns_node) if ns_node is not None else ""
                if not qualified:
                    continue
                if "." in qualified:
                    dot = qualified.rfind(".")
                    module, name = qualified[:dot], qualified[dot + 1 :]
                else:
                    module, name = "", qualified
                imports.append(
                    ImportInfo(
                        file="",
                        module=module,
                        name=name,
                        alias=alias,
                        is_from=True,
                    )
                )
            elif child.type in _CSHARP_NAMESPACE_NODES:
                body = _csharp_body(child)
                _walk(body if body is not None else child)

    _walk(root)
    return imports


# ---------------------------------------------------------------------------
# C extraction helpers
# ---------------------------------------------------------------------------


def _c_find_function_name(declarator) -> str | None:
    """Walk a C declarator chain to find the function name identifier.

    Handles direct ``function_declarator``, ``pointer_declarator`` wrapping
    (e.g. ``int *foo(void)``), and ``parenthesized_declarator`` wrapping
    (e.g. ``int (*(foo))(void)``).
    """
    if declarator is None:
        return None
    if declarator.type == "identifier":
        return _text(declarator)
    if declarator.type == "function_declarator":
        # The function name is in the "declarator" child, which may be a
        # plain identifier ("int foo(void)") or a wrapping node such as
        # parenthesized_declarator ("int (*(foo))(void)").
        name_node = declarator.child_by_field_name("declarator")
        if name_node is not None:
            if name_node.type == "identifier":
                return _text(name_node)
            # Recurse into wrapping node (e.g. parenthesized_declarator)
            return _c_find_function_name(name_node)
    elif declarator.type == "pointer_declarator":
        inner = declarator.child_by_field_name("declarator")
        return _c_find_function_name(inner)
    elif declarator.type == "parenthesized_declarator":
        # Walk through parentheses to find the inner declarator chain.
        # The content inside the parens (e.g. identifier, pointer_declarator,
        # or another parenthesized_declarator) is in a child node.
        for child in declarator.children:
            if child.type in (
                "identifier",
                "pointer_declarator",
                "parenthesized_declarator",
            ):
                return _c_find_function_name(child)
    return None


def _extract_symbols_c(root, filepath: str) -> list[Symbol]:
    """Extract function definitions and typedef'd struct types from a C parse tree.

    Functions are extracted as ``kind="function"``.  Named ``struct`` definitions
    and ``typedef struct { ... } Name`` patterns are extracted as ``kind="class"``
    to stay consistent with how structs are represented across other languages.

    Walks recursively through preprocessor conditionals (``#ifdef``, ``#ifndef``,
    ``#if``, ``#elif``, ``#else``) so that functions and structs guarded by
    platform-specific or feature-test macros are not silently dropped.
    """
    symbols: list[Symbol] = []

    # Preprocessor conditional node types that may wrap symbols
    _PREPROC_WRAPPER = frozenset(
        {
            "preproc_ifdef",
            "preproc_ifndef",
            "preproc_if",
            "preproc_elif",
            "preproc_else",
        }
    )

    def _walk(node) -> None:
        for child in node.named_children:
            if child.type in _PREPROC_WRAPPER:
                _walk(child)
            elif child.type == "function_definition":
                outer_decl = child.child_by_field_name("declarator")
                name = _c_find_function_name(outer_decl)
                if not name:
                    continue
                body = child.child_by_field_name("body")
                calls = _extract_calls(body) if body else []
                symbols.append(
                    Symbol(
                        name=name,
                        kind="function",
                        file=filepath,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        calls=list(set(calls)),
                    )
                )
            elif child.type == "struct_specifier":
                # Named struct at top level: `struct Foo { ... };`
                name_node = child.child_by_field_name("name")
                if name_node:
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="class",
                            file=filepath,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                        )
                    )
            elif child.type == "type_definition":
                # typedef struct { ... } Name; or typedef struct Named Name;
                has_struct = any(
                    c.type == "struct_specifier" for c in child.named_children
                )
                if has_struct:
                    # The typedef name is the type_identifier child at the end
                    type_id = next(
                        (
                            c
                            for c in reversed(child.named_children)
                            if c.type == "type_identifier"
                        ),
                        None,
                    )
                    if type_id:
                        symbols.append(
                            Symbol(
                                name=_text(type_id),
                                kind="class",
                                file=filepath,
                                start_line=child.start_point[0] + 1,
                                end_line=child.end_point[0] + 1,
                            )
                        )

    _walk(root)
    return symbols


def _extract_imports_c(root) -> list[ImportInfo]:
    """Extract ``#include`` directives from a C parse tree.

    Handles both angle-bracket forms (``<stdio.h>``) and quoted forms
    (``"myheader.h"``).  The include path is split on ``/`` to populate
    ``module`` (the leading directory, e.g. ``sys``) and ``name`` (the
    filename, e.g. ``types.h``).

    Walks recursively through preprocessor conditionals (``#ifdef``, ``#ifndef``,
    ``#if``, ``#elif``, ``#else``) so that platform-specific or feature-guarded
    includes are not silently dropped.
    """
    imports: list[ImportInfo] = []

    # Preprocessor conditional node types that may wrap #include directives
    _PREPROC_WRAPPER = frozenset(
        {
            "preproc_ifdef",
            "preproc_ifndef",
            "preproc_if",
            "preproc_elif",
            "preproc_else",
        }
    )

    def _walk(node) -> None:
        for child in node.named_children:
            if child.type in _PREPROC_WRAPPER:
                _walk(child)
            elif child.type == "preproc_include":
                _extract_include(child)

    def _extract_include(node) -> None:
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        if path_node.type == "system_lib_string":
            raw = _text(path_node)
            include_path = raw.strip("<>")
        elif path_node.type == "string_literal":
            content = next(
                (c for c in path_node.named_children if c.type == "string_content"),
                None,
            )
            include_path = (
                _text(content) if content else _strip_string_quotes(_text(path_node))
            )
        else:
            include_path = _text(path_node).strip("<>\"'")

        if not include_path:
            return

        if "/" in include_path:
            slash = include_path.rfind("/")
            module, name = include_path[:slash], include_path[slash + 1 :]
        else:
            module, name = "", include_path

        imports.append(
            ImportInfo(
                file="",
                module=module,
                name=name,
                is_from=False,
            )
        )

    _walk(root)
    return imports


# ---------------------------------------------------------------------------
# PHP extraction
# ---------------------------------------------------------------------------


def _extract_symbols_php(root, filepath: str) -> list[Symbol]:
    """Extract classes, interfaces, traits, methods, and functions from a PHP parse tree."""
    symbols: list[Symbol] = []

    def _extract_method_body(body_node, parent_class: str) -> None:
        for child in body_node.named_children:
            if child.type == "method_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    body = child.child_by_field_name("body")
                    calls = _extract_calls(body) if body else []
                    symbols.append(
                        Symbol(
                            name=_text(name_node),
                            kind="method",
                            file=filepath,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            parent_class=parent_class,
                            calls=list(set(calls)),
                        )
                    )

    def _walk(node) -> None:
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                body = node.child_by_field_name("body")
                calls = _extract_calls(body) if body else []
                symbols.append(
                    Symbol(
                        name=_text(name_node),
                        kind="function",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        calls=list(set(calls)),
                    )
                )
            return

        if node.type in {
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
        }:
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = _text(name_node)
                symbols.append(
                    Symbol(
                        name=class_name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    _extract_method_body(body, parent_class=class_name)
            return

        for child in node.named_children:
            _walk(child)

    _walk(root)
    return symbols


def _split_php_import_name(qualified: str, prefix: str = "") -> tuple[str, str]:
    """Split a PHP import target into ``(module, name)`` pieces."""
    module = prefix
    name = qualified
    dot = qualified.rfind("\\")
    if dot >= 0:
        module = prefix + "\\" + qualified[:dot] if prefix else qualified[:dot]
        name = qualified[dot + 1 :]
    return module, name


def _extract_imports_php(root) -> list[ImportInfo]:
    """Extract ``use`` import declarations from a PHP parse tree.

    Handles:
    - use App\\Entity\\User;
    - use App\\Util\\Helper as H;
    - use function array_map;
    - use const PHP_EOL;
    - use App\\Entity\\{User, Group};   (group use declarations)
    """
    imports: list[ImportInfo] = []

    def _walk(node) -> None:
        if node.type == "namespace_use_declaration":
            clause = next(
                (c for c in node.named_children if c.type == "namespace_use_clause"),
                None,
            )
            if clause:
                is_fn_const = any(
                    child.type in {"function", "const"} for child in clause.children
                )
                qual = next(
                    (
                        child
                        for child in clause.named_children
                        if child.type in {"qualified_name", "name"}
                    ),
                    None,
                )
                if qual is not None:
                    qualified = _text(qual)
                    if qualified:
                        module, name = _split_php_import_name(qualified)
                        alias = clause.child_by_field_name("alias")
                        alias_text = _text(alias) if alias else None
                        imports.append(
                            ImportInfo(
                                file="",
                                module=module,
                                name=name,
                                alias=alias_text,
                                is_from=True,
                            )
                        )
                        if is_fn_const:
                            return
                if is_fn_const:
                    return

            # Group use: use App\Entity\{User, Group};
            group = next(
                (c for c in node.named_children if c.type == "namespace_use_group"),
                None,
            )
            if group:
                prefix_node = next(
                    (c for c in node.named_children if c.type == "namespace_name"),
                    None,
                )
                prefix = _text(prefix_node) if prefix_node else ""
                for clause in group.named_children:
                    if clause.type != "namespace_use_clause":
                        continue
                    qual = next(
                        (
                            child
                            for child in clause.named_children
                            if child.type in {"qualified_name", "name"}
                        ),
                        None,
                    )
                    if qual is None:
                        continue
                    qualified = _text(qual)
                    if not qualified:
                        continue
                    module, name = _split_php_import_name(qualified, prefix=prefix)
                    alias = clause.child_by_field_name("alias")
                    alias_text = _text(alias) if alias else None
                    imports.append(
                        ImportInfo(
                            file="",
                            module=module,
                            name=name,
                            alias=alias_text,
                            is_from=True,
                        )
                    )
            return

        for child in node.named_children:
            _walk(child)

    _walk(root)
    return imports


# ---------------------------------------------------------------------------
# Kotlin extraction
# ---------------------------------------------------------------------------


def _extract_symbols_kotlin(root, filepath: str) -> list[Symbol]:
    """Extract classes, objects, methods, and top-level functions from Kotlin."""
    symbols: list[Symbol] = []

    def _function_symbol(node, parent_class: str | None) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        body = next(
            (child for child in node.named_children if child.type == "function_body"),
            None,
        )
        # For expression-body functions (e.g. `fun greet() = println("hi")`)
        # tree-sitter-kotlin emits no function_body child; fall back to walking
        # the whole declaration so calls in the expression are still captured.
        calls = _extract_calls(body if body is not None else node)
        symbols.append(
            Symbol(
                name=_text(name_node),
                kind="method" if parent_class else "function",
                file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent_class=parent_class,
                calls=list(set(calls)),
            )
        )

    def _walk(node, parent_class: str | None = None) -> None:
        if node.type in {"class_declaration", "object_declaration"}:
            name_node = node.child_by_field_name("name")
            class_name = _text(name_node) if name_node else ""
            if class_name:
                symbols.append(
                    Symbol(
                        name=class_name,
                        kind="class",
                        file=filepath,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent_class=parent_class,
                    )
                )
            body = next(
                (child for child in node.named_children if child.type == "class_body"),
                None,
            )
            if body is not None:
                _walk(body, class_name or parent_class)
            return

        if node.type == "companion_object":
            body = next(
                (child for child in node.named_children if child.type == "class_body"),
                None,
            )
            if body is not None:
                _walk(body, parent_class)
            return

        if node.type == "function_declaration":
            _function_symbol(node, parent_class)
            return

        for child in node.named_children:
            _walk(child, parent_class)

    _walk(root)
    return symbols


def _split_kotlin_import(qualified: str, is_wildcard: bool) -> tuple[str, str]:
    """Split a Kotlin import into ``(module, name)`` pieces."""
    if is_wildcard:
        return qualified, "*"
    if "." not in qualified:
        return "", qualified
    module, name = qualified.rsplit(".", 1)
    return module, name


def _extract_imports_kotlin(root) -> list[ImportInfo]:
    """Extract Kotlin import declarations, including aliases and wildcards."""
    imports: list[ImportInfo] = []

    for node in root.named_children:
        if node.type != "import":
            continue
        qualified_node = next(
            (
                child
                for child in node.named_children
                if child.type == "qualified_identifier"
            ),
            None,
        )
        if qualified_node is None:
            continue
        qualified = _text(qualified_node)
        if not qualified:
            continue
        is_wildcard = any(child.type == "*" for child in node.children)
        module, name = _split_kotlin_import(qualified, is_wildcard)
        alias_node = next(
            (
                child
                for child in node.named_children
                if child.type == "identifier" and child is not qualified_node
            ),
            None,
        )
        imports.append(
            ImportInfo(
                file="",
                module=module,
                name=name,
                alias=_text(alias_node) if alias_node is not None else None,
                is_from=True,
            )
        )

    return imports


# C++ extraction
# ---------------------------------------------------------------------------


_CPP_TYPE_NODES = frozenset(
    {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}
)


def _cpp_container_body(node):
    """Return a C++ namespace/type body node when present."""
    for child in node.named_children:
        if child.type in (
            "declaration_list",
            "field_declaration_list",
            "enumerator_list",
        ):
            return child
    return None


def _cpp_type_name(node) -> str | None:
    """Return the declared C++ type name."""
    for child in node.named_children:
        if child.type in {
            "type_identifier",
            "identifier",
            "namespace_identifier",
        }:
            name = _text(child)
            if name:
                return name
    return None


def _cpp_qualified_parts(node) -> list[str]:
    """Return the named segments inside a C++ qualified identifier."""
    if node.type in {
        "identifier",
        "field_identifier",
        "type_identifier",
        "namespace_identifier",
        "destructor_name",
        "operator_name",
    }:
        text = _text(node)
        return [text] if text else []

    parts: list[str] = []
    for child in node.named_children:
        parts.extend(_cpp_qualified_parts(child))
    return parts


def _cpp_function_name_and_parent(
    declarator, known_types: set[str]
) -> tuple[str | None, str | None]:
    """Return ``(name, parent_class)`` for a C++ declarator subtree."""
    if declarator.type == "qualified_identifier":
        parts = _cpp_qualified_parts(declarator)
        if not parts:
            return None, None
        parent = parts[-2] if len(parts) >= 2 and parts[-2] in known_types else None
        return parts[-1], parent

    if declarator.type in {
        "identifier",
        "field_identifier",
        "destructor_name",
        "operator_name",
    }:
        text = _text(declarator)
        return (text, None) if text else (None, None)

    for child in declarator.named_children:
        if child.type == "parameter_list":
            continue
        name, parent = _cpp_function_name_and_parent(child, known_types)
        if name:
            return name, parent
    return None, None


def _cpp_function_body(node):
    """Return a function's compound statement node when present."""
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.named_children:
        if child.type == "compound_statement":
            return child
    return None


def _extract_symbols_cpp(root, filepath: str) -> list[Symbol]:
    """Extract classes, methods, and functions from a C++ parse tree.

    Scope is intentionally narrow: namespaces, top-level functions, class/struct/enum
    declarations, inline member definitions, and out-of-class member definitions that
    use a known declared type name.
    """
    symbols: list[Symbol] = []
    known_types: set[str] = set()

    def _add_function(node, parent_class: str | None = None) -> None:
        declarator = node.child_by_field_name("declarator")
        if declarator is None:
            declarator = next(
                (child for child in node.named_children if "declarator" in child.type),
                None,
            )
        if declarator is None:
            return

        name, inferred_parent = _cpp_function_name_and_parent(declarator, known_types)
        if not name:
            return

        effective_parent = parent_class or inferred_parent
        body = _cpp_function_body(node)
        calls = _extract_calls(body) if body else []
        symbols.append(
            Symbol(
                name=name,
                kind="method" if effective_parent else "function",
                file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent_class=effective_parent,
                calls=list(set(calls)),
            )
        )

    def _visit_type(node) -> None:
        type_name = _cpp_type_name(node)
        if not type_name:
            return

        known_types.add(type_name)
        symbols.append(
            Symbol(
                name=type_name,
                kind="class",
                file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        )

        body = _cpp_container_body(node)
        if body is None:
            return

        for child in body.named_children:
            if child.type == "function_definition":
                _add_function(child, parent_class=type_name)
            elif child.type in _CPP_TYPE_NODES:
                _visit_type(child)

    def _walk_container(node) -> None:
        for child in node.named_children:
            if child.type == "namespace_definition":
                body = _cpp_container_body(child)
                _walk_container(body if body is not None else child)
            elif child.type in _CPP_TYPE_NODES:
                _visit_type(child)
            elif child.type == "function_definition":
                _add_function(child)

    _walk_container(root)
    return symbols


def _extract_imports_cpp(root) -> list[ImportInfo]:
    """Extract ``#include`` directives from a C++ parse tree.

    Walks recursively through preprocessor conditionals (``#ifdef``, ``#ifndef``,
    ``#if``, ``#elif``, ``#else``) so that platform-specific or feature-guarded
    includes are not silently dropped.
    """
    imports: list[ImportInfo] = []

    _PREPROC_WRAPPER = frozenset(
        {
            "preproc_ifdef",
            "preproc_ifndef",
            "preproc_if",
            "preproc_elif",
            "preproc_else",
        }
    )

    def _extract_include(child) -> None:
        path_node = child.child_by_field_name("path")
        if path_node is None:
            path_node = next(
                (
                    node
                    for node in child.named_children
                    if node.type in {"string_literal", "system_lib_string"}
                ),
                None,
            )
        if path_node is None:
            return
        include_path = _strip_include_delimiters(_text(path_node))
        if not include_path:
            return
        parts = include_path.split("/")
        imports.append(
            ImportInfo(
                file="",
                module="/".join(parts[:-1]),
                name=parts[-1],
                alias=None,
                is_from=True,
            )
        )

    def _walk(node) -> None:
        for child in node.named_children:
            if child.type in _PREPROC_WRAPPER:
                _walk(child)
            elif child.type == "preproc_include":
                _extract_include(child)

    _walk(root)
    return imports


# ---------------------------------------------------------------------------
# Multi-language parse_file
# ---------------------------------------------------------------------------


def parse_file(filepath: Path) -> FileParseResult:
    """Extract all symbols and imports from a source file.

    Detects language from file extension and dispatches to the correct
    tree-sitter grammar. Supports Python, TypeScript/JavaScript, Rust, Go,
    Java, C#, Ruby, C, C++, PHP, and Kotlin.
    """
    code = filepath.read_bytes()
    lang_name = _detect_language(filepath)
    if lang_name is None:
        return FileParseResult()
    attempt = _tree_sitter_parse_attempt(code, lang_name)
    root = attempt.root
    if root is None:
        return FileParseResult(language=lang_name, diagnostic=attempt.diagnostic)

    filename = str(filepath)
    symbols: list[Symbol] = []

    if lang_name == "python":
        symbols = _extract_symbols_python(root, filename)

    elif lang_name in ("typescript", "javascript", "tsx"):
        symbols = _extract_symbols_typescript(root, filename)

    elif lang_name == "rust":
        symbols = _extract_symbols_rust(root, filename)

    elif lang_name == "go":
        symbols = _extract_symbols_go(root, filename)

    elif lang_name == "java":
        symbols = _extract_symbols_java(root, filename)

    elif lang_name == "csharp":
        symbols = _extract_symbols_csharp(root, filename)
    elif lang_name == "cpp":
        symbols = _extract_symbols_cpp(root, filename)

    elif lang_name == "ruby":
        symbols = _extract_symbols_ruby(root, filename)

    elif lang_name == "c":
        symbols = _extract_symbols_c(root, filename)

    elif lang_name == "php":
        symbols = _extract_symbols_php(root, filename)

    elif lang_name == "kotlin":
        symbols = _extract_symbols_kotlin(root, filename)

    imports: list[ImportInfo] = []
    if lang_name == "python":
        imports = _extract_imports(root)
    elif lang_name in ("typescript", "javascript", "tsx"):
        imports = _extract_imports_javascript(root)
    elif lang_name == "rust":
        imports = _extract_imports_rust(root)
    elif lang_name == "go":
        imports = _extract_imports_go(root)
    elif lang_name == "java":
        imports = _extract_imports_java(root)
    elif lang_name == "csharp":
        imports = _extract_imports_csharp(root)
    elif lang_name == "cpp":
        imports = _extract_imports_cpp(root)
    elif lang_name == "ruby":
        imports = _extract_imports_ruby(root)
    elif lang_name == "c":
        imports = _extract_imports_c(root)
    elif lang_name == "php":
        imports = _extract_imports_php(root)
    elif lang_name == "kotlin":
        imports = _extract_imports_kotlin(root)

    for imp in imports:
        imp.file = filename

    return FileParseResult(symbols=symbols, imports=imports, language=lang_name)


def extract_symbols(filepath: Path) -> list[Symbol]:
    """Extract all symbols (functions, classes) from a source file.

    Returns a list of Symbol objects with names, locations, docstrings,
    and call graphs. (Convenience wrapper around parse_file.)
    """
    return parse_file(filepath).symbols


def build_index(directory: str | Path) -> SymbolIndex:
    """Build a cross-file symbol index for supported source files in a directory."""
    index = SymbolIndex()
    directory = Path(directory)
    dir_str = str(directory)
    for fp in _iter_source_files(directory):
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


def _outline_symbol_count(outline: list[dict[str, object]]) -> int:
    """Count top-level outline entries plus nested class methods."""
    count = 0
    for entry in outline:
        count += 1
        methods = entry.get("methods")
        if isinstance(methods, list):
            count += len(methods)
    return count


def _build_file_outline(symbols: list[Symbol]) -> list[dict[str, object]]:
    """Group class methods under their parent class for repo-map output."""
    outline: list[dict[str, object]] = []
    classes: dict[str, dict[str, object]] = {}

    for sym in symbols:
        if sym.kind == "class":
            item: dict[str, object] = {
                "kind": "class",
                "name": sym.name,
                "methods": [],
            }
            outline.append(item)
            classes[sym.name] = item
            continue

        if sym.kind == "method" and sym.parent_class and sym.parent_class in classes:
            methods = classes[sym.parent_class]["methods"]
            if isinstance(methods, list):
                methods.append({"kind": "method", "name": sym.name})
            continue

        item = {"kind": sym.kind, "name": sym.name}
        if sym.parent_class:
            item["parent_class"] = sym.parent_class
        outline.append(item)

    return outline


def _clip_outline(
    outline: list[dict[str, object]],
    max_symbols_per_file: int,
) -> tuple[list[dict[str, object]], int]:
    """Trim an outline to the requested symbol budget."""
    if max_symbols_per_file <= 0:
        return [], 0

    remaining = max_symbols_per_file
    clipped: list[dict[str, object]] = []
    shown = 0

    for entry in outline:
        if remaining <= 0:
            break

        if entry.get("kind") == "class":
            methods_list: list[dict[str, object]] = []
            class_item = {
                "kind": "class",
                "name": entry["name"],
                "methods": methods_list,
            }
            clipped.append(class_item)
            remaining -= 1
            shown += 1

            methods = entry.get("methods", [])
            if isinstance(methods, list):
                for method in methods:
                    if remaining <= 0:
                        break
                    methods_list.append(dict(method))
                    remaining -= 1
                    shown += 1
            continue

        clipped.append(dict(entry))
        remaining -= 1
        shown += 1

    return clipped, shown


def _repo_map_path_penalty(rel_path: str) -> int:
    """Prefer production code over test-heavy files in repo-map ranking."""
    path = Path(rel_path)
    stem = path.stem.lower()
    dir_parts = {part.lower() for part in path.parts[:-1]}

    if dir_parts & {"test", "tests", "__tests__"}:
        return 1
    if stem == "conftest":
        return 1
    if stem.startswith("test_") or stem.endswith("_test"):
        return 1
    if stem.endswith(".test") or stem.endswith(".spec") or stem.endswith("_spec"):
        return 1
    return 0


def build_repo_map(
    directory: str | Path,
    *,
    max_files: int = 20,
    max_symbols_per_file: int = 12,
) -> dict[str, object]:
    """Build a token-cheap repo skeleton grouped by file and class."""
    root = Path(directory)
    files = _iter_source_files(root)
    file_rows: list[dict[str, object]] = []
    total_symbols = 0
    # Languages whose grammar is unavailable parse to zero symbols for an
    # environment reason, not a real "no symbols" miss. Keyed by language so the
    # repo map surfaces one diagnostic per language instead of silently
    # reporting an empty skeleton (e.g. a Rust repo with no rust grammar would
    # otherwise look identical to a repo with no extractable symbols).
    missing_grammars: dict[str, dict[str, object]] = {}

    for fp in files:
        result = parse_file(fp)
        if not result.symbols:
            diag = result.diagnostic
            if diag is not None and diag.get("code") in (
                "missing-grammar",
                "missing-tree-sitter",
            ):
                code = str(diag.get("code", "unknown"))
                lang = str(diag.get("language", "unknown"))
                # "missing-tree-sitter" is an environment-level problem with a single
                # fix regardless of how many languages are affected, so key by code to
                # deduplicate. "missing-grammar" is per-language, so key by lang.
                key = lang if code == "missing-grammar" else code
                entry = missing_grammars.setdefault(
                    key,
                    {
                        "language": lang,
                        "code": diag.get("code"),
                        "message": diag.get("message"),
                        "files_skipped": 0,
                    },
                )
                entry["files_skipped"] = cast(int, entry["files_skipped"]) + 1
            continue

        outline = _build_file_outline(result.symbols)
        symbol_count = _outline_symbol_count(outline)
        total_symbols += symbol_count
        file_rows.append(
            {
                "path": fp.relative_to(root).as_posix(),
                "symbol_count": symbol_count,
                "outline": outline,
            }
        )

    file_rows.sort(
        key=lambda row: (
            _repo_map_path_penalty(str(row["path"])),
            -cast(int, row["symbol_count"]),
            str(row["path"]),
        ),
    )

    shown_rows = file_rows[: max(0, max_files)]
    files_payload: list[dict[str, object]] = []
    shown_symbols = 0

    for row in shown_rows:
        symbol_count = cast(int, row["symbol_count"])
        outline = cast(list[dict[str, object]], row["outline"])
        clipped_outline, displayed = _clip_outline(
            outline,
            max_symbols_per_file,
        )
        shown_symbols += displayed
        files_payload.append(
            {
                "path": row["path"],
                "symbol_count": symbol_count,
                "displayed_symbol_count": displayed,
                "truncated": displayed < symbol_count,
                "outline": clipped_outline,
            }
        )

    return {
        "directory": str(root.resolve()),
        "files_scanned": len(files),
        "files_with_symbols": len(file_rows),
        "files_shown": len(files_payload),
        "symbols_total": total_symbols,
        "symbols_shown": shown_symbols,
        "max_files": max_files,
        "max_symbols_per_file": max_symbols_per_file,
        "files": files_payload,
        "missing_grammars": sorted(
            missing_grammars.values(),
            key=lambda d: str(d["language"]),
        ),
    }


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


def format_repo_map(repo_map: dict[str, object]) -> str:
    """Format a repo-map payload as a compact human-readable outline."""
    lines = [
        f"Repo map: {repo_map['directory']}",
        (
            "Files shown: "
            f"{repo_map['files_shown']}/{repo_map['files_with_symbols']}  "
            f"Symbols shown: {repo_map['symbols_shown']}/{repo_map['symbols_total']}"
        ),
    ]

    # Surface missing-grammar diagnostics so an empty skeleton caused by an
    # absent tree-sitter grammar is distinguishable from a real empty parse.
    missing = repo_map.get("missing_grammars")
    if isinstance(missing, list):
        for entry_obj in missing:
            if not isinstance(entry_obj, dict):
                continue
            entry = cast(dict[str, object], entry_obj)
            skipped = entry.get("files_skipped", "?")
            message = entry.get("message", "missing grammar")
            lines.append(f"⚠ {skipped} file(s) skipped: {message}")

    files = repo_map.get("files")
    if not isinstance(files, list):
        return "\n".join(lines)

    for file_info_obj in files:
        if not isinstance(file_info_obj, dict):
            continue
        file_info = cast(dict[str, object], file_info_obj)
        total = cast(int, file_info.get("symbol_count", 0))
        displayed = cast(int, file_info.get("displayed_symbol_count", total))
        suffix = (
            f" [{displayed}/{total} symbols]"
            if file_info.get("truncated")
            else f" [{total} symbols]"
        )
        lines.append("")
        lines.append(f"{file_info['path']}{suffix}")
        outline = file_info.get("outline", [])
        if not isinstance(outline, list):
            continue
        for entry in outline:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind", "symbol"))
            name = str(entry.get("name", "?"))
            if kind == "class":
                lines.append(f"  class {name}")
                methods = entry.get("methods", [])
                if isinstance(methods, list):
                    for method in methods:
                        if isinstance(method, dict):
                            lines.append(f"    def {method.get('name', '?')}(...)")
            elif kind == "function":
                lines.append(f"  def {name}(...)")
            elif kind == "method":
                parent = entry.get("parent_class")
                label = f"{parent}.{name}" if parent else name
                lines.append(f"  def {label}(...)")
            else:
                lines.append(f"  {kind} {name}")
        if file_info.get("truncated"):
            lines.append("  ...")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Structural code retrieval via tree-sitter"
    )
    parser.add_argument("file", help="Source file or directory to analyze")
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

    p_map = sub.add_parser(
        "map",
        help="Emit a token-cheap repo map / symbol skeleton",
    )
    p_map.add_argument("--json", action="store_true", help="Output as JSON")
    p_map.add_argument(
        "--max-files",
        type=int,
        default=20,
        help="Maximum files to show (default: 20)",
    )
    p_map.add_argument(
        "--max-symbols",
        type=int,
        default=12,
        help="Maximum symbols per file (default: 12)",
    )

    args = parser.parse_args()
    filepath = Path(args.file)

    if not filepath.exists():
        sys.exit(f"File not found: {filepath}")

    if args.command == "map":
        map_dir = (
            Path(args.directory)
            if args.directory
            else (filepath if filepath.is_dir() else filepath.parent)
        )
        if not map_dir.is_dir():
            sys.exit(f"Directory not found: {map_dir}")
        repo_map = build_repo_map(
            map_dir,
            max_files=args.max_files,
            max_symbols_per_file=args.max_symbols,
        )
        if args.json:
            print(format_json(repo_map))
        else:
            print(format_repo_map(repo_map))
        return

    if not filepath.is_file():
        sys.exit(f"Not a file: {filepath}")

    result = parse_file(filepath)
    symbols = result.symbols
    if not symbols:
        if result.diagnostic is not None:
            if args.json:
                print(format_json({"file": str(filepath), **result.diagnostic}))
            else:
                print(result.diagnostic["message"])
        else:
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
