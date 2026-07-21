"""Regression tests for concurrent maildir sync state writers."""

from __future__ import annotations

import json
import multiprocessing
import queue
from pathlib import Path

from gptmail.lib import AgentEmail


def _make_workspace(root: Path) -> AgentEmail:
    for folder in ("inbox", "sent", "archive", "drafts", "filters"):
        (root / "email" / folder).mkdir(parents=True, exist_ok=True)
    return AgentEmail(root, "bob@example.com")


def _sync_update(
    workspace: str, entry: str, entered: multiprocessing.Queue, release: multiprocessing.Event
) -> None:
    email = AgentEmail(workspace, "bob@example.com")
    original = email._sync_from_maildir_locked

    def update(folder: str) -> None:
        state = email._load_sync_state(folder)
        entered.put(entry)
        release.wait(timeout=5)
        state.add(entry)
        email._save_sync_state(folder, state)

    email._sync_from_maildir_locked = update  # type: ignore[method-assign]
    try:
        email.sync_from_maildir("inbox")
    finally:
        email._sync_from_maildir_locked = original  # type: ignore[method-assign]


def test_sync_state_transactions_preserve_concurrent_updates(tmp_path: Path) -> None:
    email = _make_workspace(tmp_path)
    entered: multiprocessing.Queue = multiprocessing.Queue()
    release = multiprocessing.Event()
    processes = [
        multiprocessing.Process(target=_sync_update, args=(str(tmp_path), entry, entered, release))
        for entry in ("first", "second")
    ]

    processes[0].start()
    assert entered.get(timeout=5) == "first"
    processes[1].start()
    try:
        entered.get(timeout=0.1)
    except queue.Empty:
        pass
    else:
        raise AssertionError("the second sync entered before the first released its lock")
    release.set()
    assert entered.get(timeout=5) == "second"

    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert email._load_sync_state("inbox") == {"first", "second"}


def test_save_sync_state_replaces_json_atomically(tmp_path: Path) -> None:
    email = _make_workspace(tmp_path)

    email._save_sync_state("inbox", {"message"})

    state_path = email._get_sync_state_path("inbox")
    assert json.loads(state_path.read_text()) == {"processed_files": ["message"]}
    assert list(state_path.parent.glob(f".{state_path.name}.*.tmp")) == []
