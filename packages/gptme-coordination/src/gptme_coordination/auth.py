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
import os
from pathlib import Path

SECRETS_DIR = Path("secrets/coordination")


def compute_hmac(secret: bytes, *fields: str) -> str:
    """HMAC-SHA256 over pipe-joined fields, base64-encoded."""
    data = "|".join(fields).encode("utf-8")
    return base64.b64encode(hmac.new(secret, data, hashlib.sha256).digest()).decode(
        "ascii"
    )


def verify_hmac(secret: bytes, expected: str, *fields: str) -> bool:
    """Verify HMAC using constant-time comparison."""
    computed = compute_hmac(secret, *fields)
    return hmac.compare_digest(computed, expected)


def resolve_secret(agent_id: str) -> bytes | None:
    """Resolve an agent's secret from env var or secrets file.

    Resolution order:
    1. ``COORDINATION_SECRET_<AGENT_ID_UPPER>`` env var
    2. ``secrets/coordination/<agent_id>.secret`` file

    Returns ``None`` if neither source is available.
    """
    env_key = f"COORDINATION_SECRET_{agent_id.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val.encode("utf-8")

    secret_path = SECRETS_DIR / f"{agent_id}.secret"
    if secret_path.exists():
        return secret_path.read_bytes().strip()

    return None
