"""Tests for SessionStore cross-process locking.

Regression coverage for the 2026-07-14 production incident: concurrent
writers to session-records.jsonl (via a shared fixed temp-file name and no
mutual exclusion) produced torn/truncated JSON records. ``SessionStore``
now takes an ``fcntl.flock`` on a permanent sentinel file for every
``append()``/``rewrite()``, and ``rewrite()`` writes to a per-pid temp file
before an atomic ``replace()``.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from gptme_sessions import SessionRecord, SessionStore

try:
    import fcntl  # noqa: F401

    HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX platforms
    HAS_FCNTL = False

# Prefer "fork". This package's workspace pytest config sets
# ``--import-mode=importlib`` (needed for cross-package test collection),
# which gives test modules a synthetic dotted name that isn't reachable via
# a plain ``import`` in a freshly spawned interpreter — so multiprocessing's
# "spawn" start method fails to re-import the worker functions in the child
# (``ModuleNotFoundError: No module named 'packages'``). "fork" duplicates
# the already-imported parent process instead of re-importing, sidestepping
# that entirely. Fall back to "spawn" on platforms without fork (e.g. Windows);
# the fork-dependent tests there would need a different fix, but the module
# must still be importable.
MP_CTX = multiprocessing.get_context(
    "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
)


# ---------------------------------------------------------------------------
# 1. Reentrancy — nested lock() must not self-deadlock
# ---------------------------------------------------------------------------


def _reentrant_worker(sessions_dir: str, result_queue: "multiprocessing.Queue") -> None:
    """Nest store.lock() twice and mutate the store at both levels.

    Runs in a child process so a self-deadlock regression hangs the child
    rather than the test process, and can be caught with ``join(timeout=...)``.
    """
    try:
        store = SessionStore(sessions_dir=Path(sessions_dir))
        with store.lock():
            store.append(SessionRecord(session_id="outer-1", model="outer"))
            store.rewrite(store.load_all())
            with store.lock():
                store.append(SessionRecord(session_id="inner-1", model="inner"))
                store.rewrite(store.load_all())
            store.append(SessionRecord(session_id="outer-2", model="outer"))
        result_queue.put("ok")
    except BaseException as e:  # pragma: no cover - only hit on regression
        result_queue.put(f"error: {e!r}")


def test_lock_is_reentrant_no_self_deadlock(tmp_path: Path):
    """Nested store.lock() calls within one process must complete, not hang."""
    result_queue = MP_CTX.Queue()
    proc = MP_CTX.Process(target=_reentrant_worker, args=(str(tmp_path), result_queue), daemon=True)
    proc.start()
    proc.join(timeout=20)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        pytest.fail("nested store.lock() hung — self-deadlock regression")

    assert proc.exitcode == 0
    assert not result_queue.empty()
    assert result_queue.get(timeout=5) == "ok"


# ---------------------------------------------------------------------------
# 2. Cross-process mutual exclusion — no torn writes under concurrent load
# ---------------------------------------------------------------------------

ITERATIONS = 30
N_APPENDERS = 3
N_REWRITERS = 3


def _append_worker(sessions_dir: str, worker_id: int, iterations: int) -> None:
    store = SessionStore(sessions_dir=Path(sessions_dir))
    for i in range(iterations):
        store.append(SessionRecord(session_id=f"append-{worker_id}-{i}", model="appender"))


def _rewrite_worker(sessions_dir: str, worker_id: int, iterations: int) -> None:
    store = SessionStore(sessions_dir=Path(sessions_dir))
    for _ in range(iterations):
        with store.lock():
            records = store.load_all()
            store.rewrite(records)


def test_concurrent_append_and_rewrite_no_torn_writes(tmp_path: Path):
    """Hammer one store from multiple processes; assert no corruption.

    Regression test for the 2026-07-14 corruption: half the workers append
    unique records, half do load-modify-rewrite cycles, all against the same
    file concurrently. Afterward every line must still parse as JSON, every
    appended record must be present, and no temp-file litter may remain.
    """
    store = SessionStore(sessions_dir=tmp_path)

    procs = [
        MP_CTX.Process(target=_append_worker, args=(str(tmp_path), wid, ITERATIONS))
        for wid in range(N_APPENDERS)
    ] + [
        MP_CTX.Process(target=_rewrite_worker, args=(str(tmp_path), wid, ITERATIONS))
        for wid in range(N_REWRITERS)
    ]

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    for i, p in enumerate(procs):
        assert not p.is_alive(), f"worker {i} did not finish within timeout"
        assert p.exitcode == 0, f"worker {i} failed (exitcode={p.exitcode})"

    # (a) every line parses as JSON — no torn/truncated records.
    content = store.path.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert lines, "expected records to have been written"
    parsed = [json.loads(line) for line in lines]  # raises ValueError if any line is torn

    # (b) every appended unique session_id survived (no lost appends).
    expected_ids = {f"append-{wid}-{i}" for wid in range(N_APPENDERS) for i in range(ITERATIONS)}
    seen_ids = {rec["session_id"] for rec in parsed}
    missing = expected_ids - seen_ids
    assert not missing, f"lost {len(missing)} appended records, e.g. {sorted(missing)[:5]}"

    # (c) no leftover *.tmp* files from interrupted rewrites.
    leftover_tmp = list(tmp_path.glob("*.tmp*"))
    assert not leftover_tmp, f"leftover temp files: {leftover_tmp}"


# ---------------------------------------------------------------------------
# 3. Temp file is per-pid and cleaned up on failure
# ---------------------------------------------------------------------------


def test_rewrite_temp_file_is_per_pid(tmp_path: Path, monkeypatch):
    """rewrite()'s temp file name must embed the current pid (not a fixed name).

    A fixed shared temp name is exactly the mechanism behind the 2026-07-14
    torn-file corruption: two concurrent rewriters could interleave writes to
    the same inode. Spy on Path.replace (the atomic-install call) to capture
    the temp path actually used.
    """
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(session_id="a", model="x"))

    captured: list[Path] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target):
        captured.append(self)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)

    store.rewrite(store.load_all())

    assert len(captured) == 1
    tmp_used = captured[0]
    assert tmp_used.name == f"{store.path.name}.tmp.{os.getpid()}"


class _ExplodingRecord(SessionRecord):
    """A record whose serialization always fails, to exercise rewrite()'s
    exception-cleanup path."""

    def to_json(self) -> str:  # type: ignore[override]
        raise RuntimeError("boom - injected to_json failure")


def test_rewrite_propagates_failure_and_cleans_temp_file(tmp_path: Path):
    """A failure mid-write must propagate, leave the store file untouched,
    and not leak a temp file."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(session_id="baseline", model="opus"))
    original_content = store.path.read_text(encoding="utf-8")

    exploding = _ExplodingRecord(session_id="boom")
    with pytest.raises(RuntimeError, match="boom"):
        store.rewrite([exploding])

    # Store file must be exactly as it was before the failed rewrite.
    assert store.path.read_text(encoding="utf-8") == original_content

    # No temp file left behind.
    leftovers = list(tmp_path.glob(f"{store.sessions_file}.tmp*"))
    assert not leftovers, f"leftover temp files: {leftovers}"

    # Lock must have been released despite the exception — a subsequent
    # operation must not deadlock or hang.
    store.append(SessionRecord(session_id="after", model="opus"))
    records = store.load_all()
    assert {r.session_id for r in records} == {"baseline", "after"}


# ---------------------------------------------------------------------------
# 4. Lock sentinel semantics — permanent, composes across instances
# ---------------------------------------------------------------------------


def test_lock_sentinel_permanent_and_composes_across_instances(tmp_path: Path):
    """The <file>.lock sentinel appears after use and is never deleted; two
    separate SessionStore instances on the same path can both mutate it."""
    store1 = SessionStore(sessions_dir=tmp_path)
    store1.append(SessionRecord(session_id="a", model="one"))

    lock_path = store1.path.with_name(store1.path.name + ".lock")
    assert lock_path.exists()

    # A second, independent instance on the same path must be able to
    # append and rewrite without issue (locks compose across instances,
    # not just within one).
    store2 = SessionStore(sessions_dir=tmp_path)
    store2.append(SessionRecord(session_id="b", model="two"))
    store2.rewrite(store2.load_all())

    assert lock_path.exists()  # sentinel is permanent — never unlinked
    records = store1.load_all()
    assert {r.session_id for r in records} == {"a", "b"}


# ---------------------------------------------------------------------------
# 5. flock actually blocks a second process
# ---------------------------------------------------------------------------


def _blocking_append_worker(sessions_dir: str, done_queue: "multiprocessing.Queue") -> None:
    store = SessionStore(sessions_dir=Path(sessions_dir))
    t0 = time.monotonic()
    store.append(SessionRecord(session_id="child", model="child"))
    done_queue.put(time.monotonic() - t0)


@pytest.mark.skipif(not HAS_FCNTL, reason="flock is POSIX-only")
def test_flock_blocks_second_process_until_release(tmp_path: Path):
    """A process holding store.lock() must block a concurrent append() in
    another process until it releases."""
    store = SessionStore(sessions_dir=tmp_path)
    hold_seconds = 2.0

    done_queue = MP_CTX.Queue()
    child = MP_CTX.Process(
        target=_blocking_append_worker, args=(str(tmp_path), done_queue), daemon=True
    )

    with store.lock():
        child.start()
        # Hold the lock well past the child's process-startup time so its
        # append() is guaranteed to be waiting on flock() when we release.
        time.sleep(hold_seconds)

    child.join(timeout=15)
    assert not child.is_alive(), "child did not finish after lock release"
    assert child.exitcode == 0
    assert not done_queue.empty()

    elapsed = done_queue.get(timeout=5)
    # Generous margin below hold_seconds to absorb process-spawn overhead
    # while still clearly distinguishing "blocked" from "raced right in".
    assert elapsed >= 0.8, (
        f"child's append() returned in {elapsed:.2f}s — "
        "lock does not appear to be blocking a concurrent process"
    )

    records = store.load_all()
    assert any(r.session_id == "child" for r in records)
