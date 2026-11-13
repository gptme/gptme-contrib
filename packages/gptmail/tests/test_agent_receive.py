"""Tests for the receive-only transport (``gptmail agent receive``).

Covers the recipient trust boundary (bounded read, frontmatter validation,
per-key sender binding, atomic no-clobber publication) and the sender side
(one ``ssh`` invocation with the message on stdin, no scp/mkdir).

No network and no git: the SSH call in the sender tests is a recorded stub, and
the CLI runs against a temp workspace with ``_repo_root`` patched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptmail import agent_cli
from gptmail.agent_cli import agent
from gptmail.transport.receive import (
    ReceiveError,
    check_original_command,
    message_filename,
    publish_message,
)


def _message(
    sender: str = "alice",
    recipient: str = "bob",
    subject: str = "Hello",
    body: str = "Hi Bob.",
) -> str:
    return (
        "---\n"
        f"from: {sender}\n"
        f"to: {recipient}\n"
        "timestamp: 2026-11-13T10:00:00Z\n"
        f"subject: {subject}\n"
        "read: false\n"
        "mailbox: default\n"
        "---\n\n"
        f"{body}\n"
    )


# -- receiver trust boundary -------------------------------------------------


def test_publish_writes_valid_message_atomically(tmp_path: Path) -> None:
    inbox = tmp_path / "messages" / "inbox"
    published = publish_message(_message(), inbox)

    assert published.parent == inbox
    assert published.name.endswith("-alice-Hello.md")
    assert published.read_text() == _message()
    # No staging file may survive a successful publication.
    assert not [p for p in inbox.iterdir() if p.name.startswith(".receive-")]


@pytest.mark.parametrize(
    "raw, match",
    [
        ("no frontmatter here\n", "no valid YAML frontmatter"),
        ("---\nfrom: alice\n---\n\nbody\n", "no 'to' field"),
        ("---\nto: bob\n---\n\nbody\n", "no 'from' field"),
        ("---\nfrom: alice\nto: bob\n---\n\nbody\n", "no 'subject' field"),
        (_message(body="   "), "empty body"),
        (_message().replace("from: alice", "from: ../etc"), "invalid sender name"),
        (_message() + "\x00", "NUL byte"),
    ],
)
def test_publish_rejects_invalid_messages(tmp_path: Path, raw: str, match: str) -> None:
    inbox = tmp_path / "inbox"
    with pytest.raises(ReceiveError, match=match):
        publish_message(raw, inbox)
    assert not inbox.exists() or not list(inbox.iterdir())


def test_expected_sender_binding_is_per_key(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    with pytest.raises(ReceiveError, match="bound to sender 'bob'"):
        publish_message(_message(sender="alice"), inbox, expected_sender="bob")
    published = publish_message(_message(sender="alice"), inbox, expected_sender="Alice")
    assert published.exists()


def test_message_addressed_elsewhere_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReceiveError, match="addressed to 'carol'"):
        publish_message(_message(recipient="carol"), tmp_path, self_name="bob")
    published = publish_message(_message(recipient="Bob"), tmp_path, self_name="bob")
    assert published.exists()


@pytest.mark.parametrize(
    "original, accepted",
    [
        (None, True),
        ("", True),
        ("gptmail agent receive", True),
        ("gptmail agent receive --mailbox work", True),
        ("uv run gptmail agent receive", True),
        ("AGENT_NAME=bob gptmail agent receive", True),
        ("AGENT_NAME=bob uv run gptmail agent receive --expect-sender alice", True),
        ("AGENT_NAME=bob ls", False),
        ("ls", False),
        ("rm -rf /", False),
        ("gptmail agent receive; rm -rf /", False),
        ("gptmail agent pending", False),
    ],
)
def test_original_command_check(original: str | None, accepted: bool) -> None:
    if accepted:
        check_original_command(original)
    else:
        with pytest.raises(ReceiveError, match="receive-only endpoint"):
            check_original_command(original)


def test_publication_never_clobbers_an_existing_message(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = inbox / "20260101-000000-000000-alice-Hello.md"
    first.write_text("original\n")
    staged = tmp_path / "staged"
    staged.write_text("second\n")

    # The receiver's own naming is timestamped; this exercises the no-clobber
    # rule directly (a same-name message must never be replaced).
    from gptmail.transport.receive import _link_no_clobber

    published = _link_no_clobber(staged, inbox, first.name)
    assert published.name == "20260101-000000-000000-alice-Hello-2.md"
    assert first.read_text() == "original\n"
    assert published.read_text() == "second\n"


def test_publication_stamps_receiver_arrival(tmp_path: Path) -> None:
    old = 1_000_000_000
    raw = _message()
    inbox = tmp_path / "inbox"
    published = publish_message(raw, inbox)
    os.utime(published, (old, old))
    republished = publish_message(_message(subject="Second"), inbox)
    assert republished.stat().st_mtime > old


def test_message_filename_is_path_safe() -> None:
    name = message_filename({"from": "../../etc", "subject": "a/b\nc" + "d" * 100}, now=None)
    assert "/" not in name and "\\" not in name and "\n" not in name
    assert name.endswith(".md")
    assert len(name) <= 100


def test_receive_accepts_a_sender_rendered_message(
    tmp_path: Path, clean_agent_scanner: None
) -> None:
    """Integration: bytes the real sender renders must pass the receiver.

    The unit cases use a hand-written message; this feeds the exact output of
    ``AgentTransport.send`` (session id, reply_expected, a subject containing
    separators) through the receiver and then reads it back with a recipient
    transport, so the two halves cannot drift.
    """
    from gptmail.transport.agent import AgentTransport

    sender = AgentTransport(tmp_path / "alice" / "messages", "alice")
    message_id = sender.send(
        "bob", "Release / check: v1.2 & notes", "Line one.\n\n---\n\nLine two.\n"
    )
    raw = (sender.outbox / message_id).read_text()

    published = publish_message(raw, tmp_path / "bob" / "messages" / "inbox", self_name="bob")
    recipient = AgentTransport(tmp_path / "bob" / "messages", "bob")
    assert [name for name, _s, _t in recipient.list_inbox()] == [published.name]
    body = recipient.read(published.name)
    assert "Release / check: v1.2 & notes" in body
    assert "Line two." in body


def test_mailbox_name_traversal_is_refused(tmp_path: Path) -> None:
    from gptmail.transport.receive import mailbox_inbox

    with pytest.raises(ReceiveError, match="invalid mailbox"):
        mailbox_inbox(tmp_path, "../../etc")
    assert mailbox_inbox(tmp_path, "work") == tmp_path / "mailboxes" / "work" / "inbox"


# -- CLI surface -------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "messages" / "inbox").mkdir(parents=True)
    (tmp_path / "messages" / "outbox").mkdir(parents=True)
    monkeypatch.setenv("AGENT_NAME", "bob")
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    monkeypatch.delenv("GPTMAIL_SENDER", raising=False)
    monkeypatch.delenv("GPTMAIL_RECEIVE_MAILBOX", raising=False)
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)
    return tmp_path


def test_receive_publishes_stdin_message(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["receive"], input=_message())
    assert result.exit_code == 0, result.output
    published = workspace / "messages" / "inbox" / result.output.strip()
    assert published.exists()
    assert published.read_text() == _message()


def test_receive_refuses_foreign_ssh_command(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ls -la")
    result = CliRunner().invoke(agent, ["receive"], input="")
    assert result.exit_code == 1
    assert "refused command" in result.output
    assert not list((workspace / "messages" / "inbox").iterdir())


def test_receive_enforces_byte_limit(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["receive", "--max-bytes", "20"], input=_message())
    assert result.exit_code == 1
    assert "exceeds the 20-byte receive limit" in result.output
    assert not list((workspace / "messages" / "inbox").iterdir())


def test_receive_rejects_invalid_utf8(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["receive"], input=b"---\nfrom: \xff\n---\n")
    assert result.exit_code == 1
    assert "not valid UTF-8" in result.output


def test_receive_empty_stdin_is_refused(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["receive"], input="")
    assert result.exit_code == 1
    assert "no message on stdin" in result.output


def test_receive_honours_expect_sender_and_mailbox(workspace: Path) -> None:
    result = CliRunner().invoke(
        agent,
        ["receive", "--expect-sender", "carol", "--mailbox", "work"],
        input=_message(sender="alice"),
    )
    assert result.exit_code == 1
    assert "bound to sender 'carol'" in result.output

    result = CliRunner().invoke(
        agent,
        ["receive", "--expect-sender", "alice", "--mailbox", "work"],
        input=_message(sender="alice"),
    )
    assert result.exit_code == 0, result.output
    published = workspace / "messages" / "mailboxes" / "work" / "inbox" / result.output.strip()
    assert published.exists()


# -- sender side: one ssh invocation, no scp/mkdir ---------------------------


def _capture_subprocess(monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0) -> list[list]:
    calls: list[list] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "stdin": kwargs.get("stdin"), "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=b"", stderr=b"nope")

    monkeypatch.setattr(agent_cli.subprocess, "run", fake_run)
    return calls


def test_receive_only_delivery_is_a_single_ssh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = tmp_path / "msg.md"
    message.write_text(_message())
    calls = _capture_subprocess(monkeypatch)

    deliver = agent_cli._ssh_deliver({"bob": {"ssh": "bob@bob", "delivery": "receive-only"}})
    assert deliver(message, "bob") is True

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "ssh"
    assert argv[-1] == "gptmail agent receive"
    # No remote path, mkdir, scp or cleanup command is needed or sent.
    assert not {"mkdir", "scp", "rm"} & set(argv)
    assert calls[0]["stdin"] is not None


def test_receive_only_delivery_reports_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    message = tmp_path / "msg.md"
    message.write_text(_message())
    _capture_subprocess(monkeypatch, returncode=1)

    deliver = agent_cli._ssh_deliver({"bob": {"ssh": "bob@bob", "delivery": "receive-only"}})
    assert deliver(message, "bob") is False
    err = capsys.readouterr().err
    assert "receive-only endpoint refused" in err


def test_receive_only_delivery_uses_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = tmp_path / "msg.md"
    message.write_text(_message())
    calls = _capture_subprocess(monkeypatch)

    deliver = agent_cli._ssh_deliver(
        {
            "bob": {
                "ssh": "bob@bob",
                "delivery": "receive-only",
                "receive_command": "uv run gptmail agent receive",
            }
        }
    )
    assert deliver(message, "bob") is True
    assert calls[0]["argv"][-1] == "uv run gptmail agent receive"


def test_pending_fleet_skips_receive_only_peers(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pending --fleet`` must not SSH (or warn) at a shell-less peer."""
    import json

    import yaml

    (workspace / "messages" / "agents.yaml").write_text(
        yaml.dump({"carol": {"ssh": "carol@carol", "delivery": "receive-only"}})
    )
    calls: list[tuple] = []
    monkeypatch.setattr(agent_cli, "_remote_pending_rows", lambda *a, **k: calls.append(a) or [])

    result = CliRunner().invoke(agent, ["pending", "--for", "erik", "--fleet", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "missing required key" not in result.output
    assert json.loads(result.stdout) == []


def test_legacy_delivery_still_requires_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = tmp_path / "msg.md"
    message.write_text(_message())
    calls = _capture_subprocess(monkeypatch)

    deliver = agent_cli._ssh_deliver({"bob": {"ssh": "bob@bob"}})
    assert deliver(message, "bob") is False
    assert calls == []


def test_receive_only_round_trip_preserves_thread_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_agent_scanner: None
) -> None:
    from gptmail.transport.agent import AgentTransport, meta_of

    alice_root = tmp_path / "alice"
    bob_root = tmp_path / "bob"
    alice = AgentTransport(alice_root / "messages", "alice")
    original_id = alice.send("bob", "Question", "Original question")
    original = publish_message(
        (alice.outbox / original_id).read_text(), bob_root / "messages" / "inbox"
    )
    assert original.name != original_id
    monkeypatch.setenv("AGENT_NAME", "bob")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: bob_root)
    monkeypatch.setattr(agent_cli, "_load_agents", lambda **kw: {})
    monkeypatch.setattr(agent_cli, "_ssh_deliver", lambda *a, **kw: lambda *a: True)
    result = CliRunner().invoke(agent, ["reply", original.name, "Answer"])
    assert result.exit_code == 0, result.output
    bob = AgentTransport(bob_root / "messages", "bob")
    reply_path = next(bob.outbox.glob("*.md"))
    assert meta_of(reply_path)["in_reply_to"] == original_id
    received_reply = publish_message(reply_path.read_text(), alice.inbox)
    assert "Original question" in alice.read(received_reply.name, include_thread=True)
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: alice_root)
    parents, messages = agent_cli._local_thread_messages(["default"])
    target = agent_cli._thread_key(original_id)
    assert any(agent_cli._reaches_thread(key, target, parents) for key, _, _ in messages)


def test_failed_receive_only_reply_stays_pending(workspace: Path) -> None:
    from gptmail.transport.agent import AgentTransport

    inbox = workspace / "messages" / "inbox"
    original = publish_message(_message(), inbox)
    transport = AgentTransport(workspace / "messages", "bob")
    reply = transport.outbox / "reply.md"
    reply.write_text(
        "---\nfrom: bob\nto: alice\nsubject: Re: Hello\n"
        f"in_reply_to: {original.name}\ndelivered: false\n---\n\nAnswer\n"
    )
    assert agent_cli._is_push_reachable(
        "alice", {"alice": {"ssh": "alice@alice", "delivery": "receive-only"}}
    )
    pending = agent_cli._pending_messages(
        workspace / "messages",
        "bob",
        0,
        agents={"alice": {"ssh": "alice@alice", "delivery": "receive-only"}},
        no_reply_subject_patterns=[],
    )
    assert [message["file"] for message in pending] == [original.name]


def test_receive_selects_workspace_without_git_or_login_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.setenv("AGENT_NAME", "bob")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "gptmail agent receive")
    result = CliRunner().invoke(
        agent,
        ["receive", "--workspace", str(root), "--expect-sender", "alice"],
        input=_message(),
    )
    assert result.exit_code == 0, result.output
    assert (root / "messages" / "inbox" / result.output.strip()).exists()
    assert not (home / "messages").exists()


def test_general_watch_observes_new_receive_only_push(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_cli,
        "_load_agents",
        lambda: {"alice": {"ssh": "alice@alice", "delivery": "receive-only"}},
    )

    def arrive(_seconds):
        publish_message(_message(), workspace / "messages" / "inbox")

    monkeypatch.setattr(agent_cli.time, "sleep", arrive)
    result = CliRunner().invoke(agent, ["watch", "--once", "--pull", "--timeout", "1s"])
    assert result.exit_code == 0, result.output
    assert "Hello" in result.output


def test_pull_skips_logical_id_already_received(workspace: Path, monkeypatch) -> None:
    raw = _message().replace("from: alice", "message_id: original.md\nfrom: alice")
    original = publish_message(raw, workspace / "messages" / "inbox")
    agent_cli._mark_replied(original)
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *a, **kw: [
            {"file": "original.md", "from": "alice", "to": "bob", "mailbox": "default"}
        ],
    )

    def no_copy(*a, **kw):
        pytest.fail("Already-received logical ID must not be fetched again")

    monkeypatch.setattr(agent_cli.subprocess, "run", no_copy)
    new, failed, unreachable = agent_cli._fetch_from_agent(
        "alice",
        {"ssh": "alice@alice", "workspace": "/srv/alice"},
        self_name="bob",
        mailboxes=["default"],
        all_mailboxes=False,
    )
    assert new == [] and not failed and not unreachable
    assert len(list(original.parent.glob("*.md"))) == 1


def test_watch_local_filename_recognizes_logical_ancestor(workspace: Path, monkeypatch) -> None:
    original = publish_message(
        _message().replace("from: alice", "message_id: original.md\nfrom: alice"),
        workspace / "messages" / "inbox",
    )
    (workspace / "messages" / "outbox" / "reply.md").write_text(
        "---\nfrom: bob\nto: alice\nsubject: Re\nin_reply_to: original.md\n---\n\nAnswer\n"
    )
    followup = publish_message(
        _message(subject="Followup").replace("from: alice", "in_reply_to: reply.md\nfrom: alice"),
        original.parent,
    )
    monkeypatch.setattr(
        agent_cli,
        "_load_agents",
        lambda: {"alice": {"ssh": "alice@alice", "delivery": "receive-only"}},
    )
    result = CliRunner().invoke(
        agent, ["watch", "--thread", original.name, "--once", "--timeout", "0.01s"]
    )
    assert result.exit_code == 0, result.output
    assert followup.name in result.output


def test_receive_only_refuses_implicit_named_mailbox_routing(tmp_path: Path, monkeypatch) -> None:
    message = tmp_path / "message.md"
    message.write_text(_message())
    calls = _capture_subprocess(monkeypatch)
    deliver = agent_cli._ssh_deliver(
        {"bob": {"ssh": "bob@bob", "delivery": "receive-only"}}, mailbox="ops"
    )
    assert deliver(message, "bob") is False
    assert calls == []


def test_receive_preserves_existing_logical_message_and_ack(tmp_path: Path) -> None:
    from gptmail.transport.agent import meta_of

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    raw = _message().replace("from: alice", "message_id: original.md\nfrom: alice")
    existing = inbox / "original.md"
    existing.write_text(raw)
    agent_cli._mark_replied(existing)
    result = publish_message(raw, inbox)
    assert result == existing
    assert meta_of(result)["replied"] is True
    assert list(inbox.glob("*.md")) == [existing]


def test_general_watch_keeps_timely_push_at_deadline(workspace: Path, monkeypatch) -> None:
    import time

    now = [time.monotonic()]
    monkeypatch.setattr(agent_cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        agent_cli,
        "_load_agents",
        lambda: {"alice": {"ssh": "alice@alice", "delivery": "receive-only"}},
    )

    def arrive(seconds):
        publish_message(_message(), workspace / "messages" / "inbox")
        now[0] += seconds

    monkeypatch.setattr(agent_cli.time, "sleep", arrive)
    result = CliRunner().invoke(agent, ["watch", "--once", "--timeout", "1s"])
    assert result.exit_code == 0, result.output
    assert "Hello" in result.output
