"""MCP server wrapping gptme-codegraph — structural code retrieval via tree-sitter.

Exposes codegraph tools as MCP resources so any MCP-compatible agent
(Claude Code, Cursor, Codex, gptme) can query code structure without
a separate CLI invocation.

Usage:
    # As an MCP server (stdio transport)
    gptme-codegraph-mcp

    # Configure in Claude Code:
    #   claude mcp add codegraph -- gptme-codegraph-mcp

Tools exposed:
    codegraph_parse                — Extract symbols from a single file
    codegraph_index                — Build cross-file symbol index for a directory
    codegraph_def                  — Find where a symbol is defined
    codegraph_callers              — Find callers of a symbol
    codegraph_callees              — Find callees of a symbol
    codegraph_refs                 — Find references to a symbol
    codegraph_blast                — Compute dependency closure (walks callees)
    codegraph_impact               — Compute impact radius (walks callers — what breaks if you change this)

"""

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

from gptme_codegraph.core import (
    SqliteIndexCache,
    SymbolIndex,
    _resolve_graph_key,
    blast_radius,
    build_call_graph,
    build_cross_file_call_graph,
    build_index,
    extract_symbols,
    impact_radius,
)

mcp = FastMCP("codegraph", log_level="WARNING")

# ---------------------------------------------------------------------------
# In-memory + SQLite index cache
# ---------------------------------------------------------------------------

_index_cache: dict[str, tuple[SymbolIndex, SqliteIndexCache | None]] = {}


def _get_or_build_index(directory: str) -> SymbolIndex:
    """Get a cached index (SQLite-backed, falling through to in-memory).

    When SQLite is available, freshness is re-checked on every call so file
    edits between calls are detected automatically.  Without SQLite backing
    (i.e. the ``[treesitter]`` extra is not installed) the in-memory snapshot
    is returned unconditionally — restart the server to pick up changes.
    """
    dir_path = str(Path(directory).resolve())

    if dir_path in _index_cache:
        cached_index, cached_sqlite = _index_cache[dir_path]
        if cached_sqlite is None:
            # No freshness oracle available — return the snapshot as-is.
            return cached_index
        # Re-check SQLite freshness; returns None when any indexed file changed.
        if cached_sqlite.load() is not None:
            return cached_index
        # Stale — drop and fall through to rebuild.
        del _index_cache[dir_path]

    # Try SQLite first
    sqlite_cache: SqliteIndexCache | None = None
    try:
        sqlite_cache = SqliteIndexCache(dir_path)
        cached = sqlite_cache.load()
        if cached is not None:
            _index_cache[dir_path] = (cached, sqlite_cache)
            return cached
    except Exception:
        pass

    # Build fresh and save to SQLite
    index = build_index(Path(dir_path))
    if sqlite_cache is not None:
        try:
            sqlite_cache.save(index)
        except Exception:
            pass
    _index_cache[dir_path] = (index, sqlite_cache)
    return index


def _invalidate_cache(directory: str) -> None:
    """Force rebuild on next access for a directory."""
    dir_path = str(Path(directory).resolve())
    _index_cache.pop(dir_path, None)


# ---------------------------------------------------------------------------
# Tool: codegraph_parse
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_parse(filepath: str) -> str:
    """Extract symbols (functions, classes) from a Python file.

    Args:
        filepath: Absolute path to the Python file to analyze.

    Returns:
        JSON with symbol count, file, and list of symbols with their
        locations, docstrings, and call graphs.
    """
    fp = Path(filepath)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {filepath}"})
    if not fp.is_file():
        return json.dumps({"error": f"Not a file: {filepath}"})

    symbols = extract_symbols(fp)
    return json.dumps(
        {
            "file": filepath,
            "symbol_count": len(symbols),
            "symbols": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "parent_class": s.parent_class,
                    "docstring": s.docstring[:200] if s.docstring else None,
                    "calls": sorted(s.calls),
                }
                for s in symbols
            ],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Tool: codegraph_index
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_index(directory: str) -> str:
    """Build (or load from SQLite cache) a cross-file symbol index for a directory.

    Args:
        directory: Directory to scan recursively for Python files.

    Returns:
        JSON with file count, symbol count, and top symbols.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return json.dumps({"error": f"Directory not found: {directory}"})

    index = _get_or_build_index(directory)
    all_names = index.all_names()

    # Summarize: first N symbols (alphabetical) and file count
    file_count = len({e.file for entries in index.entries.values() for e in entries})
    sample_names = all_names[:20] if len(all_names) > 20 else all_names

    return json.dumps(
        {
            "directory": str(dir_path.resolve()),
            "files_indexed": file_count,
            "unique_symbols": len(all_names),
            "symbols_sample": sample_names,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Tool: codegraph_def
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_def(
    name: str,
    filepath: str | None = None,
    directory: str | None = None,
) -> str:
    """Find the definition of a symbol.

    Args:
        name: Symbol name to find (function, class, method).
        filepath: Single-file mode: path to the file to search.
        directory: Cross-file mode: directory to search (requires --directory).

    Returns:
        JSON with found, match details, and optional cross-file definitions.
    """
    if filepath:
        fp = Path(filepath)
        if not fp.exists():
            return json.dumps({"error": f"File not found: {filepath}"})
        symbols = extract_symbols(fp)
        for s in symbols:
            if s.name == name:
                result = {
                    "found": True,
                    "matches": [
                        {
                            "name": s.name,
                            "kind": s.kind,
                            "file": fp.name,
                            "start_line": s.start_line,
                            "end_line": s.end_line,
                            "parent_class": s.parent_class,
                            "docstring": s.docstring[:200] if s.docstring else None,
                        }
                    ],
                }
                # Cross-file fallback
                if directory:
                    index = _get_or_build_index(directory)
                    if index.has(name):
                        entries = index.lookup(name)
                        result["definitions"] = len(entries)
                        result["cross_file"] = [
                            {"file": e.file, "line": e.start_line, "kind": e.kind}
                            for e in entries[:10]
                        ]
                return json.dumps(result, indent=2)

        # Not found locally; try cross-file if directory given
        if directory:
            return str(codegraph_def(name, directory=directory))
        return json.dumps({"found": False, "matches": []})

    if directory:
        index = _get_or_build_index(directory)
        if index.has(name):
            entries = index.lookup(name)
            return json.dumps(
                {
                    "found": True,
                    "definitions": len(entries),
                    "matches": [
                        {
                            "name": e.name,
                            "kind": e.kind,
                            "file": e.file,
                            "start_line": e.start_line,
                            "end_line": e.end_line,
                            "parent_class": e.parent_class,
                        }
                        for e in entries[:20]
                    ],
                },
                indent=2,
            )

    return json.dumps({"found": False, "matches": []})


# ---------------------------------------------------------------------------
# Tool: codegraph_callers
# ---------------------------------------------------------------------------


def _cross_file_callers_mcp(name: str, directory: str) -> str:
    """Cross-file caller lookup using the index-based call graph."""
    index = _get_or_build_index(directory)
    if not index.has(name):
        return json.dumps({"error": f"Symbol '{name}' not found in index"})

    dir_path = Path(directory)
    callees_graph, callers_graph = build_cross_file_call_graph(index, dir_path)

    resolved = _resolve_graph_key(name, callers_graph)
    direct_callers = sorted(callers_graph.get(resolved, set())) if resolved else []
    files = {e.file for e in index.lookup(name)}

    return json.dumps(
        {
            "symbol": name,
            "callers": direct_callers,
            "caller_count": len(direct_callers),
            "defined_in": sorted(files),
            "search_directory": str(dir_path),
        },
        indent=2,
    )


def _cross_file_callees_mcp(name: str, directory: str) -> str:
    """Cross-file callee lookup using the index-based call graph."""
    index = _get_or_build_index(directory)
    if not index.has(name):
        return json.dumps({"error": f"Symbol '{name}' not found in index"})

    dir_path = Path(directory)
    callees_graph, _callers_graph = build_cross_file_call_graph(index, dir_path)

    resolved = _resolve_graph_key(name, callees_graph)
    direct_callees = sorted(callees_graph.get(resolved, set())) if resolved else []

    return json.dumps(
        {
            "symbol": name,
            "callees": direct_callees,
            "callee_count": len(direct_callees),
            "search_directory": str(dir_path),
        },
        indent=2,
    )


@mcp.tool()
def codegraph_callers(
    name: str,
    filepath: str | None = None,
    directory: str | None = None,
) -> str:
    """Find all callers of a symbol — within a file, or across an indexed directory.

    Args:
        name: Symbol name to find callers for.
        filepath: Optional path to the Python file containing the symbol.
            If omitted, ``directory`` must be set for index-based lookup.
        directory: Optional directory for cross-file index lookup.

    Returns:
        JSON with symbol, callers list, and cross-file info.
    """
    # Cross-file mode: no filepath, use index
    if filepath is None:
        if not directory:
            return json.dumps({"error": "Either filepath or directory is required"})
        return _cross_file_callers_mcp(name, directory)

    # Single-file mode
    fp = Path(filepath)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {filepath}"})

    symbols = extract_symbols(fp)
    name_map = {s.name: s for s in symbols}
    _callees_graph, callers_graph = build_call_graph(symbols)

    if name not in name_map:
        # Try cross-file as fallback
        if directory:
            return _cross_file_callers_mcp(name, directory)
        return json.dumps({"error": f"Symbol '{name}' not found in {filepath}"})

    resolved = _resolve_graph_key(name, callers_graph)
    callers = sorted(callers_graph.get(resolved, set())) if resolved else []
    result: dict[str, Any] = {
        "symbol": name,
        "callers": callers,
    }

    if directory:
        index = _get_or_build_index(directory)
        if index.has(name):
            entries = index.lookup(name)
            result["definitions"] = len(entries)
            result["cross_file_defs"] = [
                {"file": e.file, "line": e.start_line} for e in entries[:10]
            ]

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool: codegraph_callees
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_callees(
    name: str,
    filepath: str | None = None,
    directory: str | None = None,
) -> str:
    """Find all symbols called by a given function/method.

    Args:
        name: Symbol name to find callees for.
        filepath: Optional path to the Python file containing the symbol.
            If omitted, ``directory`` must be set for index-based lookup.
        directory: Optional directory for cross-file lookup.

    Returns:
        JSON with symbol, callees list, and optional cross-file info.
    """
    # Cross-file mode: no filepath, use index
    if filepath is None:
        if not directory:
            return json.dumps({"error": "Either filepath or directory is required"})
        return _cross_file_callees_mcp(name, directory)

    # Single-file mode
    fp = Path(filepath)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {filepath}"})

    symbols = extract_symbols(fp)
    name_map = {s.name: s for s in symbols}
    callees_graph, _callers_graph = build_call_graph(symbols)

    if name not in name_map:
        if directory:
            index = _get_or_build_index(directory)
            if index.has(name):
                return json.dumps(
                    {
                        "symbol": name,
                        "note": "Symbol found in cross-file index but callee"
                        " analysis is single-file only.",
                        "definitions": len(index.lookup(name)),
                    },
                    indent=2,
                )
        return json.dumps({"error": f"Symbol '{name}' not found in {filepath}"})

    resolved = _resolve_graph_key(name, callees_graph)
    callees = sorted(callees_graph.get(resolved, set())) if resolved else []
    result: dict[str, Any] = {
        "symbol": name,
        "callees": callees,
    }

    if directory:
        index = _get_or_build_index(directory)
        if index.has(name):
            result["definitions"] = len(index.lookup(name))

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool: codegraph_refs
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_refs(
    name: str,
    filepath: str,
    directory: str | None = None,
) -> str:
    """Find all references to a symbol (delegates to callers for now).

    Args:
        name: Symbol name to find references for.
        filepath: Path to the Python file.
        directory: Optional directory for cross-file lookup.

    Returns:
        JSON with symbol and references list.
    """
    return str(codegraph_callers(name, filepath, directory))


# ---------------------------------------------------------------------------
# Tool: codegraph_blast
# ---------------------------------------------------------------------------


@mcp.tool()
def codegraph_blast(
    name: str,
    filepath: str | None = None,
    max_depth: int = 5,
    directory: str | None = None,
) -> str:
    """Compute the dependency closure of a symbol — what it transitively depends on.

    Walks the callee graph (downstream): if you read this symbol's body, what
    other symbols does it ultimately invoke? Use ``codegraph_impact`` instead
    when you want the upstream view ("what breaks if I change this").

    Args:
        name: Symbol name to compute dependency closure for.
        filepath: Optional path to the Python file containing the symbol.
            If omitted, ``directory`` must be set for index-based cross-file mode.
        max_depth: Maximum depth for transitive call graph (default: 5).
        directory: Optional directory for cross-file lookup.

    Returns:
        JSON with symbol, total affected, and breakdown by depth.
    """
    # Cross-file mode: no filepath, use index-based cross-file blast
    if filepath is None:
        if not directory:
            return json.dumps({"error": "Either filepath or directory is required"})
        index = _get_or_build_index(directory)
        dir_path = Path(directory)
        callees_graph, _callers_graph = build_cross_file_call_graph(index, dir_path)
        resolved = _resolve_graph_key(name, callees_graph)
        if resolved is None:
            if not index.has(name):
                return json.dumps({"error": f"Symbol '{name}' not found in index"})
            files = {e.file for e in index.lookup(name)}
            return json.dumps(
                {
                    "symbol": name,
                    "total_affected": 1,
                    "depth_breakdown": {"depth_0": [name]},
                    "defined_in": sorted(files),
                    "note": "No cross-file calls found for this symbol",
                },
                indent=2,
            )
        radius = blast_radius(resolved, callees_graph, max_depth=max_depth)
        total = sum(len(names) for names in radius.values())
        files = {e.file for e in index.lookup(name)}
        return json.dumps(
            {
                "symbol": name,
                "total_affected": total,
                "depth_breakdown": {k: sorted(v) for k, v in sorted(radius.items())},
                "defined_in": sorted(files),
            },
            indent=2,
        )

    # Single-file mode
    fp = Path(filepath)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {filepath}"})

    symbols = extract_symbols(fp)
    name_map = {s.name: s for s in symbols}
    callees_graph, _callers_graph = build_call_graph(symbols)

    if name not in name_map:
        if directory:
            index = _get_or_build_index(directory)
            if index.has(name):
                return json.dumps(
                    {
                        "symbol": name,
                        "note": "Symbol in index but blast radius requires"
                        " single-file call graph.",
                        "definitions": len(index.lookup(name)),
                    }
                )
        return json.dumps({"error": f"Symbol '{name}' not found in {filepath}"})

    radius = blast_radius(name, callees_graph, max_depth=max_depth)
    total = sum(len(names) for names in radius.values())

    result = {
        "symbol": name,
        "total_affected": total,
        "depth_breakdown": {k: sorted(v) for k, v in sorted(radius.items())},
    }

    if directory:
        index = _get_or_build_index(directory)
        if index.has(name):
            result["definitions"] = len(index.lookup(name))

    return json.dumps(result, indent=2)


@mcp.tool()
def codegraph_impact(
    name: str,
    filepath: str | None = None,
    max_depth: int = 5,
    directory: str | None = None,
) -> str:
    """Compute the impact radius of a symbol — what breaks if you change it.

    Walks the caller graph (upstream): which symbols transitively depend on
    this one? Use ``codegraph_blast`` for the downstream view (what this
    symbol depends on).

    Args:
        name: Symbol name to compute impact radius for.
        filepath: Optional path to the Python file containing the symbol.
            If omitted, ``directory`` must be set for index-based cross-file mode.
        max_depth: Maximum depth for transitive call graph (default: 5).
        directory: Optional directory for cross-file lookup.

    Returns:
        JSON with symbol, total affected callers, and breakdown by depth.
    """
    # Cross-file mode: no filepath, use index-based cross-file impact
    if filepath is None:
        if not directory:
            return json.dumps({"error": "Either filepath or directory is required"})
        index = _get_or_build_index(directory)
        dir_path = Path(directory)
        _callees_graph, callers_graph = build_cross_file_call_graph(index, dir_path)
        resolved = _resolve_graph_key(name, callers_graph)
        if resolved is None:
            if not index.has(name):
                return json.dumps({"error": f"Symbol '{name}' not found in index"})
            files = {e.file for e in index.lookup(name)}
            return json.dumps(
                {
                    "symbol": name,
                    "total_affected": 1,
                    "depth_breakdown": {"depth_0": [name]},
                    "defined_in": sorted(files),
                    "note": "No cross-file callers found for this symbol",
                },
                indent=2,
            )
        radius = impact_radius(resolved, callers_graph, max_depth=max_depth)
        total = sum(len(names) for names in radius.values())
        files = {e.file for e in index.lookup(name)}
        return json.dumps(
            {
                "symbol": name,
                "total_affected": total,
                "depth_breakdown": {k: sorted(v) for k, v in sorted(radius.items())},
                "defined_in": sorted(files),
            },
            indent=2,
        )

    # Single-file mode
    fp = Path(filepath)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {filepath}"})

    symbols = extract_symbols(fp)
    name_map = {s.name: s for s in symbols}
    _callees_graph, callers_graph = build_call_graph(symbols)

    if name not in name_map:
        if directory:
            index = _get_or_build_index(directory)
            if index.has(name):
                return json.dumps(
                    {
                        "symbol": name,
                        "note": "Symbol in index but impact radius requires"
                        " single-file call graph.",
                        "definitions": len(index.lookup(name)),
                    }
                )
        return json.dumps({"error": f"Symbol '{name}' not found in {filepath}"})

    radius = impact_radius(name, callers_graph, max_depth=max_depth)
    total = sum(len(names) for names in radius.values())

    result: dict[str, Any] = {
        "symbol": name,
        "total_affected": total,
        "depth_breakdown": {k: sorted(v) for k, v in sorted(radius.items())},
    }

    if directory:
        index = _get_or_build_index(directory)
        if index.has(name):
            result["definitions"] = len(index.lookup(name))

    return json.dumps(result, indent=2)


def main() -> None:
    """Run the codegraph MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
