"""HMAC-based sender authentication for coordination primitives.

Each agent has a per-agent secret (env var or file) used to sign messages
and work claims. Readers verify the signature using the asserted sender's
known secret. A forged sender field is detectable because the forger doesn't
hold the real agent's secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SECRETS_DIR = Path("secrets/coordination")


def compute_hmac(secret: bytes, *fields: Any) -> str:
    """HMAC-SHA256 over compact JSON-array-encoded fields, base64-encoded.

    Uses ``json.dumps(list(fields), sort_keys=True, separators=(",", ":"))``
    — the canonical encoding used by WorkClaimManager and MessageBus —
    so verify_hmac can validate signatures produced by either manager.
    Fields may be str, int, or None; types are preserved in JSON serialization.
    """
    data = json.dumps(list(fields), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.b64encode(hmac.new(secret, data, hashlib.sha256).digest()).decode(
        "ascii"
    )


def verify_hmac(secret: bytes, expected: str, *fields: Any) -> bool:
    """Verify HMAC using constant-time comparison."""
    computed = compute_hmac(secret, *fields)
    return hmac.compare_digest(computed, expected)


def _secret_env_key(agent_id: str) -> str:
    safe_agent_id = "".join(
        char if char.isalnum() else "_" for char in agent_id.upper()
    )
    return f"COORDINATION_SECRET_{safe_agent_id}"


def resolve_secrets_dir(
    secrets_dir: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the directory containing per-agent secret files."""
    environment = os.environ if env is None else env
    if env_dir := environment.get("COORDINATION_SECRETS_DIR"):
        return Path(env_dir).expanduser()

    base = Path(secrets_dir) if secrets_dir is not None else SECRETS_DIR
    if base.is_absolute():
        return base

    cwd_path = Path.cwd() if cwd is None else Path(cwd)
    for parent in (cwd_path, *cwd_path.parents):
        if (parent / ".git").exists():
            return parent / base

    return cwd_path / base


def resolve_secret(
    agent_id: str,
    *,
    secrets_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes | None:
    """Resolve an agent's secret from env var or secrets file.

    Resolution order:
    1. ``COORDINATION_SECRET_<NORMALIZED_AGENT_ID>`` env var
    2. ``COORDINATION_SECRETS_DIR/<agent_id>.secret`` file
    3. Git-root-relative ``secrets/coordination/<agent_id>.secret`` file
    4. Current-working-directory-relative ``secrets/coordination/<agent_id>.secret``

    ``NORMALIZED_AGENT_ID`` is uppercased with non-alphanumeric characters
    converted to underscores, so ``agent-a`` maps to
    ``COORDINATION_SECRET_AGENT_A``.

    Returns ``None`` if neither source is available.
    """
    environment = os.environ if env is None else env
    env_keys = [_secret_env_key(agent_id)]
    legacy_env_key = f"COORDINATION_SECRET_{agent_id.upper()}"
    if legacy_env_key not in env_keys:
        env_keys.append(legacy_env_key)
    for env_key in env_keys:
        env_val = environment.get(env_key)
        if env_val:
            return env_val.encode("utf-8")

    secret_path = resolve_secrets_dir(secrets_dir, cwd=cwd, env=environment) / (
        f"{agent_id}.secret"
    )
    if secret_path.exists():
        return secret_path.read_bytes().strip()

    return None
