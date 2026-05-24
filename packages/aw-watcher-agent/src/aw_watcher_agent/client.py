"""Thin vendored ActivityWatch REST client (stdlib-only).

Phase 1 of aw-watcher-agent keeps zero heavy dependencies: it talks to a local
aw-server over its documented REST API using ``urllib`` instead of pulling in
``aw-client``/``aw-core``. The watcher therefore stays pip-installable and light
enough to ship as a hook target.

API shapes were verified against a live aw-server v0.13.2:

- ``GET  /api/0/info`` -> ``{hostname, version, ...}``
- ``GET  /api/0/buckets/`` -> ``{bucket_id: {id, type, client, hostname, ...}}``
- ``POST /api/0/buckets/{id}`` -> create bucket (idempotent-ish; 304 if exists)
- ``POST /api/0/buckets/{id}/events`` -> insert event(s); returns event with ``id``
- ``DELETE /api/0/buckets/{id}/events/{event_id}`` -> remove an event
- ``POST /api/0/buckets/{id}/heartbeat?pulsetime=N`` -> merge-extend an event
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:5600"


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string aw-server accepts."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    """An ActivityWatch event: a timestamped, durationed bag of string data."""

    timestamp: str
    duration: float
    data: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "duration": self.duration,
            "data": self.data,
        }


class AWClientError(RuntimeError):
    """Raised when the aw-server returns an unrecoverable error."""


class AWClient:
    """Minimal aw-server REST client over the stdlib."""

    def __init__(self, server: str = DEFAULT_SERVER, timeout: float = 5.0) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        url = f"{self.server}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw) if raw else None
                return resp.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw.decode(errors="replace")
            return exc.code, parsed

    # --- read ---------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        status, body = self._request("GET", "/api/0/info")
        if status != 200 or not isinstance(body, dict):
            raise AWClientError(f"unexpected /info response: {status} {body!r}")
        return body

    def buckets(self) -> dict[str, Any]:
        status, body = self._request("GET", "/api/0/buckets/")
        if status != 200 or not isinstance(body, dict):
            raise AWClientError(f"unexpected /buckets response: {status} {body!r}")
        return body

    # --- bucket lifecycle ---------------------------------------------------

    def ensure_bucket(
        self,
        bucket_id: str,
        event_type: str,
        client_name: str,
        hostname: str,
    ) -> bool:
        """Create the bucket if missing. Returns True if newly created.

        Idempotent: an already-existing bucket is treated as success rather than
        an error, so the watcher can call this on every session start.
        """
        if bucket_id in self.buckets():
            return False
        payload = {
            "client": client_name,
            "type": event_type,
            "hostname": hostname,
        }
        status, body = self._request("POST", f"/api/0/buckets/{bucket_id}", payload)
        # 200/201 = created, 304 = already existed (race with a parallel start)
        if status in (200, 201, 304):
            return status != 304
        raise AWClientError(f"failed to create bucket {bucket_id}: {status} {body!r}")

    # --- events -------------------------------------------------------------

    def post_event(self, bucket_id: str, event: Event) -> int | None:
        """Insert a single event. Returns the server-assigned event id if present."""
        status, body = self._request(
            "POST", f"/api/0/buckets/{bucket_id}/events", event.to_payload()
        )
        if status not in (200, 201):
            raise AWClientError(f"failed to post event to {bucket_id}: {status} {body!r}")
        if isinstance(body, dict):
            return body.get("id")
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return body[0].get("id")
        return None

    def delete_event(self, bucket_id: str, event_id: int) -> bool:
        status, _ = self._request("DELETE", f"/api/0/buckets/{bucket_id}/events/{event_id}")
        return status in (200, 204)

    def heartbeat(self, bucket_id: str, event: Event, pulsetime: float) -> int | None:
        """Merge-extend the latest event whose data matches, within ``pulsetime``."""
        status, body = self._request(
            "POST",
            f"/api/0/buckets/{bucket_id}/heartbeat?pulsetime={pulsetime}",
            event.to_payload(),
        )
        if status not in (200, 201):
            raise AWClientError(f"failed to heartbeat {bucket_id}: {status} {body!r}")
        return body.get("id") if isinstance(body, dict) else None
