"""Backend + model selection from a workspace-local candidate list.

Implements :func:`select_backend` which reads a ``harness-quota.toml``
config, consults the block registry, and returns the first viable
``(backend, model)`` pair from the configured candidate list.

Config format (``harness-quota.toml``)
---------------------------------------
::

    # Optional: override per-workspace (default: ~/.config/gptme/harness-quota.toml)
    [[candidates]]
    backend  = "claude-code"
    model    = "claude-sonnet-4-6"
    priority = 1

    [[candidates]]
    backend  = "gptme"
    model    = "openrouter/deepseek/deepseek-chat-v3-0324:free"
    priority = 2

Candidates are tried in ascending ``priority`` order (lowest number first).
A candidate is skipped when:
  - the backend binary is not found in PATH (hard gate), or
  - the block registry holds a still-active block for that backend/model combo.

Stdlib-only (``tomllib`` ≥ 3.11; ``tomli`` fallback for 3.10).
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

DEFAULT_BACKEND_BINARIES: dict[str, str] = {
    "gptme": "gptme",
    "claude-code": "claude",
    "codex": "codex",
    "grok-build": "grok-build",
}


@dataclass(frozen=True)
class Candidate:
    """One arm in the candidate list."""

    backend: str
    model: str
    priority: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"backend": self.backend, "model": self.model, "priority": self.priority}


@dataclass
class SelectConfig:
    """Parsed ``harness-quota.toml`` content."""

    candidates: list[Candidate] = field(default_factory=list)

    @property
    def ordered(self) -> list[Candidate]:
        return sorted(self.candidates, key=lambda c: c.priority)


@dataclass(frozen=True)
class SelectResult:
    """Output of :func:`select_backend`."""

    backend: str
    model: str
    candidates: list[Candidate]

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model,
            "candidates": [c.to_dict() for c in self.candidates],
        }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "gptme" / "harness-quota.toml",
]


def load_select_config(path: Path | None = None) -> SelectConfig:
    """Load candidate list from ``harness-quota.toml``.

    Search order:
    1. ``path`` if provided.
    2. ``~/.config/gptme/harness-quota.toml``.

    Returns an empty :class:`SelectConfig` (no candidates) if no file is found.
    """
    search = [path] if path else _DEFAULT_CONFIG_PATHS
    for candidate_path in search:
        if candidate_path and candidate_path.exists():
            with candidate_path.open("rb") as f:
                raw = tomllib.load(f)
            return _parse_config(raw)
    return SelectConfig()


def _parse_config(raw: dict) -> SelectConfig:
    candidates: list[Candidate] = []
    for i, entry in enumerate(raw.get("candidates", [])):
        backend = entry.get("backend", "")
        model = entry.get("model", "")
        priority = int(entry.get("priority", i))
        if backend and model:
            candidates.append(
                Candidate(backend=backend, model=model, priority=priority)
            )
    return SelectConfig(candidates=candidates)


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------


def _backend_binary(backend: str) -> str:
    return DEFAULT_BACKEND_BINARIES.get(backend, backend)


def _binary_available(backend: str) -> bool:
    return shutil.which(_backend_binary(backend)) is not None


def _is_block_active(state_dir: Path, backend: str, model: str) -> bool:
    """Return True when the block registry has an active block for this arm."""
    try:
        from gptme_block_registry.contract import (
            ARM_BLOCK_KINDS,
            arm_block_path,
            backend_block_path,
        )
        from gptme_block_registry.writers import read_block_until
    except ImportError:
        # Fail closed when *block-shaped* state exists but cannot be read
        # (broken registry install): unverified arms must not be selected.
        # Match the registry's block-file naming — every kind ends in
        # "-until.txt" — rather than "any file present": a state dir holding
        # only incidental files (a README, a lock, a temp file) cannot encode
        # a block, and must not block every candidate.
        # No state dir at all means no blocks can exist — fail open.
        if state_dir.is_dir() and any(
            p.is_file() for p in state_dir.glob("*-until.txt")
        ):
            return True
        return False

    now = datetime.now(tz=timezone.utc)

    # Backend-level block (all arms on this backend)
    backend_block = backend_block_path(state_dir, backend)
    until = read_block_until(backend_block)
    if until is not None and until > now:
        return True

    # Arm-level block (specific backend+model)
    for kind in ARM_BLOCK_KINDS:
        arm_path = arm_block_path(state_dir, backend, model, kind)
        until = read_block_until(arm_path)
        if until is not None and until > now:
            return True

    return False


# ---------------------------------------------------------------------------
# Main selector
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "gptme" / "block-registry"


def select_backend(
    config: SelectConfig | None = None,
    config_path: Path | None = None,
    state_dir: Path | None = None,
) -> SelectResult | None:
    """Pick the first viable backend+model from the candidate list.

    A candidate is viable when:
    - its backend binary is found in PATH, and
    - the block registry has no active block for that backend/model.

    Parameters
    ----------
    config:
        Pre-loaded :class:`SelectConfig`. When *None*, loaded from disk via
        :func:`load_select_config`.
    config_path:
        Path to ``harness-quota.toml`` passed through to :func:`load_select_config`
        when ``config`` is *None*.
    state_dir:
        Block-registry state directory.
        Defaults to ``~/.local/share/gptme/block-registry``.

    Returns
    -------
    :class:`SelectResult` with the chosen backend/model and the full evaluated
    candidate list, or *None* when every candidate is blocked/unavailable.
    """
    if config is None:
        config = load_select_config(config_path)
    if state_dir is None:
        state_dir = _DEFAULT_STATE_DIR

    for candidate in config.ordered:
        if not _binary_available(candidate.backend):
            continue
        if _is_block_active(state_dir, candidate.backend, candidate.model):
            continue
        return SelectResult(
            backend=candidate.backend,
            model=candidate.model,
            candidates=config.ordered,
        )
    return None
