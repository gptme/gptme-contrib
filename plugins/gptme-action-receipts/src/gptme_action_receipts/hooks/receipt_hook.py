"""Pre-action audit receipt hook for gptme.

Before each tool executes, appends one JSON line to an append-only ledger
at ``~/.local/share/gptme/receipts.jsonl``.  The receipt captures enough
context to answer "who authorized this action, when, and in what scope" —
the minimal audit trail that would have surfaced the gptme-contrib#1175
unauthorized self-merge incident at response time.

Phase 1 (this module): ledger + receipt emission only — no blocking gate.
Phase 2 (future): wire a scope-check that aborts out-of-scope actions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.hooks.server_confirm import current_session_id

if TYPE_CHECKING:
    from gptme.hooks import StopPropagation
    from gptme.hooks.types import ToolExecutePreData
    from gptme.message import Message

logger = logging.getLogger(__name__)

# Default ledger path — respects XDG_DATA_HOME if set.
_DEFAULT_LEDGER = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "gptme"
    / "receipts.jsonl"
)


def _ledger_path() -> Path:
    """Return the active ledger path, honouring GPTME_RECEIPTS_LEDGER env override."""
    override = os.environ.get("GPTME_RECEIPTS_LEDGER")
    return Path(override) if override else _DEFAULT_LEDGER


def _make_receipt(
    tool_name: str,
    target: str,
    workspace: Path | None,
    session_id: str | None,
) -> dict:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "session_id": session_id or os.environ.get("GPTME_SESSION_ID", "unknown"),
        "model": (
            os.environ.get("CC_MODEL") or os.environ.get("GPTME_MODEL") or "unknown"
        ),
        "action_type": tool_name,
        "target": target,
        "workspace": str(workspace) if workspace else None,
        "timestamp": ts,
    }
    # Deterministic hash of the receipt content for tamper-evidence.
    blob = json.dumps(payload, sort_keys=True).encode()
    payload["receipt_hash"] = "sha256:" + hashlib.sha256(blob).hexdigest()
    return payload


def _receipt_pre(
    data: ToolExecutePreData,
) -> Generator[Message | StopPropagation, None, None]:
    """Emit one receipt line to the ledger before the tool executes."""
    if data.tool_use is None:
        return

    tool_name = getattr(data.tool_use, "tool", "unknown") or "unknown"
    # Use content as the target descriptor (first 512 chars to keep lines reasonable).
    raw_content = getattr(data.tool_use, "content", "") or ""
    target = raw_content[:512].strip()

    session_id = current_session_id.get()
    receipt = _make_receipt(tool_name, target, data.workspace, session_id)

    ledger = _ledger_path()
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt) + "\n")
        logger.debug("action-receipts: emitted receipt for %s", tool_name)
    except OSError as exc:
        # Never crash the agent over an audit write failure — log and move on.
        logger.warning("action-receipts: failed to write receipt: %s", exc)

    return
    yield  # make this a generator


def register() -> None:
    """Register the action receipt hook with gptme."""
    register_hook(
        "action_receipts.pre",
        HookType.TOOL_EXECUTE_PRE,
        _receipt_pre,
        # Low priority — run after higher-priority hooks (confirm, snapshot).
        # Negative so user hooks at 0 still fire first.
        priority=-50,
    )
    logger.info("action-receipts: hook registered (ledger: %s)", _ledger_path())
