"""Twilio farewell drain: generation completion is not playback completion."""

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# Safety bound for a lost provider response.done or Twilio mark, not a pause
# added to successful calls. Normal hangup proceeds as soon as playback ends.
HANGUP_DRAIN_TIMEOUT_SECONDS = 30.0


class TwilioPlayback:
    """Connection-scoped playback barrier and cancellable hangup ownership.

    Twilio echoes marks after playback OR clear. Cancel the hangup before
    sending clear, so cleared audio can never authorize graceful termination.
    """

    def __init__(self, websocket, stream_sid: str) -> None:
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.hangup_task: asyncio.Task[None] | None = None
        # Set by caller speech, cleared when the provider starts its next
        # response. Until then, hangup triggers belong to the interrupted one.
        self.interrupted = False
        self.terminating = False
        self.stopped = False
        # The media stream dropped without Twilio's ``stop``: playback can no
        # longer be confirmed, but the platform call still has to be completed.
        self.disconnected = False
        self.send_lock = asyncio.Lock()
        self._response_done = asyncio.Event()
        self._response_done.set()
        self._revision = 0
        self._mark_number = 0
        self._pending_mark: str | None = None
        self._played = asyncio.Event()

    def response_started(self) -> None:
        # The provider runs one response at a time, so every late event of an
        # interrupted response precedes this one. Do not wait for the caller's
        # transcript here: it is asynchronous and often arrives afterwards.
        self.interrupted = False
        self._revision += 1
        self._response_done.clear()

    def response_finished(self) -> None:
        self._response_done.set()

    def audio_sent(self) -> None:
        self._revision += 1

    def acknowledge(self, name: str) -> None:
        if name == self._pending_mark:
            self._played.set()

    def cancel_hangup(self) -> None:
        self.interrupted = True
        if self.hangup_task is not None and not self.terminating:
            self.hangup_task.cancel()
        # A clear echo must not satisfy a later farewell's mark.
        self._pending_mark = None

    async def wait_until_played(self) -> None:
        """Wait for all generated audio, then the exact transport acknowledgement."""
        deadline = asyncio.get_running_loop().time() + HANGUP_DRAIN_TIMEOUT_SECONDS
        try:
            while True:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                try:
                    revision = await asyncio.wait_for(self._drain(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:
                    # The media stream closed while the mark was being sent or
                    # awaited (``websockets.ConnectionClosed`` and friends).
                    # Playback cannot be confirmed, but a transport error must
                    # not skip the caller's authoritative kill: closing the
                    # WebSocket does not end the call at the platform level.
                    # Cancellation stays a BaseException and still propagates.
                    logger.warning(
                        "Farewell playback unconfirmed: transport error for "
                        "stream %s: %s",
                        self.stream_sid,
                        exc,
                    )
                    return
                # wait_for resumes us after its child task finishes: recheck here
                # too, in case audio arrived during that event-loop handoff.
                if revision == self._revision and self._response_done.is_set():
                    logger.info(
                        "Farewell playback acknowledged: stream=%s mark=%s",
                        self.stream_sid,
                        self._pending_mark,
                    )
                    return
        except asyncio.TimeoutError:
            logger.warning(
                "Farewell playback unconfirmed: drain timed out for stream %s",
                self.stream_sid,
            )
        finally:
            self._pending_mark = None

    async def _drain(self) -> int:
        await self._response_done.wait()
        # Serialize with media writes, including concurrent tool sound cues.
        async with self.send_lock:
            revision = self._revision
            self._mark_number += 1
            self._pending_mark = f"farewell-{self._mark_number}"
            self._played.clear()
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": self._pending_mark},
                    }
                )
            )
        await self._played.wait()
        return revision
