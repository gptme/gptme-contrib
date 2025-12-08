"""
Warp Grep agentic code search tool for gptme.

Uses Morph's warp-grep model to intelligently search codebases
over multiple turns until relevant code is found.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from gptme.tools.base import ToolSpec

# Constants from Morph SDK
MAX_TURNS = 4
TIMEOUT_MS = 30000

# Default excludes (common patterns to skip)
DEFAULT_EXCLUDES = [
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pyc",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    ".coverage",
    "htmlcov",
    "dist",
    "build",
    ".egg-info",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "*.lock",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
]

# System prompt from Morph documentation
SYSTEM_PROMPT = """You are a code search agent. Your task is to find all relevant code for a given query.

<workflow>
You have exactly 4 turns. The 4th turn MUST be a `finish` call. Each turn allows up to 8 parallel tool calls.

- Turn 1: Map the territory OR dive deep (based on query specificity)
- Turn 2-3: Refine based on findings
- Turn 4: MUST call `finish` with all relevant code locations
- You MAY call `finish` early if confident—but never before at least 1 search turn.

Remember, if the task feels easy to you, it is strongly desirable to call `finish` early using fewer turns, but quality over speed.
</workflow>

<tools>
### `analyse <path> [pattern]`
Directory tree or file search. Shows structure of a path, optionally filtered by regex pattern.
- `path`: Required. Directory or file path (use `.` for repo root)
- `pattern`: Optional regex to filter results

Examples:
analyse .
analyse src/api
analyse . ".*\\.ts$"
analyse src "test.*"

### `read <path>[:start-end]`
Read file contents. Line range is 1-based, inclusive.
- Returns numbered lines for easy reference
- Omit range to read entire file

Examples:
read src/main.py
read src/db/conn.py:10-50
read package.json:1-20

### `grep '<pattern>' <path>`
Ripgrep search. Finds pattern matches across files.
- `'<pattern>'`: Required. Regex pattern wrapped in single quotes
- `<path>`: Required. Directory or file to search (use `.` for repo root)

Examples:
grep 'class.*Service' src/
grep 'def authenticate' .
grep 'import.*from' src/components/
grep 'TODO' .

### `finish <file1:ranges> [file2:ranges ...]`
Submit final answer with all relevant code locations.
- Include generous line ranges—don't be stingy with context
- Ranges are comma-separated: `file.py:10-30,50-60`
- ALWAYS include import statements at the top of files (usually lines 1-20)
- If code spans multiple files, include ALL of them
- Small files can be returned in full

Examples:
finish src/auth.py:1-15,25-50,75-80 src/models/user.py:1-10,20-45
finish src/index.ts:1-100
</tools>

<strategy>
**Before your first tool call, classify the query:**

| Query Type | Turn 1 Strategy | Early Finish? |
|------------|-----------------|---------------|
| **Specific** (function name, error string, unique identifier) | 8 parallel greps on likely paths | Often by turn 2 |
| **Conceptual** (how does X work, where is Y handled) | analyse + 2-3 broad greps | Rarely early |
| **Exploratory** (find all tests, list API endpoints) | analyse at multiple depths | Usually needs 3 turns |

**Parallel call patterns:**
- **Shotgun grep**: Same pattern, 8 different directories—fast coverage
- **Variant grep**: 8 pattern variations (synonyms, naming conventions)—catches inconsistent codebases
- **Funnel**: 1 analyse + 7 greps—orient and search simultaneously
- **Deep read**: 8 reads on files you already identified—gather full context fast
</strategy>

<output_format>
EVERY response MUST follow this exact format:

1. First, wrap your reasoning in `<think>...</think>` tags containing:
   - Query classification (specific/conceptual/exploratory)
   - Confidence estimate (can I finish in 1-2 turns?)
   - This turn's parallel strategy
   - What signals would let me finish early?

2. Then, output tool calls wrapped in `<tool_call>...</tool_call>` tags, one per line.

Example:
<think>
This is a specific query about authentication. I'll grep for auth-related patterns.
High confidence I can finish in 2 turns if I find the auth module.
Strategy: Shotgun grep across likely directories.
</think>
<tool_call>grep 'authenticate' src/</tool_call>
<tool_call>grep 'login' src/</tool_call>
<tool_call>analyse src/auth</tool_call>

No commentary outside `<think>`. No explanations after tool calls.
</output_format>

<finishing_requirements>
When calling `finish`:
- Include the import section (typically lines 1-20) of each file
- Include all function/class definitions that are relevant
- Include any type definitions, interfaces, or constants used
- Better to over-include than leave the user missing context
- If unsure about boundaries, include more rather than less
</finishing_requirements>

Begin your exploration now to find code relevant to the query."""


@dataclass
class ToolCall:
    """Parsed tool call from model output."""

    name: str
    arguments: dict


@dataclass
class ResolvedFile:
    """A file with its content resolved."""

    path: str
    content: str


def parse_tool_calls(text: str) -> list[ToolCall]:
    """Parse tool calls from model output (XML or plain format)."""
    # Remove think blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    tool_calls = []

    # Try XML format first: <tool_call>...</tool_call>
    xml_matches = list(re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL))
    if xml_matches:
        for match in xml_matches:
            call_text = match.group(1).strip()
            tool_call = _parse_single_tool_call(call_text)
            if tool_call:
                tool_calls.append(tool_call)
        return tool_calls

    # Fall back to plain format: one command per line
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tool_call = _parse_single_tool_call(line)
        if tool_call:
            tool_calls.append(tool_call)

    return tool_calls


def _parse_single_tool_call(text: str) -> ToolCall | None:
    """Parse a single tool call text into structured format."""
    text = text.strip()

    # Parse grep: grep 'pattern' path
    grep_match = re.match(r"grep\s+'([^']+)'\s+(.+)", text)
    if grep_match:
        return ToolCall(
            name="grep",
            arguments={"pattern": grep_match.group(1), "path": grep_match.group(2)},
        )

    # Parse read: read path[:start-end]
    read_match = re.match(r"read\s+([^\s:]+)(?::(\d+)-(\d+))?", text)
    if read_match:
        args: dict = {"path": read_match.group(1)}
        if read_match.group(2):
            args["start"] = int(read_match.group(2))
            args["end"] = int(read_match.group(3))
        return ToolCall(name="read", arguments=args)

    # Parse analyse: analyse path [pattern]
    analyse_match = re.match(r"analyse\s+(\S+)(?:\s+\"([^\"]+)\")?", text)
    if analyse_match:
        args = {"path": analyse_match.group(1)}
        if analyse_match.group(2):
            args["pattern"] = analyse_match.group(2)
        return ToolCall(name="analyse", arguments=args)

    # Parse finish: finish file1:ranges file2:ranges ...
    if text.startswith("finish"):
        files_text = text[6:].strip()
        files = _parse_finish_files(files_text)
        return ToolCall(name="finish", arguments={"files": files})

    return None


def _parse_finish_files(text: str) -> list[dict]:
    """Parse finish command file specifications."""
    files = []
    # Match file:ranges patterns
    for match in re.finditer(r"(\S+?):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)", text):
        path = match.group(1)
        ranges_text = match.group(2)
        ranges = []
        for range_match in re.finditer(r"(\d+)(?:-(\d+))?", ranges_text):
            start = int(range_match.group(1))
            end = int(range_match.group(2)) if range_match.group(2) else start
            ranges.append([start, end])
        files.append({"path": path, "lines": ranges})
    return files


def format_tool_result(name: str, args: dict, output: str) -> str:
    """Format tool output with XML wrapper for model consumption."""
    args_str = " ".join(f'{k}="{v}"' for k, v in args.items() if k != "files")
    return f"<{name}_output {args_str}>\n{output}\n</{name}_output>"


def format_turn_message(turn: int, max_turns: int = MAX_TURNS) -> str:
    """Format turn counter message."""
    remaining = max_turns - turn
    if turn >= max_turns:
        return f"\n\n[Turn {turn}/{max_turns}] This is your LAST turn. You MUST call finish now."
    return f"\n\n[Turn {turn}/{max_turns}] You have {remaining} turns remaining."


def format_analyse_tree(entries: list[dict]) -> str:
    """Format directory listing entries as a tree."""
    lines = []
    for entry in entries:
        indent = "  " * entry.get("depth", 0)
        marker = "[D]" if entry["type"] == "dir" else "[F]"
        lines.append(f"{indent}- {marker} {entry['name']}")
    return "\n".join(lines)


class LocalProvider:
    """Local filesystem provider for warp-grep operations."""

    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).resolve()

    def grep(self, pattern: str, path: str) -> dict:
        """Search for pattern using ripgrep."""
        search_path = (self.repo_root / path).resolve()
        # Security: prevent path traversal attacks
        if not search_path.is_relative_to(self.repo_root):
            return {"lines": [], "error": f"Path outside repository: {path}"}
        if not search_path.exists():
            return {"lines": [], "error": f"Path not found: {path}"}

        # Build exclude args
        exclude_args = []
        for exc in DEFAULT_EXCLUDES:
            exclude_args.extend(["--glob", f"!{exc}"])

        try:
            result = subprocess.run(
                ["rg", "--line-number", "--no-heading", pattern, str(search_path)]
                + exclude_args,
                capture_output=True,
                text=True,
                timeout=30,
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            # Limit output to prevent token explosion
            if len(lines) > 100:
                lines = lines[:100] + [f"... ({len(lines) - 100} more matches)"]
            return {"lines": lines}
        except subprocess.TimeoutExpired:
            return {"lines": [], "error": "Search timed out"}
        except FileNotFoundError:
            return {"lines": [], "error": "ripgrep (rg) not found"}
        except Exception as e:
            return {"lines": [], "error": str(e)}

    def read(self, path: str, start: int | None = None, end: int | None = None) -> dict:
        """Read file contents with optional line range."""
        file_path = (self.repo_root / path).resolve()
        # Security: prevent path traversal attacks
        if not file_path.is_relative_to(self.repo_root):
            return {"lines": [], "error": f"Path outside repository: {path}"}
        if not file_path.exists():
            return {"lines": [], "error": f"File not found: {path}"}
        if not file_path.is_file():
            return {"lines": [], "error": f"Not a file: {path}"}

        try:
            with open(file_path) as f:
                all_lines = f.readlines()

            # Apply line range (1-based, inclusive)
            if start is not None and end is not None:
                start_idx = max(0, start - 1)
                end_idx = min(len(all_lines), end)
                selected = all_lines[start_idx:end_idx]
                # Format with line numbers
                lines = [
                    f"{start + i}|{line.rstrip()}" for i, line in enumerate(selected)
                ]
            else:
                # Full file with line numbers
                lines = [f"{i + 1}|{line.rstrip()}" for i, line in enumerate(all_lines)]

            # Limit output
            if len(lines) > 500:
                lines = lines[:500] + [f"... ({len(all_lines) - 500} more lines)"]

            return {"lines": lines}
        except Exception as e:
            return {"lines": [], "error": str(e)}

    def analyse(
        self,
        path: str,
        pattern: str | None = None,
        max_results: int = 100,
        max_depth: int = 3,
    ) -> list[dict]:
        """List directory structure."""
        target_path = (self.repo_root / path).resolve()
        # Security: prevent path traversal attacks
        if not target_path.is_relative_to(self.repo_root):
            return [
                {
                    "name": path,
                    "path": path,
                    "type": "error",
                    "depth": 0,
                    "error": "Path outside repository",
                }
            ]
        if not target_path.exists():
            return [{"name": path, "path": path, "type": "error", "depth": 0}]

        entries: list[dict] = []
        pattern_re = re.compile(pattern) if pattern else None

        def walk(p: Path, depth: int = 0):
            if depth > max_depth or len(entries) >= max_results:
                return

            try:
                items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except PermissionError:
                return

            for item in items:
                # Skip excluded patterns (exact name match to avoid false positives like "rebuild" matching "build")
                if any(
                    item.name == exc or item.name.endswith(exc)
                    for exc in DEFAULT_EXCLUDES
                ):
                    continue

                rel_path = str(item.relative_to(self.repo_root))

                # Apply pattern filter
                if pattern_re and not pattern_re.search(item.name):
                    if item.is_file():
                        continue

                entry = {
                    "name": item.name,
                    "path": rel_path,
                    "type": "dir" if item.is_dir() else "file",
                    "depth": depth,
                }
                entries.append(entry)

                if len(entries) >= max_results:
                    return

                if item.is_dir():
                    walk(item, depth + 1)

        walk(target_path)
        return entries

    def resolve_finish_files(self, files: list[dict]) -> list[ResolvedFile]:
        """Read file ranges specified in a finish command."""
        resolved = []
        for file_spec in files:
            path = file_spec["path"]
            ranges = file_spec.get("lines", [])

            file_path = (self.repo_root / path).resolve()
            # Security: prevent path traversal attacks
            if not file_path.is_relative_to(self.repo_root):
                resolved.append(
                    ResolvedFile(
                        path=path, content=f"# Path outside repository: {path}"
                    )
                )
                continue
            if not file_path.exists():
                resolved.append(
                    ResolvedFile(path=path, content=f"# File not found: {path}")
                )
                continue

            try:
                with open(file_path) as f:
                    all_lines = f.readlines()

                # Collect all requested ranges
                content_lines = []
                for start, end in ranges:
                    start_idx = max(0, start - 1)
                    end_idx = min(len(all_lines), end)
                    for i, line in enumerate(all_lines[start_idx:end_idx], start=start):
                        content_lines.append(f"{i}|{line.rstrip()}")
                    content_lines.append("")  # Separator between ranges

                resolved.append(
                    ResolvedFile(path=path, content="\n".join(content_lines))
                )
            except Exception as e:
                resolved.append(
                    ResolvedFile(path=path, content=f"# Error reading {path}: {e}")
                )

        return resolved


def _call_morph_api(messages: list[dict], api_key: str) -> str:
    """Call the Morph warp-grep API."""
    with httpx.Client(timeout=TIMEOUT_MS / 1000) as client:
        response = client.post(
            "https://api.morphllm.com/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": "morph-warp-grep",
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 1024,
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"])


def warp_grep_search(
    query: str,
    repo_root: str | Path = ".",
    api_key: str | None = None,
) -> list[ResolvedFile]:
    """
    Search a codebase using Morph's warp-grep agentic search.

    Args:
        query: Natural language query describing what code to find
        repo_root: Root directory of the repository to search
        api_key: Morph API key (defaults to MORPH_API_KEY env var)

    Returns:
        List of ResolvedFile with path and content of relevant code
    """
    api_key = api_key or os.environ.get("MORPH_API_KEY")
    if not api_key:
        raise ValueError(
            "MORPH_API_KEY not set. Get one at https://morphllm.com/dashboard"
        )

    provider = LocalProvider(repo_root)

    # Build initial messages
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"<query>{query}</query>"},
    ]

    # Add initial repo state
    initial_entries = provider.analyse(".", max_results=50)
    dirs = [e["name"] for e in initial_entries if e["type"] == "dir"]
    files = [e["name"] for e in initial_entries if e["type"] == "file"]
    messages.append(
        {
            "role": "user",
            "content": f"<repo_root>{provider.repo_root}</repo_root>\n<top_dirs>{', '.join(dirs)}</top_dirs>\n<top_files>{', '.join(files)}</top_files>",
        }
    )

    # Agent loop
    for turn in range(1, MAX_TURNS + 1):
        # Call model
        response = _call_morph_api(messages, api_key)
        messages.append({"role": "assistant", "content": response})

        # Parse tool calls
        tool_calls = parse_tool_calls(response)
        if not tool_calls:
            break

        # Check for finish first
        finish_call = next((c for c in tool_calls if c.name == "finish"), None)
        if finish_call:
            return provider.resolve_finish_files(finish_call.arguments.get("files", []))

        # Execute all tools
        results = []
        for call in tool_calls:
            if call.name == "grep":
                r = provider.grep(call.arguments["pattern"], call.arguments["path"])
                output = r.get("error") or "\n".join(r["lines"]) or "no matches"
            elif call.name == "read":
                r = provider.read(
                    call.arguments["path"],
                    call.arguments.get("start"),
                    call.arguments.get("end"),
                )
                output = r.get("error") or "\n".join(r["lines"]) or "(empty)"
            elif call.name == "analyse":
                entries = provider.analyse(
                    call.arguments["path"],
                    call.arguments.get("pattern"),
                )
                output = format_analyse_tree(entries)
            else:
                output = f"Unknown tool: {call.name}"

            results.append(format_tool_result(call.name, call.arguments, output))

        # Feed results back
        messages.append(
            {"role": "user", "content": "\n".join(results) + format_turn_message(turn)}
        )

    return []  # No results if we exhaust turns without finish


# Tool function for gptme
def warp_grep(query: str, repo_root: str = ".") -> str:
    """
    Search a codebase using Morph's warp-grep agentic search.

    Args:
        query: Natural language query describing what code to find
        repo_root: Root directory of the repository to search (default: current dir)

    Returns:
        Formatted string with relevant code snippets
    """
    try:
        results = warp_grep_search(query, repo_root)
        if not results:
            return "No relevant code found for the query."

        output_parts = []
        for file in results:
            output_parts.append(f"### {file.path}\n```\n{file.content}\n```")

        return "\n\n".join(output_parts)
    except ValueError as e:
        return f"Error: {e}"
    except httpx.HTTPStatusError as e:
        return f"API Error: {e.response.status_code} - {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


# Tool specification for gptme
TOOL_INSTRUCTIONS = """
Search codebases using Morph's warp-grep agentic search.

Warp Grep uses AI to intelligently explore code repositories over multiple turns,
finding relevant code snippets for natural language queries.

Requires: MORPH_API_KEY environment variable (get one at https://morphllm.com/dashboard)

### Examples

Search for authentication code:
```ipython
warp_grep("Find where authentication tokens are validated")
```

Search in a specific directory:
```ipython
warp_grep("Find all API endpoints", "/path/to/project")
```

Find error handling patterns:
```ipython
warp_grep("How are database connection errors handled?")
```
"""

warp_grep_tool = ToolSpec(
    name="warp_grep",
    desc="Agentic code search using Morph's warp-grep",
    instructions=TOOL_INSTRUCTIONS,
    functions=[warp_grep],
)
