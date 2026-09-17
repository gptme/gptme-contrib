"""Backward-compatible re-export from :mod:`missed_call_context`.

The implementation has moved to :mod:`~gptme_voice.realtime.missed_call_context`.
This module keeps its public API intact so existing imports continue to work.
"""

from .missed_call_context import (
    CALLBACK_GREETING,
    CALLBACK_GUIDANCE,
    load_callback_brief,
    load_callback_candidate,
    load_callback_history_index,
    record_inbound_call,
    write_missed_call_context,
)

__all__ = [
    "CALLBACK_GREETING",
    "CALLBACK_GUIDANCE",
    "load_callback_brief",
    "load_callback_candidate",
    "load_callback_history_index",
    "record_inbound_call",
    "write_missed_call_context",
]
