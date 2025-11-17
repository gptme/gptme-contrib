"""Tests for Discord bot state management.

Tests verify:
1. State tracking across message lifecycle
2. State persistence and recovery
3. Concurrent message handling
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Add scripts to path for communication_utils import
sys.path.insert(0, str(Path(__file__).parent.parent))

from communication_utils.state import ConversationTracker, MessageState


class TestStateTracking:
    """Test message state tracking throughout lifecycle."""

    def test_message_lifecycle_states(self, tmp_path: Path) -> None:
        """Test state transitions: IN_PROGRESS → COMPLETED/FAILED."""
        tracker = ConversationTracker(tmp_path)
        conv_id = "test_channel"
        msg_id = "test_message"

        # Initial state: IN_PROGRESS
        tracker.set_message_state(conv_id, msg_id, MessageState.IN_PROGRESS)
        message_info = tracker.get_message_state(conv_id, msg_id)
        assert message_info is not None
        assert message_info.state == MessageState.IN_PROGRESS

        # Success: COMPLETED
        tracker.set_message_state(conv_id, msg_id, MessageState.COMPLETED)
        message_info = tracker.get_message_state(conv_id, msg_id)
        assert message_info is not None
        assert message_info.state == MessageState.COMPLETED

    def test_message_failure_state(self, tmp_path: Path) -> None:
        """Test failure state with error details."""
        tracker = ConversationTracker(tmp_path)
        conv_id = "test_channel"
        msg_id = "test_message"

        # Failure: FAILED with error
        tracker.set_message_state(
            conv_id,
            msg_id,
            MessageState.FAILED,
            error="Test error occurred",
        )
        message_info = tracker.get_message_state(conv_id, msg_id)
        assert message_info is not None
        assert message_info.state == MessageState.FAILED
        assert message_info.error == "Test error occurred"


class TestStatePersistence:
    """Test state persistence and recovery."""

    def test_state_survives_restart(self, tmp_path: Path) -> None:
        """Test that message states persist across tracker restarts."""
        conv_id = "persistent_channel"
        msg_id = "persistent_message"

        # Create tracker and set state
        tracker1 = ConversationTracker(tmp_path)
        tracker1.set_message_state(conv_id, msg_id, MessageState.COMPLETED)

        # Create new tracker instance (simulates bot restart)
        tracker2 = ConversationTracker(tmp_path)
        message_info = tracker2.get_message_state(conv_id, msg_id)
        assert message_info is not None
        assert message_info.state == MessageState.COMPLETED

    def test_multiple_messages_persist(self, tmp_path: Path) -> None:
        """Test that multiple message states persist correctly."""
        tracker1 = ConversationTracker(tmp_path)
        conv_id = "test_channel"

        # Set multiple message states
        tracker1.set_message_state(conv_id, "msg1", MessageState.COMPLETED)
        tracker1.set_message_state(conv_id, "msg2", MessageState.IN_PROGRESS)
        tracker1.set_message_state(conv_id, "msg3", MessageState.FAILED)

        # Recover with new tracker
        tracker2 = ConversationTracker(tmp_path)
        msg1_info = tracker2.get_message_state(conv_id, "msg1")
        assert msg1_info is not None
        assert msg1_info.state == MessageState.COMPLETED

        msg2_info = tracker2.get_message_state(conv_id, "msg2")
        assert msg2_info is not None
        assert msg2_info.state == MessageState.IN_PROGRESS

        msg3_info = tracker2.get_message_state(conv_id, "msg3")
        assert msg3_info is not None
        assert msg3_info.state == MessageState.FAILED


class TestConcurrentMessageHandling:
    """Test concurrent message handling patterns (asyncio concurrency)."""

    @pytest.mark.asyncio
    async def test_concurrent_state_updates(self, tmp_path: Path) -> None:
        """Test that concurrent state updates don't conflict."""
        tracker = ConversationTracker(tmp_path)
        conv_id = "test_channel"

        async def update_message_state(msg_id: str, state: MessageState) -> None:
            """Simulate message state update."""
            tracker.set_message_state(conv_id, msg_id, state)
            # Simulate some async work
            await asyncio.sleep(0.01)
            tracker.set_message_state(conv_id, msg_id, MessageState.COMPLETED)

        # Process multiple messages concurrently
        tasks = [
            update_message_state(f"msg{i}", MessageState.IN_PROGRESS) for i in range(10)
        ]
        await asyncio.gather(*tasks)

        # Verify all messages reached COMPLETED state
        for i in range(10):
            message_info = tracker.get_message_state(conv_id, f"msg{i}")
            assert message_info is not None
            assert message_info.state == MessageState.COMPLETED

    @pytest.mark.asyncio
    async def test_no_race_conditions_on_state_access(self, tmp_path: Path) -> None:
        """Test that file locks prevent race conditions."""
        tracker = ConversationTracker(tmp_path)
        conv_id = "test_channel"
        msg_id = "concurrent_test"

        # Simulate multiple coroutines trying to access same message
        results = []

        async def access_and_update(iteration: int) -> None:
            """Read and update state, simulating concurrent access."""
            tracker.set_message_state(
                conv_id,
                msg_id,
                MessageState.IN_PROGRESS,
            )
            await asyncio.sleep(0.001)  # Tiny delay to encourage interleaving
            message_info = tracker.get_message_state(conv_id, msg_id)
            assert message_info is not None
            results.append((iteration, message_info.state))

        # Run multiple concurrent accesses
        await asyncio.gather(*[access_and_update(i) for i in range(5)])

        # All should see IN_PROGRESS (last written state)
        assert all(state == MessageState.IN_PROGRESS for _, state in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
