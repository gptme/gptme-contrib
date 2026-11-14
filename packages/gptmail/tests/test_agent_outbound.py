"""Synthetic outbound checks at the filesystem publication/delivery boundary."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gptmail.transport.agent import AgentTransport

SECRET = "synthetic-peer-forbidden-literal"


@pytest.fixture
def scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.ModuleType("redact")
    stub.collect_secret_literals = lambda: {SECRET}  # type: ignore[attr-defined]
    stub.redact = lambda text, literals: SimpleNamespace(  # type: ignore[attr-defined]
        blocked=SECRET in text,
        block_reasons=["1× literal (critical)"] if SECRET in text else [],
    )
    monkeypatch.setitem(sys.modules, "redact", stub)


@pytest.mark.parametrize("field", ["subject", "body", "reply"])
def test_forbidden_message_never_published_or_delivered(
    tmp_path: Path, scanner: None, field: str
) -> None:
    delivered: list[tuple[Path, str]] = []

    def deliver(path: Path, recipient: str) -> bool:
        delivered.append((path, recipient))
        return True

    transport = AgentTransport(tmp_path / "messages", "alice", deliver=deliver)
    if field == "reply":
        (transport.inbox / "parent.md").write_text(
            "---\nfrom: bob\nsubject: Question\n---\n\nordinary question\n"
        )
    with pytest.raises(ValueError, match="Outbound agent message blocked"):
        if field == "reply":
            transport.send("bob", "Re: Question", SECRET, reply_to="parent.md")
        else:
            transport.send(
                "bob",
                SECRET if field == "subject" else "Hello",
                SECRET if field == "body" else "ordinary text",
            )
    assert delivered == []
    assert list(transport.outbox.iterdir()) == []
    ledger = tmp_path / "state" / "outbound-redact-blocks.jsonl"
    assert '"channel": "agent"' in ledger.read_text()
    assert SECRET not in ledger.read_text()


@pytest.mark.parametrize("failure", ["missing", "import", "collector", "scan"])
@pytest.mark.parametrize("local_only", [False, True])
def test_scanner_failure_is_private_and_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    local_only: bool,
) -> None:
    import builtins

    real_import = builtins.__import__

    def broken_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "redact":
            if failure == "missing":
                raise ModuleNotFoundError(SECRET, name="redact")
            if failure == "import":
                raise ImportError(SECRET)
        return real_import(name, *args, **kwargs)

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError(SECRET)

    stub = types.ModuleType("redact")
    stub.collect_secret_literals = fail if failure == "collector" else lambda: set()  # type: ignore[attr-defined]
    stub.redact = fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redact", stub)
    monkeypatch.setattr(builtins, "__import__", broken_import)
    calls: list[str] = []

    def deliver(path: Path, recipient: str) -> bool:
        calls.append(recipient)
        return True

    transport = AgentTransport(
        tmp_path / "messages", "alice", deliver=None if local_only else deliver
    )
    # A missing scanner is a dependency error, not a policy block: it says so.
    expected = (
        "Outbound agent message not sent"
        if failure == "missing"
        else "Outbound agent message blocked"
    )
    with pytest.raises(ValueError, match=expected) as exc:
        transport.send("bob", "Hello", "ordinary text")
    assert SECRET not in str(exc.value)
    assert SECRET not in caplog.text
    assert not calls
    assert not list(transport.outbox.iterdir())
    assert not (tmp_path / "state" / "outbound-redact-blocks.jsonl").exists()


@pytest.mark.parametrize("local_only", [False, True])
def test_clean_send_and_reply_still_work(tmp_path: Path, scanner: None, local_only: bool) -> None:
    peer = tmp_path / "peer-inbox"
    transport = AgentTransport(
        tmp_path / "messages",
        "alice",
        mailbox="ops",
        deliver=None if local_only else AgentTransport.local_deliver(peer),
    )
    first = transport.send("bob", "Hello", "ordinary text")
    reply = transport.send("bob", "Re: Hello", "ordinary reply", reply_to=first)
    assert (transport.outbox / first).exists()
    assert "in_reply_to:" in (transport.outbox / reply).read_text()
    assert len(list(transport.outbox.iterdir())) == 2
    if local_only:
        assert not peer.exists()
    else:
        assert (peer / first).read_bytes() == (transport.outbox / first).read_bytes()
        assert (peer / reply).read_bytes() == (transport.outbox / reply).read_bytes()


def test_local_only_secret_is_also_blocked(tmp_path: Path, scanner: None) -> None:
    transport = AgentTransport(tmp_path / "messages", "alice")
    with pytest.raises(ValueError, match="Outbound agent message blocked"):
        transport.send("bob", "Hello", SECRET)
    assert not list(transport.outbox.iterdir())


@pytest.mark.parametrize("field", ["sender", "recipient", "reply_to"])
def test_metadata_only_secret_is_rejected_before_filename(
    tmp_path: Path, scanner: None, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Use a lowercase literal so sender/recipient normalization preserves it.
    transport = AgentTransport(tmp_path / "messages", SECRET if field == "sender" else "alice")

    def forbidden_filename(*args: object) -> str:
        pytest.fail("A blocked message must not reach filename generation")

    monkeypatch.setattr(transport, "_make_filename", forbidden_filename)
    with pytest.raises(ValueError, match="Outbound agent message blocked"):
        transport.send(
            SECRET if field == "recipient" else "bob",
            "Hello",
            "ordinary body",
            reply_to=SECRET if field == "reply_to" else None,
        )
    assert not list(transport.outbox.iterdir())


def test_scanner_checks_exact_published_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned: list[str] = []
    stub = types.ModuleType("redact")
    stub.collect_secret_literals = lambda: set()  # type: ignore[attr-defined]

    def scan(text: str, literals: object) -> SimpleNamespace:
        scanned.append(text)
        return SimpleNamespace(blocked=False, block_reasons=[])

    stub.redact = scan  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redact", stub)
    transport = AgentTransport(tmp_path / "messages", "alice", mailbox="ops")
    mid = transport.send("bob", "Unicode ☃ subject", "body\nwith a newline", reply_to="prior.md")
    # Preflight blocks secret-bearing metadata before filename generation;
    # the final scan covers the added logical ID and exact published bytes.
    assert len(scanned) == 2
    assert scanned[-1] == (transport.outbox / mid).read_text()
    assert "mailbox: ops" in scanned[0]
    assert "in_reply_to: prior.md" in scanned[0]


def test_real_scanner_with_synthetic_literal_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Optional workspace integration. Never inspect an inherited secret inventory.
    import os

    monkeypatch.setattr(os, "environ", {})
    pytest.importorskip("redact")
    monkeypatch.setenv("SYNTHETIC_TEST_API_KEY", SECRET)
    transport = AgentTransport(tmp_path / "messages", "alice")
    with pytest.raises(ValueError, match="Outbound agent message blocked"):
        transport.send("bob", "Hello", SECRET)
    assert not list(transport.outbox.iterdir())
    assert SECRET not in (tmp_path / "state" / "outbound-redact-blocks.jsonl").read_text()
    assert (transport.outbox / transport.send("bob", "Hello", "ordinary text")).exists()
