"""Optional fail-closed secret gate for outbound communication.

The agent workspace may provide the ``redact`` package. When it does, critical
findings block delivery and are recorded in the workspace state ledger. A
standalone gptme-contrib install does not require that package, so its absence
is reported and the gate is skipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def guard_outbound(text: str, channel: str, workspace: Path) -> bool:
    """Return whether ``text`` may be sent through ``channel``.

    Missing optional redaction support warns and allows delivery. A detected
    critical secret blocks delivery and writes only finding metadata, never the
    outbound text, to ``state/outbound-redact-blocks.jsonl``.
    """
    try:
        from redact import collect_secret_literals
        from redact import redact as scan
    except ImportError:
        logger.warning("redact package not available — skipping outbound secret scan")
        return True

    result = scan(text, literals=collect_secret_literals())
    if not result.blocked:
        return True

    ledger = workspace / "state" / "outbound-redact-blocks.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "blocked": True,
        "reasons": result.block_reasons,
    }
    with ledger.open("a") as f:
        f.write(json.dumps(record) + "\n")

    reasons = "; ".join(result.block_reasons)
    logger.error("Outbound content blocked on channel=%s: %s", channel, reasons)
    return False
