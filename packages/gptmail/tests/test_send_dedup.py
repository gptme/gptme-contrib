"""Send-site duplicate guard for AgentEmail.send().

Concurrent sessions can each decide an inbox email is unreplied; the guard has
to live at the send site, not in the dispatch-time check.
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentEmail:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    a = AgentEmail(str(tmp_path), "bob@gptme.org")
    monkeypatch.setattr(a, "_validate_msmtp_config", lambda: True)
    return a


@pytest.fixture
def deliveries(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    sent: list[bytes] = []

    def fake_run(cmd: Any, input: bytes = b"", **kwargs: Any) -> Any:
        time.sleep(0.1)  # widen the window a check-then-send gate would lose
        sent.append(input)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return sent


def _draft(agent: AgentEmail, name: str, in_reply_to: str | None = None) -> str:
    headers = "To: erik@example.com\nSubject: Re: hi\n"
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\n"
    (agent.email_dir / "drafts" / f"{name}.md").write_text(headers + "\nHello\n")
    return name


def test_resending_same_draft_is_refused(agent: AgentEmail, deliveries: list) -> None:
    msg = _draft(agent, "d1")
    agent.send(msg)
    with pytest.raises(ValueError, match="Already sent"):
        agent.send(msg)
    assert len(deliveries) == 1


def test_second_reply_to_completed_original_is_refused(agent: AgentEmail, deliveries: list) -> None:
    agent.send(_draft(agent, "r1", "<orig@example.com>"))
    second = _draft(agent, "r2", "<orig@example.com>")
    with pytest.raises(ValueError, match="refusing duplicate reply"):
        agent.send(second)
    assert len(deliveries) == 1
    assert (agent.email_dir / "drafts" / "r2.md").exists()


def test_force_sends_reply_to_completed_original(agent: AgentEmail, deliveries: list) -> None:
    agent.send(_draft(agent, "r1", "<orig@example.com>"))
    agent.send(_draft(agent, "r2", "<orig@example.com>"), force=True)
    assert len(deliveries) == 2


def test_no_reply_needed_original_is_refused(agent: AgentEmail, deliveries: list) -> None:
    agent._mark_no_reply_needed("<orig@example.com>")
    with pytest.raises(ValueError, match="refusing duplicate reply"):
        agent.send(_draft(agent, "r1", "<orig@example.com>"))
    assert deliveries == []


def test_replies_to_different_originals_both_send(agent: AgentEmail, deliveries: list) -> None:
    agent.send(_draft(agent, "r1", "<a@example.com>"))
    agent.send(_draft(agent, "r2", "<b@example.com>"))
    assert len(deliveries) == 2


def test_failed_delivery_keeps_draft_and_allows_retry(
    agent: AgentEmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = _draft(agent, "r1", "<orig@example.com>")

    def fail(cmd: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, cmd, b"", b"smtp down")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        agent.send(msg)
    assert not agent._is_completed("<orig@example.com>")
    assert list((agent.email_dir / "sent").iterdir()) == []  # no staged copy left

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b"")
    )
    agent.send(msg)
    assert (agent.email_dir / "sent" / "r1.md").exists()


def test_concurrent_replies_to_same_original_deliver_once(
    agent: AgentEmail, deliveries: list
) -> None:
    """Different drafts racing to answer one email: the lock admits exactly one."""
    drafts = [_draft(agent, f"r{i}", "<orig@example.com>") for i in range(4)]
    results: list[str] = []

    def worker(msg: str) -> None:
        try:
            agent.send(msg)
            results.append("sent")
        except ValueError:
            results.append("refused")

    threads = [threading.Thread(target=worker, args=(d,)) for d in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(deliveries) == 1
    assert sorted(results) == ["refused"] * 3 + ["sent"]


def test_concurrent_sends_of_same_draft_deliver_once(agent: AgentEmail, deliveries: list) -> None:
    msg = _draft(agent, "d1")
    results: list[str] = []

    def worker() -> None:
        try:
            agent.send(msg)
            results.append("sent")
        except ValueError:
            results.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(deliveries) == 1
    assert sorted(results) == ["refused"] * 2 + ["sent"]


def test_second_sender_outwaits_a_holder_queued_on_the_reply_lock(
    agent: AgentEmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft-lock holder spends a reply-lock wait plus a send under it.

    Both fit the timeout on their own but not together, so a second sender of
    the same draft needs the longer wait to be refused instead of timing out.
    """
    import hashlib

    import gptmail.lib as lib

    monkeypatch.setattr(lib, "SEND_LOCK_TIMEOUT", 2.0)
    msg = _draft(agent, "queued", "<orig@example.com>")
    digest = hashlib.sha256(b"reply:orig@example.com").hexdigest()[:16]
    reply_lock = lib.FileLock(agent.email_dir / ".send-locks" / f"{digest}.lock")

    def slow_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        time.sleep(1.2)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", slow_run)
    results: list[str] = []

    def worker() -> None:
        try:
            agent.send(msg)
            results.append("sent")
        except ValueError:
            results.append("refused")
        except lib.LockError:
            results.append("timed out")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    with reply_lock:
        # Another reply to the same original is mid-send.
        for t in threads:
            t.start()
        time.sleep(1.2)
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["refused", "sent"]


def test_reply_lock_follows_the_draft_read_under_lock(
    agent: AgentEmail, deliveries: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply lock key must come from the draft as read under a lock (#1807).

    If In-Reply-To is read before any lock, an edit in between lets two senders
    of the same draft take different reply locks and both deliver it.
    """
    import hashlib

    import gptmail.lib as lib

    msg = _draft(agent, "d1", "<orig@example.com>")
    draft = agent.email_dir / "drafts" / "d1.md"
    real_lock = lib.FileLock
    locked: list[str] = []

    class EditingLock(real_lock):  # type: ignore[misc,valid-type]
        def __enter__(self) -> Any:
            if not locked:
                # A concurrent edit lands while this sender waits for a lock.
                draft.write_text(draft.read_text().replace("orig@", "new@"))
            locked.append(self.lock_path.stem)
            return super().__enter__()

    monkeypatch.setattr(lib, "FileLock", EditingLock)
    agent.send(msg)

    def key(k: str) -> str:
        return hashlib.sha256(k.encode()).hexdigest()[:16]

    assert key("reply:new@example.com") in locked
    assert key("reply:orig@example.com") not in locked
    assert len(deliveries) == 1


@pytest.mark.parametrize("original", [None, "<orig@example.com>"])
def test_send_uses_one_draft_snapshot(
    agent: AgentEmail, deliveries: list, monkeypatch: pytest.MonkeyPatch, original: str | None
) -> None:
    """An unlocked edit after the lock-key read cannot change what is delivered.

    Both surfaces are pinned to the first read: the bytes handed to msmtp and
    the copy published in sent/. The edit is written by the monkeypatched read
    itself, so any later re-read of the draft would return it.
    """
    from email import message_from_bytes

    msg = _draft(agent, "snapshot", original)
    draft = agent.email_dir / "drafts" / "snapshot.md"
    snapshot = draft.read_text()
    agent._mark_no_reply_needed("<completed@example.com>")
    real_read = Path.read_text
    reads = 0

    def edit_after_read(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal reads
        content = real_read(path, *args, **kwargs)
        if path == draft:
            reads += 1
            if reads == 1:
                draft.write_text(
                    "To: erik@example.com\nSubject: edited\n"
                    "In-Reply-To: <completed@example.com>\n\nEdited body\n"
                )
        return content

    monkeypatch.setattr(Path, "read_text", edit_after_read)
    agent.send(msg)

    assert len(deliveries) == 1
    assert b"Edited body" not in deliveries[0]
    delivered = message_from_bytes(deliveries[0])
    assert delivered.get("In-Reply-To") == original
    assert delivered["Subject"] == "Re: hi"
    assert "Edited body" in real_read(draft)  # never delivered, so still a draft
    archived = (agent.email_dir / "sent" / "snapshot.md").read_text()
    assert archived.startswith(snapshot)
    assert "Edited body" not in archived
    if original:
        assert agent._is_completed(original)


def test_draft_replaced_while_sent_copy_is_published(
    agent: AgentEmail, deliveries: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor swapping the draft file cannot change what sent/ records.

    The edit was never delivered, so it must also survive in drafts/.
    """
    msg = _draft(agent, "swapped", None)
    draft = agent.email_dir / "drafts" / "swapped.md"
    sent = agent.email_dir / "sent" / "swapped.md"
    snapshot = draft.read_text()

    def swap_draft_before(real_move: Any) -> Any:
        def move(path: Path, target: Any) -> Any:
            if Path(target) == sent:
                edited = draft.with_name("editor.swp")
                edited.write_text("To: erik@example.com\nSubject: edited\n\nEdited body\n")
                os.replace(edited, draft)
            return real_move(path, target)

        return move

    # Whichever way the code publishes into sent/, the editor lands just before it.
    for name in ("rename", "replace"):
        monkeypatch.setattr(Path, name, swap_draft_before(getattr(Path, name)))
    agent.send(msg)

    assert len(deliveries) == 1
    assert sent.read_text().startswith(snapshot)
    assert "Edited body" not in sent.read_text()
    assert "Edited body" in draft.read_text()


def test_edit_landing_during_cleanup_is_not_deleted(
    agent: AgentEmail, deliveries: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor that saves the draft while the sender cleans up keeps its file.

    The cleanup must not be read-then-unlink: a save landing between those two
    calls would be deleted without ever being delivered.
    """
    msg = _draft(agent, "cleanup-race", None)
    draft = agent.email_dir / "drafts" / "cleanup-race.md"
    edited = "To: erik@example.com\nSubject: late edit\n\nLate body\n"
    real_unlink = Path.unlink
    wrote = False

    def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal wrote
        if not wrote and path.parent == draft.parent:
            wrote = True
            draft.write_text(edited)  # editor saves a new version mid-cleanup
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    agent.send(msg)

    assert len(deliveries) == 1
    assert wrote
    assert draft.exists()
    assert draft.read_text() == edited


def test_draft_archived_during_delivery_still_completes_the_send(
    agent: AgentEmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """archive() takes no send lock; a delivered reply must still be recorded."""
    msg = _draft(agent, "moved", "<orig@example.com>")
    sent = agent.email_dir / "sent" / "moved.md"
    snapshot = (agent.email_dir / "drafts" / "moved.md").read_text()

    def archive_during_delivery(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        agent.archive(msg)
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", archive_during_delivery)
    agent.send(msg)

    assert sent.read_text().startswith(snapshot)
    assert agent._is_completed("<orig@example.com>")
