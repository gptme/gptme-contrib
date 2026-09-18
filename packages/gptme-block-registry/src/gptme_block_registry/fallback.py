"""Config-driven fallback arm for auth-death scenarios.

When the per-slot circuit breaker opens due to repeated auth deaths, the normal
spawn is suppressed. This module lets a caller recover gracefully by falling
back to a *configured* cheap arm — typically an OpenRouter model — rather than
waiting silently for a human.

Design
------
*   :class:`FallbackArm` names the alternative backend/model and the scope it
    may operate in. The ``scope`` field is a contract between the agent and its
    runner: ``"lights-on"`` means standups, monitoring, escalation, and small
    mechanical fixes only — no arc work, no self-modifying control logic.
*   :class:`FallbackConfig` holds one optional arm per trigger type. Currently
    only ``on_auth_death`` is defined; the shape is extensible.
*   :func:`decide_fallback` takes the output of :func:`decide_respawn` and
    returns either ``None`` (no fallback — proceed normally or suppress) or a
    :class:`FallbackDecision` the runner can use to spawn a survival session.

Separation of concerns
-----------------------
This module knows nothing about how to *spawn* the fallback. The caller is
responsible for translating a :class:`FallbackDecision` into a concrete command
(e.g. ``gptme -n --model openrouter/deepseek/...``). This keeps the logic
portable and testable without a running agent.

Session labelling
-----------------
``FallbackDecision.label`` must be embedded in every session record spawned
under the fallback arm. This is how post-hoc analysis distinguishes cheap
survival sessions from normal production work — and how the outage stays
*visible* even when the loop keeps running.

Config format (TOML)
---------------------
::

    [on_auth_death]
    backend = "gptme"
    model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"
    scope   = "lights-on"
    label   = "fallback:auth-death"
    note    = "Cheap OpenRouter arm — Claude credential down"

Load it with :func:`load_fallback_config`.

Tier 0 — stdlib only (``tomllib`` ≥ 3.11; ``tomli`` fallback for 3.10).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------

LIGHTS_ON_SCOPE = "lights-on"
"""Allowed scope value for survival-mode sessions.

A fallback arm with this scope may only run categories that keep the agent
alive without touching arc work or self-modifying logic:
standups, monitoring, escalation, small mechanical fixes.
"""

#: Categories allowed when scope == LIGHTS_ON_SCOPE.
LIGHTS_ON_CATEGORIES: frozenset[str] = frozenset(
    {
        "standup",
        "monitoring",
        "escalation",
        "cleanup",
        "code",  # small mechanical fixes only
        "infrastructure",  # health checks, service restarts
    }
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FallbackArm:
    """A concrete alternative arm to spawn when the primary arm is suppressed.

    Parameters
    ----------
    backend:
        Runtime backend identifier (e.g. ``"gptme"``, ``"claude-code"``).
    model:
        Full model slug the backend should use (e.g.
        ``"openrouter/deepseek/deepseek-chat-v3-0324:free"``).
    scope:
        Operational scope restriction. Use :data:`LIGHTS_ON_SCOPE` for the
        standard survival-mode contract.
    label:
        Short tag stamped on every session record spawned under this arm.
        Keeps fallback sessions auditable.
    note:
        Optional human-readable description; not machine-interpreted.
    """

    backend: str
    model: str
    scope: str
    label: str
    note: str = ""

    def allowed_categories(self) -> frozenset[str] | None:
        """Return the set of allowed session categories, or None for unrestricted."""
        if self.scope == LIGHTS_ON_SCOPE:
            return LIGHTS_ON_CATEGORIES
        return None

    def is_category_allowed(self, category: str) -> bool:
        """Return True when *category* is permitted under this arm's scope."""
        allowed = self.allowed_categories()
        if allowed is None:
            return True
        return category in allowed


@dataclass(frozen=True)
class FallbackConfig:
    """Fallback arms keyed by trigger type.

    Attributes
    ----------
    on_auth_death:
        Arm to use when the per-slot circuit breaker opens due to auth deaths.
        ``None`` means no fallback — the runner simply suppresses the spawn
        and waits.
    """

    on_auth_death: FallbackArm | None = field(default=None)


@dataclass(frozen=True)
class FallbackDecision:
    """A resolved decision to use a fallback arm.

    Returned by :func:`decide_fallback` when the circuit is open and a fallback
    is configured. The caller uses ``arm`` to construct the spawn command and
    stamps ``arm.label`` on the session record.

    Parameters
    ----------
    arm:
        The configured fallback arm to use.
    triggered_by:
        Short identifier for the trigger that fired (e.g. ``"auth_death"``).
    """

    arm: FallbackArm
    triggered_by: str


# ---------------------------------------------------------------------------
# Decision function
# ---------------------------------------------------------------------------


def decide_fallback(
    *,
    suppress: bool,
    breaker_state: str,
    config: FallbackConfig,
) -> FallbackDecision | None:
    """Decide whether to activate the fallback arm.

    Call this *after* :func:`~gptme_block_registry.slot_circuit_breaker.decide_respawn`
    with its outputs to determine whether a fallback session should be spawned.

    Parameters
    ----------
    suppress:
        The first return value of :func:`decide_respawn` — ``True`` when the
        circuit breaker has suppressed the normal spawn.
    breaker_state:
        The second return value of :func:`decide_respawn` — the post-decision
        circuit state name (e.g. ``"OPEN"``, ``"CLOSED"``, ``"HALF_OPEN"``).
    config:
        The loaded fallback configuration.

    Returns
    -------
    :class:`FallbackDecision` when a fallback should be spawned, ``None`` otherwise.

    Fallback fires when **all** of:
    - ``suppress`` is ``True`` (normal spawn is blocked), AND
    - ``breaker_state == "OPEN"`` (the blocker is auth-death, not an
      unrelated suppression), AND
    - ``config.on_auth_death`` is not ``None``.

    HALF_OPEN suppression is deliberately excluded: it means we already sent a
    probe; a fallback there would overlap the probe and produce misleading signal.
    """
    if not suppress:
        return None
    if breaker_state != "OPEN":
        return None
    if config.on_auth_death is None:
        return None
    return FallbackDecision(arm=config.on_auth_death, triggered_by="auth_death")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_fallback_config(path: Path) -> FallbackConfig:
    """Load a :class:`FallbackConfig` from a TOML file.

    Missing or empty ``[on_auth_death]`` section returns a config with
    ``on_auth_death=None`` (no fallback configured) — this is the safe default
    for agents that have not yet set up a fallback arm.

    Raises :class:`ValueError` if the file is present but unparseable, or if
    required arm fields are missing.
    """
    if not path.exists():
        return FallbackConfig()

    raw = path.read_text(encoding="utf-8")

    if sys.version_info >= (3, 11):
        import tomllib  # stdlib ≥ 3.11

        data = tomllib.loads(raw)
    else:
        try:
            import tomli  # optional backport for 3.10

            data = tomli.loads(raw)
        except ImportError as exc:
            raise ImportError(
                "Python <3.11 requires the 'tomli' package to read TOML config files. "
                "Install it with: pip install tomli"
            ) from exc

    oad = data.get("on_auth_death")
    if oad is None:
        return FallbackConfig()

    if not isinstance(oad, dict):
        raise ValueError("[on_auth_death] must be a TOML table, not a scalar")

    missing = [k for k in ("backend", "model", "scope", "label") if k not in oad]
    if missing:
        raise ValueError(
            f"[on_auth_death] is missing required field(s): {', '.join(missing)}"
        )

    arm = FallbackArm(
        backend=str(oad["backend"]),
        model=str(oad["model"]),
        scope=str(oad["scope"]),
        label=str(oad["label"]),
        note=str(oad.get("note", "")),
    )
    return FallbackConfig(on_auth_death=arm)
