"""
gptme-retrieval - Automatic context retrieval plugin for gptme.

Provides a STEP_PRE hook that retrieves relevant context before each LLM step,
with per-conversation deduplication to avoid injecting the same document twice.
Uses backends like qmd or gptme-rag for semantic/keyword search.

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

import hashlib
import json
import logging
import shlex
import subprocess
from collections import OrderedDict
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from gptme.config import get_config
from gptme.message import Message
from gptme.tools import ToolSpec

if TYPE_CHECKING:
    from gptme.logmanager import LogManager

logger = logging.getLogger(__name__)

__all__ = [
    "plugin",
    "get_retrieval_config",
    "retrieve_context",
    "step_pre_hook",
    "turn_pre_hook",
    "_MAX_TRACKED_CONVS",
]


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

# Per-conversation deduplication state: maps conversation name -> set of injected doc keys.
# A doc key is "source#content_hash" — stable across multiple retrievals of the same document.
# Capped at _MAX_TRACKED_CONVS entries (FIFO eviction) to avoid unbounded growth in long-running
# processes (daemons, servers) that handle many conversations over their lifetime.
_MAX_TRACKED_CONVS = 500
_injected_per_conv: OrderedDict[str, set[str]] = OrderedDict()


def _doc_key(result: dict[str, Any]) -> str:
    """Compute a stable deduplication key for a retrieved document."""
    source = result.get("source", "")
    content = result.get("content", "")
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"{source}#{content_hash}"


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
    """Hook that retrieves relevant context before each LLM step, with deduplication.

    Fires on every LLM step (STEP_PRE) but only injects documents that haven't
    been seen in this conversation yet. This is better than TURN_PRE for autonomous
    sessions: it responds to topic changes across steps while never injecting the
    same document twice.

    Deduplication is keyed by (source, content_hash) per conversation, so a document
    retrieved on step 1 won't be re-injected on steps 2-N, but a genuinely new document
    (different source or updated content) will be injected when it first appears.

    Note: the retrieval backend is called on every step to discover newly-relevant docs
    (e.g. after a topic change). Only injection is deduplicated, not retrieval.
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

    # Deduplicate: only inject documents not already in this conversation's context.
    # Use `or "default"` to handle the case where log.name exists but is None (e.g.
    # nameless/ephemeral conversations) — without this, all such conversations would
    # share a single dedup set and silently suppress injection for each other.
    conv_name = getattr(manager.log, "name", None) or "default"
    if conv_name not in _injected_per_conv:
        _injected_per_conv[conv_name] = set()
        # Evict oldest entries when over the cap (FIFO)
        while len(_injected_per_conv) > _MAX_TRACKED_CONVS:
            _injected_per_conv.popitem(last=False)
    injected = _injected_per_conv[conv_name]

    new_results = [r for r in results if _doc_key(r) not in injected]

    if not new_results:
        return

    # Format and inject only new results
    formatted = _format_retrieval_results(new_results)

    if formatted:
        # Mark these docs as injected before yielding
        for r in new_results:
            injected.add(_doc_key(r))

        inject_as = config.get("inject_as", "system")
        hide = inject_as == "hidden"

        yield Message(
            role="system",
            content=formatted,
            hide=hide,
        )


def turn_pre_hook(
    manager: "LogManager",
) -> Generator[Message, None, None]:
    """Hook that retrieves relevant context once per turn (no deduplication).

    Fires exactly once per user message (TURN_PRE). Simpler than step_pre_hook
    but less responsive to topic changes within a multi-step turn.
    Kept for backward compatibility; step_pre_hook is preferred.
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

This plugin automatically retrieves relevant context before each LLM step (STEP_PRE hook),
injecting only documents not already present in the conversation (deduplication by source + content).

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
