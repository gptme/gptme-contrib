"""Parse Bash command strings for file-write and commit detection.

Uses tree-sitter-bash when available for reliable heredoc, redirect, and
git-command detection. Falls back to regex if the parser is not installed.

Public API
----------
bash_heredoc_write_paths(cmd) -> list[str]
    Paths written via heredoc in a Bash command (cat/tee with <<).

has_git_commit_command(cmd) -> bool
    True when cmd contains a git commit or git-safe-commit invocation.

has_git_push_command(cmd) -> bool
    True when cmd contains a git push invocation.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Optional tree-sitter-bash parser
# ---------------------------------------------------------------------------

try:
    import tree_sitter_bash as _tsb
    from tree_sitter import Language as _Language
    from tree_sitter import Parser as _Parser

    _BASH_LANG = _Language(_tsb.language())
    _PARSER = _Parser(_BASH_LANG)
    _TREE_SITTER_AVAILABLE = True
except Exception:  # ImportError or any wheel-load failure
    _TREE_SITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------


def _ts_find_all(node: Any, *types: str) -> list:
    """Depth-first collect all nodes whose .type is in *types."""
    results: list = []
    if node.type in types:
        results.append(node)
    for child in node.children:
        results.extend(_ts_find_all(child, *types))
    return results


def _ts_path_text(node: Any) -> str | None:
    """Extract the string value from a path node (word/string/raw_string/concatenation)."""
    if node is None:
        return None
    node_type = node.type
    # Double-quoted strings: the node type is 'string' and the path is in the
    # 'string_content' child, not the node text (which includes the quotes).
    if node_type == "string":
        for child in node.children:
            if child.type == "string_content":
                return str(child.text.decode(errors="replace"))
        return None
    # Concatenation: e.g. /journal/$(date +%Y-%m-%d)/session.md is represented
    # as concatenation of word("/journal/") + command_substitution + word("/session.md").
    # Reconstruct by joining child text; preserve $(…) and ${VAR} literally so the
    # caller can resolve date expansions or glob parameter expansions. Dropping a
    # ${VAR} child would mangle the path (e.g. "/journal/${DATE}/x.md" ->
    # "/journal//x.md"), and the mangled string no longer matches the caller's
    # "${" glob branch, silently losing the journal write.
    if node_type == "concatenation":
        parts: list[str] = []
        for child in node.children:
            if child.type in ("word", "raw_string"):
                parts.append(child.text.decode(errors="replace").strip("'\""))
            elif child.type in ("command_substitution", "expansion", "variable_expansion"):
                parts.append(child.text.decode(errors="replace"))
        return "".join(parts) if parts else None
    return str(node.text.decode(errors="replace").strip("'\""))


def _ts_file_redirect_path(fr_node: Any) -> str | None:
    """Return the destination path of a file_redirect node (> or >>), or None."""
    children = list(fr_node.children)
    has_write_op = any(c.type in (">", ">>") for c in children)
    if not has_write_op:
        return None
    # A file-descriptor redirect (`2> err.log`) targets stderr, not stdout, so
    # its path is not a heredoc *write* even when the statement also writes a
    # heredoc to stdin. Only plain `>`/`>>` count.
    if any(c.type == "file_descriptor" for c in children):
        return None
    for c in children:
        if c.type in (
            "word",
            "raw_string",
            "string",
            "concatenation",
            "command_substitution",
            "expansion",
            "variable_expansion",
        ):
            return _ts_path_text(c)
    return None


def _ts_heredoc_write_paths(cmd: str) -> list[str]:
    """Tree-sitter implementation: paths written via heredoc."""
    tree = _PARSER.parse(cmd.encode())
    root = tree.root_node

    paths: list[str] = []

    for stmt in _ts_find_all(root, "redirected_statement"):
        # Only process statements that contain a heredoc redirect.
        all_heredocs = _ts_find_all(stmt, "heredoc_redirect")
        if not all_heredocs:
            continue

        # 1. file_redirect children anywhere inside this statement (including
        #    nested inside heredoc_redirect for `cat <<'EOF' > file` syntax).
        for fr in _ts_find_all(stmt, "file_redirect"):
            path = _ts_file_redirect_path(fr)
            if path and path not in paths:
                paths.append(path)

        # 2. tee command: first non-flag word argument is the output file.
        for cmd_node in _ts_find_all(stmt, "command"):
            name_nodes = [c for c in cmd_node.children if c.type == "command_name"]
            if not name_nodes:
                continue
            if name_nodes[0].text.decode() != "tee":
                continue
            for arg in cmd_node.children:
                if arg.type not in ("word", "raw_string", "string"):
                    continue
                if arg is cmd_node.children[0]:
                    continue  # skip command_name child (shouldn't happen, but safe)
                text = _ts_path_text(arg)
                if text and not text.startswith("-") and text not in paths:
                    paths.append(text)
                    break

    return paths


# git global options that consume the following token as their value.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


def _ts_git_subcommand(cmd_node: Any) -> str | None:
    """Return git's subcommand — the first positional (non-option) argument.

    Anchoring on the subcommand avoids false positives that a bare
    ``"commit" in args`` check produces: ``git log commit`` or
    ``git log --grep commit`` contain the word ``commit`` but are not commit
    invocations. This mirrors the regex fallback's ``git [-C path] commit``
    anchoring while also tolerating value-taking options such as
    ``git -c user.name=x commit``.
    """
    words = [str(c.text.decode()) for c in cmd_node.children if c.type == "word"]
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            # Skip the option, and its value too for value-taking options.
            i += 2 if word in _GIT_VALUE_OPTS else 1
            continue
        return word
    return None


def _ts_has_git_commit(cmd: str) -> bool:
    """Tree-sitter: True when cmd contains a git commit or git-safe-commit."""
    tree = _PARSER.parse(cmd.encode())
    root = tree.root_node

    for cmd_node in _ts_find_all(root, "command"):
        name_nodes = [c for c in cmd_node.children if c.type == "command_name"]
        if not name_nodes:
            continue
        name = name_nodes[0].text.decode()
        if name == "git-safe-commit":
            return True
        if name == "git" and _ts_git_subcommand(cmd_node) == "commit":
            return True
    return False


def _ts_has_git_push(cmd: str) -> bool:
    """Tree-sitter: True when cmd contains a git push."""
    tree = _PARSER.parse(cmd.encode())
    root = tree.root_node

    for cmd_node in _ts_find_all(root, "command"):
        name_nodes = [c for c in cmd_node.children if c.type == "command_name"]
        if not name_nodes:
            continue
        name = name_nodes[0].text.decode()
        if name == "git" and _ts_git_subcommand(cmd_node) == "push":
            return True
    return False


# ---------------------------------------------------------------------------
# Regex fallbacks (used when tree-sitter is unavailable)
# ---------------------------------------------------------------------------

# cat > file <<, cat >> file <<, tee file <<, tee -a file <<
# Also handles `cat <<'EOF' > file` ordering.
# Group 1: optional quote, Group 2: path (no quotes/redirects/pipes).
# The cat patterns use two forms: one for paths without spaces (fast path),
# and one using .*? up to a terminator for paths that may include shell
# expansions like $(date +%Y-%m-%d) which contain spaces.
_HEREDOC_WRITE_RES = (
    # cat >> path << or cat > path <<  (unquoted paths without spaces)
    re.compile(r"\bcat\s*>>?\s*(['\"]?)([^\s'\"<>|;&]+)\1\s*<<"),
    # cat > "quoted path with spaces" << (double or single quoted paths)
    re.compile(r'\bcat\s*>>?\s*(["\'])([^"\']*?)\1\s*<<'),
    # cat >> "path with $(date) expansion" << (paths ending with known ext)
    re.compile(r"\bcat\s*>>?\s*(.*?\.(?:md|txt|py|sh|json|yaml|toml))\s+<<"),
    # cat << 'EOF' >> path  (heredoc then redirect)
    re.compile(r"\bcat\s+<<-?\s*['\"]?\w+['\"]?\s*>>?\s*(['\"]?)([^\s'\"<>|;&]+)\1"),
    re.compile(r"\btee\s+(?:-a\s+)?(['\"]?)([^\s'\"<>|;&-][^\s'\"<>|;&]*)\1\s*<<"),
)

# Optional git global options before the subcommand. Value-taking options
# (-C <path>, -c <name>=<val>, --git-dir <dir>, ...) consume their following
# token; other options are bare flags. Anchoring on the subcommand keeps
# `git log commit` / `git log --grep push` from matching as commit/push.
_GIT_VALUE_OPTS_RE = r"(?:-C|-c|--git-dir|--work-tree|--namespace|--exec-path)"
_GIT_OPTS = rf"(?:\s+{_GIT_VALUE_OPTS_RE}\s+\S+|\s+-{{1,2}}[A-Za-z][\w-]*(?:=\S+)?)*"
_COMMIT_CMD_RE = re.compile(rf"\bgit{_GIT_OPTS}\s+commit\b|\bgit-safe-commit\b")
_PUSH_CMD_RE = re.compile(rf"\bgit{_GIT_OPTS}\s+push\b")


def _re_heredoc_write_paths(cmd: str) -> list[str]:
    """Regex fallback: paths written via heredoc."""
    paths: list[str] = []
    for pattern in _HEREDOC_WRITE_RES:
        for m in pattern.finditer(cmd):
            # Patterns with 2 groups: group(1)=quote, group(2)=path.
            # Patterns with 1 group: group(1)=path directly.
            try:
                path = m.group(2).strip()
            except IndexError:
                path = m.group(1).strip()
            if path and path not in paths:
                paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _filter_paths(raw: list[str]) -> list[str]:
    """Drop device/proc paths; deduplicate. Callers apply any further exclusions."""
    out: list[str] = []
    for path in raw:
        if not path:
            continue
        if path.startswith("/dev/") or path.startswith("/proc/"):
            continue
        if path not in out:
            out.append(path)
    return out


def bash_heredoc_write_paths(cmd: str) -> list[str]:
    """Return file paths written via heredoc (cat/tee + <<) in a Bash command.

    Uses tree-sitter-bash when available; falls back to regex otherwise.
    When tree-sitter is available, regex runs as a supplement to catch edge cases
    where unusual path syntax (e.g. escaped ``\\$(date)``) confuses the parser.
    Excludes /dev/* and /proc/*. All other paths are returned; callers apply
    further filtering (e.g. skip /tmp/ scratch, route /journal/ separately).
    """
    if _TREE_SITTER_AVAILABLE:
        raw = _ts_heredoc_write_paths(cmd)
        # Supplement with regex: tree-sitter may truncate paths that contain
        # escaped ``\$(...)`` sequences (parsed as subshells, not path text).
        for p in _re_heredoc_write_paths(cmd):
            if p not in raw:
                raw.append(p)
    else:
        raw = _re_heredoc_write_paths(cmd)
    return _filter_paths(raw)


def has_git_commit_command(cmd: str) -> bool:
    """True when *cmd* contains a ``git commit`` or ``git-safe-commit`` call.

    Uses tree-sitter-bash when available; falls back to regex otherwise.
    """
    if _TREE_SITTER_AVAILABLE:
        return _ts_has_git_commit(cmd)
    return bool(_COMMIT_CMD_RE.search(cmd))


def has_git_push_command(cmd: str) -> bool:
    """True when *cmd* contains a ``git push`` call.

    Uses tree-sitter-bash when available; falls back to regex otherwise.
    """
    if _TREE_SITTER_AVAILABLE:
        return _ts_has_git_push(cmd)
    return bool(_PUSH_CMD_RE.search(cmd))
