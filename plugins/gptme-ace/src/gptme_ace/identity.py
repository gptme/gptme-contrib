"""Workspace and agent identity resolution for gptme-ace.

gptme-ace ships as a generic plugin, so no module should assume the agent is
Bob. Workspace resolution mirrors ``gptme_coordination`` so a fork resolves the
same workspace root everywhere:

1. Workspace env vars (absolute paths only): ``BOB_WORKSPACE``,
   ``AGENT_WORKSPACE``, or the legacy ``BOB_BRAIN_ROOT`` alias.
2. An agent repo detected by walking up from this file for a directory that
   contains both ``gptme.toml`` and ``gptme-contrib/``.
3. ``$HOME/<home-name>`` (e.g. ``/home/bob`` -> ``/home/bob/bob``). It is
   returned even when the directory does not exist yet, so insight storage
   always has a dedicated root instead of landing in the home-directory root
   (which mixes with dotfiles and can be unwritable on restricted homes). This
   is the one deliberate divergence from ``gptme_coordination``, which returns
   ``$HOME`` itself in that case for its ledger writes. For Bob this is
   identical to the previous ``Path.home() / "bob"`` default.

The agent display name resolves from ``AGENT_NAME``, then ``[agent].name`` in
the workspace ``gptme.toml``, then the historical default ``"Bob"``. Bob's
deployment is unchanged: its ``gptme.toml`` sets ``[agent].name = "Bob"`` and
its workspace resolves to ``/home/bob/bob``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

#: Env vars checked for the workspace root, in precedence order. Matches
#: ``gptme_coordination`` (legacy ``BOB_WORKSPACE`` first, neutral
#: ``AGENT_WORKSPACE`` next, then the legacy ``BOB_BRAIN_ROOT`` alias).
WORKSPACE_ENV_VARS = ("BOB_WORKSPACE", "AGENT_WORKSPACE", "BOB_BRAIN_ROOT")

#: Historical agent name used when nothing else is configured.
DEFAULT_AGENT_NAME = "Bob"


def workspace_from_env(environ: Mapping[str, str] | None = None) -> Path | None:
    """Return the workspace root from the agent env vars, if one is set.

    Only absolute values are accepted: a relative or ``~`` value would resolve
    against whatever cwd happens to be and could redirect insight storage into
    a checkout subdirectory.
    """
    environment = os.environ if environ is None else environ
    for var in WORKSPACE_ENV_VARS:
        value = environment.get(var)
        if not value:
            continue
        path = Path(value)
        if path.is_absolute():
            return path
    return None


def detect_agent_repo(start: Path | str | None = None) -> Path | None:
    """Walk up from *start* (default: this file) for an agent repo root.

    An agent repo root holds both a ``gptme.toml`` and a ``gptme-contrib/``
    directory - the layout these packages ship in. Works for editable installs
    (the common case) and returns ``None`` for a wheel install, which then
    falls back to the env vars or the home-directory default.
    """
    base = Path(start).resolve() if start is not None else Path(__file__).resolve()
    for parent in (base, *base.parents):
        if (parent / "gptme.toml").is_file() and (parent / "gptme-contrib").is_dir():
            return parent
    return None


def default_workspace_root(
    environ: Mapping[str, str] | None = None,
    start: Path | str | None = None,
) -> Path:
    """Resolve the workspace root used for insight storage.

    Resolution order is documented at module level. Never raises.
    """
    from_env = workspace_from_env(environ)
    if from_env is not None:
        return from_env

    detected = detect_agent_repo(start)
    if detected is not None:
        return detected

    home = Path.home()
    return home / home.name


def _agent_name_from_config(workspace: Path | str | None) -> str | None:
    """Read ``[agent].name`` from the workspace ``gptme.toml``, if present.

    Uses gptme's canonical project-config loader (same as ``gptme-voice``), so
    ``.github/gptme.toml`` and local overrides are honored. The import is lazy
    and guarded: ``generator.py`` is runnable as a standalone PEP 723 script
    without gptme installed, and must keep working there.
    """
    if workspace is None:
        return None
    try:
        from gptme.config import get_project_config
    except Exception:  # pragma: no cover - gptme missing (standalone script)
        return None

    config = get_project_config(Path(workspace), quiet=True)
    if config is None:
        return None
    name = getattr(getattr(config, "agent", None), "name", None)
    return str(name) if name else None


def agent_name(
    workspace: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the agent's display name.

    ``AGENT_NAME`` wins (the convention used across gptme-contrib), then
    ``[agent].name`` from the workspace ``gptme.toml``, then ``"Bob"``.
    """
    environment = os.environ if environ is None else environ
    from_env = environment.get("AGENT_NAME")
    if from_env:
        return from_env

    from_config = _agent_name_from_config(workspace)
    if from_config:
        return from_config

    return DEFAULT_AGENT_NAME
