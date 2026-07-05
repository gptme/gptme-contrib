"""
Attention-based context routing with HOT/WARM/COLD tiers.

Implements dynamic context management inspired by Claude Cognitive:
- HOT tier (score >= 0.8): Full content included
- WARM tier (0.25 <= score < 0.8): Header/summary only
- COLD tier (score < 0.25): Excluded from context

Features:
- Decay: Attention scores decay each turn when not activated
- Co-activation: Related files can boost each other's scores
- Keywords: Files activate to HOT tier when keywords match
- Cache-aware: Batches updates and flushes on cache invalidation
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.tools.base import ToolSpec

if TYPE_CHECKING:
    from gptme.logmanager import LogManager

logger = logging.getLogger(__name__)

# Default thresholds
HOT_THRESHOLD = 0.8
WARM_THRESHOLD = 0.25
DEFAULT_DECAY_RATE = 0.75
COACTIVATION_BOOST = 0.35
DEFAULT_MAX_WARM_LINES = 30
DEFAULT_BATCH_SIZE = 10  # Flush after N turns if no cache invalidation

# State file location
STATE_FILE = Path(".gptme/attention_state.json")


@dataclass
class AttentionState:
    """Tracks attention scores for files/lessons."""

    scores: dict[str, float] = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    coactivation: dict[str, list[str]] = field(default_factory=dict)
    decay_rates: dict[str, float] = field(default_factory=dict)
    pinned: set[str] = field(default_factory=set)
    turn_count: int = 0
    # Batched update tracking
    pending_turns: int = 0  # Turns since last flush
    pending_keyword_matches: list[str] = field(default_factory=list)  # Paths matched

    def to_dict(self) -> dict:
        """Convert to serializable dict."""
        return {
            "scores": self.scores,
            "keywords": self.keywords,
            "coactivation": self.coactivation,
            "decay_rates": self.decay_rates,
            "pinned": list(self.pinned),
            "turn_count": self.turn_count,
            "pending_turns": self.pending_turns,
            "pending_keyword_matches": self.pending_keyword_matches,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AttentionState:
        """Create from dict."""
        return cls(
            scores=data.get("scores", {}),
            keywords=data.get("keywords", {}),
            coactivation=data.get("coactivation", {}),
            decay_rates=data.get("decay_rates", {}),
            pinned=set(data.get("pinned", [])),
            turn_count=data.get("turn_count", 0),
            pending_turns=data.get("pending_turns", 0),
            pending_keyword_matches=data.get("pending_keyword_matches", []),
        )


# Global state (persisted to file)
_state: AttentionState | None = None


def _get_state() -> AttentionState:
    """Get or load attention state."""
    global _state
    if _state is None:
        _state = _load_state()
    return _state


def _load_state() -> AttentionState:
    """Load state from file or create new."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            return AttentionState.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load attention state: {e}")
    return AttentionState()


def _save_state() -> None:
    """Persist state to file."""
    state = _get_state()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state.to_dict(), f, indent=2)


def register_file(
    path: str,
    keywords: list[str] | None = None,
    coactivate_with: list[str] | None = None,
    decay_rate: float | None = None,
    pinned: bool = False,
    initial_score: float = 0.5,
) -> str:
    """
    Register a file for attention tracking.

    Args:
        path: File path to track
        keywords: Keywords that activate this file to HOT tier
        coactivate_with: Other files that boost this file's score
        decay_rate: Custom decay rate (default: 0.75)
        pinned: If True, never falls below WARM tier
        initial_score: Starting attention score (default: 0.5)

    Returns:
        Status message

    Example:
        register_file(
            "lessons/workflow/git-workflow.md",
            keywords=["git", "commit", "branch", "push"],
            coactivate_with=["lessons/workflow/git-worktree-workflow.md"],
            pinned=True
        )
    """
    state = _get_state()
    state.scores[path] = initial_score
    if keywords:
        state.keywords[path] = keywords
    if coactivate_with:
        state.coactivation[path] = coactivate_with
    if decay_rate is not None:
        state.decay_rates[path] = decay_rate
    if pinned:
        state.pinned.add(path)
    _save_state()
    return f"Registered '{path}' with score {initial_score}, keywords={keywords}, pinned={pinned}"


def unregister_file(path: str) -> str:
    """
    Remove a file from attention tracking.

    Args:
        path: File path to remove

    Returns:
        Status message
    """
    state = _get_state()
    removed = False
    if path in state.scores:
        del state.scores[path]
        removed = True
    if path in state.keywords:
        del state.keywords[path]
    if path in state.coactivation:
        del state.coactivation[path]
    if path in state.decay_rates:
        del state.decay_rates[path]
    state.pinned.discard(path)
    _save_state()
    return f"Removed '{path}' from tracking" if removed else f"'{path}' was not tracked"


def _apply_batch_update() -> dict:
    """
    Apply all pending updates (decay + keyword activations).

    Called when cache is invalidated or batch threshold reached.

    Returns:
        Dict with update details
    """
    state = _get_state()

    if state.pending_turns == 0:
        return {"status": "no_pending_updates"}

    activated = []
    decayed = []

    # Step 1: Apply decay for all pending turns
    for path in list(state.scores.keys()):
        old_score = state.scores[path]
        decay_rate = state.decay_rates.get(path, DEFAULT_DECAY_RATE)
        # Apply decay for all pending turns at once
        new_score = old_score * (decay_rate**state.pending_turns)

        # Pinned files never go below WARM
        if path in state.pinned:
            new_score = max(new_score, WARM_THRESHOLD)

        state.scores[path] = new_score
        if old_score > new_score:
            decayed.append(
                {"path": path, "old": round(old_score, 3), "new": round(new_score, 3)}
            )

    # Step 2: Activate by recorded keyword matches (most recent wins)
    unique_matches = list(set(state.pending_keyword_matches))
    for path in unique_matches:
        state.scores[path] = 1.0
        activated.append(path)

    # Step 3: Apply co-activation for activated files
    for path in activated:
        if path in state.coactivation:
            for related_path in state.coactivation[path]:
                if related_path in state.scores:
                    boosted = min(1.0, state.scores[related_path] + COACTIVATION_BOOST)
                    state.scores[related_path] = boosted

    # Clear pending state
    turns_processed = state.pending_turns
    state.pending_turns = 0
    state.pending_keyword_matches = []

    _save_state()

    tiers = get_tiers()

    return {
        "status": "flushed",
        "turns_processed": turns_processed,
        "activated": activated,
        "decayed_count": len(decayed),
        "tiers": tiers,
    }


def process_turn(message: str, apply_now: bool = False) -> dict:
    """
    Process a conversation turn - track keyword matches for batched update.

    By default, updates are deferred until cache invalidation or batch threshold.
    Use apply_now=True to immediately apply updates (useful for testing).

    When cache_awareness module is available, uses its turn tracking for more
    accurate batching decisions synchronized with actual cache state.

    Args:
        message: The user message text to check for keyword matches
        apply_now: If True, immediately apply updates instead of deferring

    Returns:
        Dict with activated files, decayed files, and tier assignments

    Example:
        result = process_turn("How do I commit changes with git?")
        # Files with "git" or "commit" keywords will be queued for activation
    """
    state = _get_state()
    state.turn_count += 1
    state.pending_turns += 1

    message_lower = message.lower()

    # Track keyword matches (don't apply yet)
    for path, keywords in state.keywords.items():
        if any(kw.lower() in message_lower for kw in keywords):
            state.pending_keyword_matches.append(path)

    _save_state()

    # Check if we should flush
    should_flush = apply_now

    if not should_flush:
        # Try to use cache_awareness for smarter batching decisions
        if _cache_awareness_available:
            try:
                turns = get_turns_since_invalidation()
                # Use cache_awareness turns if actively tracking (> 0),
                # otherwise fall back to internal counter (e.g. in tests
                # or when no conversation is running)
                effective_turns = turns if turns > 0 else state.pending_turns
                should_flush = effective_turns >= DEFAULT_BATCH_SIZE
            except Exception:
                # Fall back to internal counter
                should_flush = state.pending_turns >= DEFAULT_BATCH_SIZE
        else:
            # No cache_awareness, use internal counter
            should_flush = state.pending_turns >= DEFAULT_BATCH_SIZE

    if should_flush:
        return _apply_batch_update()

    # Return deferred status
    return {
        "status": "deferred",
        "turn": state.turn_count,
        "pending_turns": state.pending_turns,
        "pending_matches": list(set(state.pending_keyword_matches)),
    }


def flush_pending_updates() -> dict:
    """
    Flush all pending updates immediately.

    Called by the CACHE_INVALIDATED hook to apply batched updates
    at the optimal time (when cache invalidation happens anyway).

    Returns:
        Dict with update details
    """
    return _apply_batch_update()


def get_tiers() -> dict:
    """
    Get current tier assignments for all tracked files.

    Returns:
        Dict with HOT, WARM, and COLD lists

    Example:
        tiers = get_tiers()
        hot_files = tiers["HOT"]  # Files to include fully
        warm_files = tiers["WARM"]  # Files to include headers only
    """
    state = _get_state()
    hot = []
    warm = []
    cold = []

    for path, score in state.scores.items():
        entry = {"path": path, "score": round(score, 3)}
        if score >= HOT_THRESHOLD:
            hot.append(entry)
        elif score >= WARM_THRESHOLD:
            warm.append(entry)
        else:
            cold.append(entry)

    # Sort by score descending
    hot.sort(key=lambda x: x["score"], reverse=True)
    warm.sort(key=lambda x: x["score"], reverse=True)
    cold.sort(key=lambda x: x["score"], reverse=True)

    return {"HOT": hot, "WARM": warm, "COLD": cold}


def get_score(path: str) -> float | None:
    """
    Get current attention score for a file.

    Args:
        path: File path to check

    Returns:
        Score (0.0-1.0) or None if not tracked
    """
    state = _get_state()
    return state.scores.get(path)


def set_score(path: str, score: float) -> str:
    """
    Manually set attention score for a file.

    Args:
        path: File path
        score: New score (0.0-1.0)

    Returns:
        Status message
    """
    if not 0 <= score <= 1:
        return f"Error: Score must be between 0.0 and 1.0, got {score}"

    state = _get_state()
    if path not in state.scores:
        return f"Error: '{path}' is not tracked. Use register_file first."

    old_score = state.scores[path]
    state.scores[path] = score
    _save_state()
    return f"Set '{path}' score: {old_score:.3f} → {score:.3f}"


def get_context_recommendation(max_hot: int = 10, max_warm: int = 20) -> dict:
    """
    Get recommended files to include in context based on attention scores.

    Args:
        max_hot: Maximum HOT tier files to include (default: 10)
        max_warm: Maximum WARM tier files to include (default: 20)

    Returns:
        Dict with files to include fully (HOT) and partially (WARM)
    """
    tiers = get_tiers()

    hot_files = [entry["path"] for entry in tiers["HOT"][:max_hot]]
    warm_files = [entry["path"] for entry in tiers["WARM"][:max_warm]]

    return {
        "include_full": hot_files,
        "include_header": warm_files,
        "excluded_count": len(tiers["COLD"]),
        "total_tracked": len(_get_state().scores),
    }


def extract_header(path: str, max_lines: int = DEFAULT_MAX_WARM_LINES) -> str | None:
    """
    Extract header/summary from a file (first N lines).

    Used for WARM tier context loading.

    Args:
        path: File path to read
        max_lines: Maximum lines to extract (default: 30)

    Returns:
        Header content or None if file not found
    """
    try:
        file_path = Path(path)
        if not file_path.exists():
            return None

        with open(file_path, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line)

        content = "".join(lines)
        if len(lines) == max_lines:
            content += f"\n... [truncated, showing first {max_lines} lines] ..."

        return content
    except Exception as e:
        logger.warning(f"Failed to extract header from {path}: {e}")
        return None


def get_status() -> dict:
    """
    Get full status of the attention router.

    Returns:
        Complete state including scores, configuration, and statistics
    """
    state = _get_state()
    tiers = get_tiers()

    return {
        "turn_count": state.turn_count,
        "total_tracked": len(state.scores),
        "pinned_count": len(state.pinned),
        "pending_turns": state.pending_turns,
        "pending_matches": list(set(state.pending_keyword_matches)),
        "tier_counts": {
            "HOT": len(tiers["HOT"]),
            "WARM": len(tiers["WARM"]),
            "COLD": len(tiers["COLD"]),
        },
        "thresholds": {
            "HOT": HOT_THRESHOLD,
            "WARM": WARM_THRESHOLD,
        },
        "default_decay_rate": DEFAULT_DECAY_RATE,
        "batch_size": DEFAULT_BATCH_SIZE,
    }


def reset_state() -> str:
    """
    Reset all attention state to defaults.

    Returns:
        Status message
    """
    global _state
    _state = AttentionState()
    _save_state()
    return "Attention state reset"


# CACHE_INVALIDATED hook implementation
def cache_invalidated_hook(
    manager: LogManager,
    reason: str,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
) -> Generator[Message, None, None]:
    """
    Hook called when prompt cache is invalidated.

    This is the optimal time to flush batched attention updates since
    the cache will be regenerated anyway. No extra cache invalidation cost.

    Args:
        manager: Conversation manager with log and workspace
        reason: Reason for cache invalidation (e.g., "compact")
        tokens_before: Token count before operation
        tokens_after: Token count after operation
    """
    state = _get_state()

    # Only flush if there are pending updates
    if state.pending_turns == 0:
        return

    logger.info(
        f"Attention router: flushing {state.pending_turns} pending turns "
        f"on cache invalidation (reason: {reason})"
    )

    result = flush_pending_updates()

    # Log the tier changes if significant
    if result.get("activated"):
        logger.info(f"Attention router: activated {result['activated']}")

    # Optionally yield a hidden message with tier update info
    # This can be useful for debugging but is hidden from the conversation
    if result.get("status") == "flushed":
        yield Message(
            "system",
            f"🎯 Attention router updated ({result['turns_processed']} turns): "
            f"{len(result.get('activated', []))} activated, "
            f"{result.get('decayed_count', 0)} decayed. "
            f"Tiers: HOT={len(result['tiers']['HOT'])}, "
            f"WARM={len(result['tiers']['WARM'])}, "
            f"COLD={len(result['tiers']['COLD'])}",
            hide=True,
        )


# Try to register with cache_awareness module (gptme PR #1074)
# Falls back to direct CACHE_INVALIDATED hook registration if not available
_cache_awareness_available = False

try:
    from gptme.hooks.cache_awareness import (
        get_turns_since_invalidation,
        on_cache_change,
    )

    def _on_cache_invalidated(cache_state) -> None:
        """Callback when cache is invalidated via cache_awareness module."""
        current = _get_state()

        # Only flush if there are pending updates
        if current.pending_turns == 0:
            return

        logger.info(
            f"Attention router: flushing {current.pending_turns} pending turns "
            f"on cache invalidation via cache_awareness"
        )

        result = flush_pending_updates()

        # Log the tier changes if significant
        if result.get("activated"):
            logger.info(f"Attention router: activated {result['activated']}")

    # Register callback with cache_awareness module
    _unsubscribe = on_cache_change(_on_cache_invalidated)
    _cache_awareness_available = True
    logger.info("Registered attention_router with cache_awareness module")

except ImportError:
    logger.debug(
        "cache_awareness module not available, trying direct hook registration"
    )

    # Fallback: Try direct CACHE_INVALIDATED hook registration
    try:
        from gptme.hooks import HookType, register_hook

        register_hook(
            name="attention_router_cache_invalidated",
            hook_type=HookType.CACHE_INVALIDATED,
            func=cache_invalidated_hook,
            priority=50,  # Run before other cache-related hooks
        )
        logger.info("Registered attention_router CACHE_INVALIDATED hook (fallback)")
    except ImportError:
        logger.debug("gptme.hooks not available, no cache hook registered")
    except Exception as e:
        logger.warning(f"Failed to register CACHE_INVALIDATED hook: {e}")

except Exception as e:
    logger.warning(f"Failed to register with cache_awareness: {e}")


# Tool specification for gptme
tool = ToolSpec(
    name="attention_router",
    desc="Attention-based context routing with HOT/WARM/COLD tiers",
    instructions="""
Use this tool to manage dynamic context loading based on attention scores.

**Tier System:**
- HOT (score ≥ 0.8): Full file content included in context
- WARM (0.25 ≤ score < 0.8): Only header/first 30 lines included
- COLD (score < 0.25): Excluded from context

**Features:**
- **Decay**: Scores decay each turn (default 0.75×), so unused files fade
- **Keyword Activation**: Files activate to HOT when keywords match
- **Co-activation**: Related files can boost each other's scores
- **Pinning**: Critical files can be pinned to never fall below WARM
- **Cache-aware batching**: Updates are batched and applied on cache invalidation

**Cache Integration:**
Updates are now batched and applied when the prompt cache is invalidated
(e.g., during auto-compaction). This avoids unnecessary cache invalidation
while still maintaining fresh tier assignments.

**Usage Pattern:**

1. Register files you want to track:
```python
register_file(
    "lessons/workflow/git-workflow.md",
    keywords=["git", "commit", "branch"],
    pinned=True
)
```

2. Process each turn to track keyword matches (deferred by default):
```python
result = process_turn("How do I commit with git?")
# result["status"] == "deferred" - updates queued for later
```

3. Updates automatically flush on cache invalidation, or manually:
```python
flush_pending_updates()  # Apply all pending updates now
```

4. Get context recommendations:
```python
rec = get_context_recommendation()
# rec["include_full"] = HOT files to load completely
# rec["include_header"] = WARM files to load headers only
```

**Token Savings:**
- Full files at 2-5k tokens each
- Headers at 200-500 tokens each
- Expected 64-95% token reduction depending on codebase
    """,
    examples="""
### Register Files for Tracking

> User: Set up attention tracking for my workflow lessons
> Assistant: I'll register the workflow lessons with appropriate keywords.
```ipython
register_file(
    "lessons/workflow/git-workflow.md",
    keywords=["git", "commit", "branch", "push", "pull"],
    coactivate_with=["lessons/workflow/git-worktree-workflow.md"],
    pinned=True
)
```

### Process a Turn (Batched)

> User: How do I check my git status?
> Assistant: Let me process this turn to update attention scores.
```ipython
result = process_turn("How do I check my git status?")
```
> System: {"status": "deferred", "turn": 5, "pending_turns": 1, "pending_matches": ["lessons/workflow/git-workflow.md"]}

### Manually Flush Updates

> User: Apply all pending attention updates now
> Assistant: I'll flush the pending updates.
```ipython
result = flush_pending_updates()
```
> System: {"status": "flushed", "turns_processed": 3, "activated": [...], "decayed_count": 12, "tiers": {...}}

### Get Current Tiers

> User: What files are currently hot?
> Assistant: I'll check the current tier assignments.
```ipython
tiers = get_tiers()
print("HOT files:", [f["path"] for f in tiers["HOT"]])
print("WARM files:", [f["path"] for f in tiers["WARM"]])
```

### Get Context Recommendations

> User: What should be included in context right now?
> Assistant: Let me get the current recommendations.
```ipython
rec = get_context_recommendation(max_hot=5, max_warm=10)
```
    """,
    functions=[
        register_file,
        unregister_file,
        process_turn,
        flush_pending_updates,
        get_tiers,
        get_score,
        set_score,
        get_context_recommendation,
        extract_header,
        get_status,
        reset_state,
    ],
)
