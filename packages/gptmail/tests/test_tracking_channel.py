"""Tests for the ``channel`` field on tracked messages.

The channel field lets a single ConversationTracker store serve multiple
transports (email, inter-agent) and answer cross-channel "what do I owe a
reply to" queries. See task: fold-agent-msg-into-gptmail-single-comms-tool.
"""

import json
from pathlib import Path

from gptmail.communication_utils.state.tracking import (
    ConversationTracker,
    MessageInfo,
    MessageState,
)


def test_track_message_records_channel(tmp_path: Path) -> None:
    tracker = ConversationTracker(tmp_path)
    info = tracker.track_message("conv1", "msg1", channel="agent")
    assert info.channel == "agent"

    # Round-trips through the on-disk JSON store.
    reloaded = tracker.get_message_state("conv1", "msg1")
    assert reloaded is not None
    assert reloaded.channel == "agent"


def test_channel_defaults_to_none(tmp_path: Path) -> None:
    """Email transport (and existing callers) omit channel; it stays None."""
    tracker = ConversationTracker(tmp_path)
    tracker.track_message("conv1", "msg1")
    reloaded = tracker.get_message_state("conv1", "msg1")
    assert reloaded is not None
    assert reloaded.channel is None


def test_legacy_records_without_channel_load(tmp_path: Path) -> None:
    """Pre-channel #1085 state files must load unchanged (backward compatible)."""
    state_file = tmp_path / "conv1.json"
    state_file.write_text(
        json.dumps(
            {
                "conversation_id": "conv1",
                "messages": {
                    "msg1": {
                        "message_id": "msg1",
                        "conversation_id": "conv1",
                        "state": "pending",
                        "created_at": "2026-06-01T00:00:00",
                    }
                },
            }
        )
    )
    tracker = ConversationTracker(tmp_path)
    info = tracker.get_message_state("conv1", "msg1")
    assert info is not None
    assert info.channel is None
    assert info.state == MessageState.PENDING


def test_from_dict_filters_unknown_but_keeps_channel() -> None:
    info = MessageInfo.from_dict(
        {
            "message_id": "m",
            "state": "pending",
            "channel": "agent",
            "platform": "legacy-ignored",
        }
    )
    assert info.channel == "agent"
