"""
gptme-retrieval - Automatic context retrieval plugin for gptme.

Provides a STEP_PRE hook that retrieves relevant context before each LLM step
using backends like qmd or gptme-rag for semantic/keyword search.

Configuration via gptme.toml:
```toml
[plugin.retrieval]
enabled = true
backend = "qmd"          # "qmd" (default), "gptme-rag", "grep", or custom command
mode = "vsearch"         # "search" (bm25), "vsearch" (semantic), "query" (hybrid)
max_results = 5
threshold = 0.3          # Minimum score for results
collections = []         # Optional: filter by collection names
```
"""

import logging
import shlex
import subprocess
import json
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from gptme.config import get_config
from gptme.message import Message
from gptme.tools import ToolSpec

if TYPE_CHECKING:
    from gptme.logmanager import LogManager

logger = logging.getLogger(__name__)

__all__ = ["plugin", "get_retrieval_config", "retrieve_context"]


# Default configuration
DEFAULT_CONFIG = {
    "enabled": True,
    "backend": "qmd",
    "mode": "vsearch",
    "max_results": 5,
    "threshold": 0.3,
    "collections": [],
    "inject_as": "system",  # "system" or "hidden"
}


def get_retrieval_config() -> dict[str, Any]:
    """Get retrieval configuration from gptme config.

    Reads from [plugin.retrieval] section in gptme.toml.
    Falls back to defaults if not configured.
    """
    config = get_config()
    plugin_config = {}

    # Try project config first, then user config
    if config.project and hasattr(config.project, "plugin"):
        plugin_config = getattr(config.project, "plugin", {}).get("retrieval", {})

    if not plugin_config and config.user and hasattr(config.user, "plugin"):
        plugin_config = getattr(config.user, "plugin", {}).get("retrieval", {})

    # Merge with defaults
    result = {**DEFAULT_CONFIG, **plugin_config}
    return result


def retrieve_context(
    query: str,
    backend: str = "qmd",
    mode: str = "vsearch",
    max_results: int = 5,
    threshold: float = 0.3,
    collections: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve relevant context using the specified backend.

    Args:
        query: The search query (typically the user's message)
        backend: Backend command to use ("qmd", "gptme-rag", "grep", or custom)
        mode: Search mode ("search" for BM25, "vsearch" for semantic, "query" for hybrid)
        max_results: Maximum number of results to return
        threshold: Minimum score threshold for results
        collections: Optional list of collection names to filter by

    Returns:
        List of result dictionaries with 'content', 'source', and 'score' keys
    """
    results = []

    if backend == "qmd":
        results = _retrieve_qmd(query, mode, max_results, collections)
    elif backend == "gptme-rag":
        results = _retrieve_gptme_rag(query, max_results)
    elif backend == "grep":
        results = _retrieve_grep(query, max_results)
    else:
        # Custom backend - try to execute as command
        results = _retrieve_custom(backend, query, mode, max_results)

    # Filter by threshold
    results = [r for r in results if r.get("score", 1.0) >= threshold]

    return results[:max_results]


def _retrieve_qmd(
    query: str,
    mode: str,
    max_results: int,
    collections: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve context using qmd."""
    cmd = ["qmd", mode, query, "--json", "-n", str(max_results)]

    if collections:
        for collection in collections:
            cmd.extend(["--collection", collection])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.debug(f"qmd returned non-zero: {result.stderr}")
            return []

        # Parse JSON output
        data = json.loads(result.stdout)

        # Normalize output format
        results = []
        for item in data:
            results.append(
                {
                    "content": item.get("content", ""),
                    "source": item.get("path", item.get("source", "unknown")),
                    "score": item.get("score", 1.0),
                }
            )

        return results

    except FileNotFoundError:
        logger.warning("qmd not found - retrieval disabled")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("qmd timed out")
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse qmd output: {e}")
        return []
    except Exception as e:
        logger.warning(f"qmd retrieval failed: {e}")
        return []


def _retrieve_gptme_rag(query: str, max_results: int) -> list[dict[str, Any]]:
    """Retrieve context using gptme-rag.

    Uses the gptme-rag CLI tool for retrieval.
    Note: gptme-rag is experimental and may have issues.
    """
    cmd = ["gptme-rag", "search", query, "-n", str(max_results), "--json"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            logger.debug(f"gptme-rag returned non-zero: {result.stderr}")
            return []

        # Parse JSON output
        data = json.loads(result.stdout)

        # Normalize output format
        results = []
        for item in data:
            results.append(
                {
                    "content": item.get("content", item.get("text", "")),
                    "source": item.get("path", item.get("source", "unknown")),
                    "score": item.get("score", item.get("similarity", 1.0)),
                }
            )

        return results

    except FileNotFoundError:
        logger.warning("gptme-rag not found - install with: pipx install gptme-rag")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("gptme-rag timed out")
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse gptme-rag output: {e}")
        return []
    except Exception as e:
        logger.warning(f"gptme-rag retrieval failed: {e}")
        return []


def _retrieve_grep(query: str, max_results: int) -> list[dict[str, Any]]:
    """Simple grep-based retrieval."""
    # Basic grep search - useful as fallback
    cmd = ["grep", "-r", "-l", "-i", query, "."]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        results = []
        for line in result.stdout.strip().split("\n")[:max_results]:
            if line:
                results.append(
                    {
                        "content": f"File: {line}",
                        "source": line,
                        "score": 1.0,
                    }
                )

        return results

    except Exception as e:
        logger.warning(f"grep retrieval failed: {e}")
        return []


def _retrieve_custom(
    backend: str,
    query: str,
    mode: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Execute custom backend command.

    Security note: Uses shlex.split() to safely parse the backend command
    without shell=True to prevent command injection.
    """
    # Parse backend command safely (e.g., "my-search --json" -> ["my-search", "--json"])
    try:
        base_cmd = shlex.split(backend)
    except ValueError as e:
        logger.warning(f"Invalid backend command syntax: {e}")
        return []

    # Build full command with arguments
    cmd = base_cmd + [query, "--mode", mode, "-n", str(max_results)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        data = json.loads(result.stdout)
        return data

    except FileNotFoundError:
        logger.warning(f"Custom backend command not found: {base_cmd[0]}")
        return []
    except Exception as e:
        logger.warning(f"Custom backend '{backend}' failed: {e}")
        return []


def _format_retrieval_results(results: list[dict[str, Any]]) -> str:
    """Format retrieval results for context injection."""
    if not results:
        return ""

    lines = ["## Retrieved Context\n"]

    for i, result in enumerate(results, 1):
        source = result.get("source", "unknown")
        content = result.get("content", "")
        score = result.get("score", 0)

        lines.append(f"### Result {i} (score: {score:.2f})")
        lines.append(f"**Source**: {source}\n")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


def step_pre_hook(
    manager: "LogManager",
) -> Generator[Message, None, None]:
    """Hook that retrieves relevant context before each LLM step.

    This hook fires before each step in a turn. It extracts the last
    user message and retrieves relevant context using the configured backend.
    """
    config = get_retrieval_config()

    if not config.get("enabled", True):
        return

    # Get the last user message
    last_user_msg = None
    for msg in reversed(manager.log.messages):
        if msg.role == "user":
            last_user_msg = msg
            break

    if not last_user_msg:
        return

    # Retrieve relevant context
    results = retrieve_context(
        query=last_user_msg.content,
        backend=config.get("backend", "qmd"),
        mode=config.get("mode", "vsearch"),
        max_results=config.get("max_results", 5),
        threshold=config.get("threshold", 0.3),
        collections=config.get("collections", []),
    )

    if not results:
        return

    # Format and inject results
    formatted = _format_retrieval_results(results)

    if formatted:
        inject_as = config.get("inject_as", "system")
        hide = inject_as == "hidden"

        yield Message(
            role="system",
            content=formatted,
            hide=hide,
        )


def register_hooks() -> None:
    """Register the retrieval hook with gptme."""
    from gptme.hooks import HookType, register_hook

    config = get_retrieval_config()

    if not config.get("enabled", True):
        logger.info("gptme-retrieval: Disabled by configuration")
        return

    register_hook(
        name="gptme_retrieval.step_pre",
        hook_type=HookType.STEP_PRE,
        func=step_pre_hook,
        priority=100,  # High priority - run early
    )

    logger.info(f"gptme-retrieval: Registered with backend={config.get('backend')}")


# Plugin instructions for gptme context
_instructions = """
## Retrieval Context

This plugin automatically retrieves relevant context before each LLM step.

Configuration example in gptme.toml:
```toml
[plugin.retrieval]
enabled = true
backend = "qmd"
mode = "vsearch"
max_results = 5
```

If you see "## Retrieved Context" in system messages, that's retrieved
information relevant to the current query.
"""

# Plugin specification
plugin = ToolSpec(
    name="gptme_retrieval",
    desc="Automatic context retrieval using qmd or other backends",
    instructions=_instructions,
    init=register_hooks,
)
