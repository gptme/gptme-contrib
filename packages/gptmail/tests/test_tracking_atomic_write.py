"""A crash mid-write must not corrupt the tracker state (gptme/gptme-contrib#2303 item 5)."""

import json
from pathlib import Path

import pytest
from gptmail.communication_utils.state import tracking
from gptmail.communication_utils.state.tracking import (
    ConversationTracker,
    MessageState,
)


def _crashing_dump(obj, fp, **kwargs):
    fp.write("{")
    raise RuntimeError("crash mid-dump")


@pytest.mark.parametrize("op", ["track", "update"])
def test_crash_mid_write_keeps_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    tracker = ConversationTracker(tmp_path)
    tracker.track_message("email", "msg1")
    tracker.set_message_state("email", "msg1", MessageState.COMPLETED)

    monkeypatch.setattr(tracking.json, "dump", _crashing_dump)
    with pytest.raises(RuntimeError):
        if op == "track":
            tracker.track_message("email", "msg2")
        else:
            tracker.set_message_state("email", "msg1", MessageState.FAILED)
    monkeypatch.undo()

    # The previous state is intact and readable; no temp files are left behind.
    info = tracker.get_message_state("email", "msg1")
    assert info is not None and info.state == MessageState.COMPLETED
    leftovers = [p.name for p in tmp_path.rglob("*") if p.is_file() and ".tmp" in p.name]
    assert leftovers == []


def test_write_is_valid_json(tmp_path: Path) -> None:
    tracker = ConversationTracker(tmp_path)
    tracker.track_message("email", "msg1")
    files = [p for p in tmp_path.rglob("*.json")]
    assert files and all(json.loads(p.read_text()) for p in files)
