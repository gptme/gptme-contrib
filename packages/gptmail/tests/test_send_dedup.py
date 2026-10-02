"""Send-site duplicate guard for AgentEmail.send().

Concurrent sessions can each decide an inbox email is unreplied; the guard has
to live at the send site, not in the dispatch-time check.
"""

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
