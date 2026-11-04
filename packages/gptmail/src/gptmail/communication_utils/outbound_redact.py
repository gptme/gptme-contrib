"""Optional fail-closed secret gate for outbound communication.

The agent workspace may provide the ``redact`` package. When it does, critical
findings block delivery and are recorded in the workspace state ledger. A
standalone gptme-contrib install does not require that package, so its absence is
reported and the gate is skipped unless the caller requires scanning.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def guard_outbound(
    text: str, channel: str, workspace: Path, *, require_scanner: bool = False
) -> bool:
    """Return whether ``text`` may be sent through ``channel``.

    Missing redaction support refuses delivery when ``require_scanner`` is set;
    otherwise it warns and allows delivery for standalone compatibility. A detected
    critical secret blocks delivery and writes only finding metadata, never the
    outbound text, to ``state/outbound-redact-blocks.jsonl``.
    """
    try:
        from redact import collect_secret_literals
        from redact import redact as scan
    except ModuleNotFoundError as exc:
        if exc.name != "redact":
            raise
        if require_scanner:
            logger.error("Outbound content blocked: required secret scanner unavailable")
            return False
        logger.warning("redact package not available — skipping outbound secret scan")
        return True

    result = scan(text, literals=collect_secret_literals())
    if not result.blocked:
        return True

    ledger = workspace / "state" / "outbound-redact-blocks.jsonl"
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "blocked": True,
        "reasons": result.block_reasons,
    }
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Could not record outbound security block: %s", exc)

    reasons = "; ".join(result.block_reasons)
    logger.error("Outbound content blocked on channel=%s: %s", channel, reasons)
    return False
