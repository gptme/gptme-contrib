"""Tests for the ``gptmail agent`` CLI subgroup (agent_cli).

Step 3 of folding agent-msg into gptmail: the CLI surface over ``AgentTransport``
plus ``ConversationTracker`` stamping. Exercised over a temp ``messages/`` dir
with no network and no git — ``_repo_root`` is patched to the temp workspace and
the SSH deliver hook is replaced with a local-copy stub (single-host delivery).

Parity target: ``scripts/agent-msg.py`` (send/broadcast/list/read/reply/pending).
See task: fold-agent-msg-into-gptmail-single-comms-tool.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gptmail import agent_cli
from gptmail.agent_cli import agent
from gptmail.communication_utils.state.tracking import ConversationTracker, MessageState


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp workspace with a messages/ dir, agents.yaml, and patched seams.

    ``alice`` is self; ``bob`` is a peer whose inbox is a local directory so the
    SSH deliver hook can be swapped for a local copy (no network).
    """
    messages = tmp_path / "messages"
    (messages / "inbox").mkdir(parents=True)
    (messages / "outbox").mkdir(parents=True)
    peer_inbox = tmp_path / "bob" / "messages" / "inbox"
    peer_inbox.mkdir(parents=True)
    (messages / "agents.yaml").write_text(
        yaml.dump({"bob": {"ssh": "bob@bob", "workspace": str(tmp_path / "bob")}})
    )

    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)

    # Replace SSH/SCP delivery with a local copy into the peer's inbox.
    def _local_deliver(agents):
        from gptmail.transport.agent import AgentTransport

        return AgentTransport.local_deliver(peer_inbox)

    monkeypatch.setattr(agent_cli, "_ssh_deliver", _local_deliver)
    return tmp_path


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---", 2)[1])


def _only_outbox_msg(workspace: Path) -> Path:
    files = list((workspace / "messages" / "outbox").glob("*.md"))
    assert len(files) == 1, files
    return files[0]


def test_send_writes_outbox_and_delivers(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["send", "bob", "Hello", "body text"])
    assert result.exit_code == 0, result.output
    assert "Sent to bob: Hello" in result.output

    out = _only_outbox_msg(workspace)
    meta = _frontmatter(out)
    assert meta["from"] == "alice"
    assert meta["to"] == "bob"
    assert meta["subject"] == "Hello"
    assert "delivered" not in meta  # successful delivery leaves no failure stamp

    delivered = list((workspace / "bob" / "messages" / "inbox").glob("*.md"))
    assert len(delivered) == 1


def test_send_unknown_agent_errors(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["send", "nobody", "Hi", "x"])
    assert result.exit_code == 1
    assert "unknown agent" in result.output.lower()


@pytest.mark.parametrize(
    "entry",
    [
        {"workspace": "/tmp/bob"},  # missing ssh
        {"ssh": "bob@bob"},  # missing workspace
        {"ssh": "", "workspace": "/tmp/bob"},  # present-but-empty ssh
    ],
)
def test_ssh_deliver_missing_key_returns_false(tmp_path: Path, entry: dict) -> None:
    """A registry entry missing (or empty) ssh/workspace returns False, not KeyError.

    Guards the deliver contract: a malformed agents.yaml stamps ``delivered: false``
    rather than raising an uncaught KeyError that aborts the whole send (greptile
    P1, #1097). Covers the empty-value case the bare ``k not in agent`` guard missed.
    """
    deliver = agent_cli._ssh_deliver({"bob": entry})
    msg = tmp_path / "msg.md"
    msg.write_text("---\nto: bob\n---\n")
    assert deliver(msg, "bob") is False


def test_send_stamps_tracker_channel_agent(workspace: Path) -> None:
    CliRunner().invoke(agent, ["send", "bob", "Hello", "body"])
    tracker = ConversationTracker(workspace / "messages" / ".tracking")
    pending = tracker.get_pending_messages("agent:alice|bob")
    assert len(pending) == 1
    assert pending[0].channel == "agent"


def _seed_inbox(workspace: Path, sender: str = "bob", subject: str = "Q") -> str:
    """Drop a message from ``sender`` into alice's inbox; return its filename."""
    inbox = workspace / "messages" / "inbox"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{ts}-000000-{sender}-{subject}.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (inbox / name).write_text(
        f"---\nfrom: {sender}\nto: alice\n"
        f"timestamp: {timestamp}\nsubject: {subject}\nread: false\n---\n\nplease advise\n"
    )
    return name


def test_list_shows_inbox(workspace: Path) -> None:
    name = _seed_inbox(workspace)
    result = CliRunner().invoke(agent, ["list"])
    assert result.exit_code == 0
    assert name in result.output
    # Unread messages are prefixed with the agent-msg.py "*" marker and the
    # sender, e.g. ``  * [..] bob: Q  (file.md)``.
    assert "* [" in result.output
    assert "bob: Q" in result.output


def test_list_unread_default_hides_read(workspace: Path) -> None:
    """Default ``list`` mirrors agent-msg.py: only unread messages show."""
    name = _seed_inbox(workspace)
    CliRunner().invoke(agent, ["read", name])  # marks it read
    result = CliRunner().invoke(agent, ["list"])
    assert result.exit_code == 0
    assert name not in result.output
    assert "No unread messages." in result.output


def test_list_all_includes_read(workspace: Path) -> None:
    """``--all`` shows read messages too, with a blank (space) marker."""
    name = _seed_inbox(workspace)
    CliRunner().invoke(agent, ["read", name])
    result = CliRunner().invoke(agent, ["list", "--all"])
    assert result.exit_code == 0
    assert name in result.output
    # Read messages render with a blank marker (no leading "*"): the line is
    # ``    [..] bob: Q  (file.md)`` — agent-msg.py's read-marker convention.
    line = next(ln for ln in result.output.splitlines() if name in ln)
    assert not line.lstrip().startswith("*")


def test_list_rejects_path_traversal_folder(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["list", "../secrets"])
    assert result.exit_code != 0
    assert "Invalid folder name" in result.output


def test_list_rejects_symlink_escaping_messages_dir(workspace: Path) -> None:
    """A plainly-named symlink inside messages/ that points outside is rejected.

    The old ``"/" in folder`` string check passed this (the name has no slash);
    the resolve-based guard follows the link and sees it escapes messages/.
    """
    secret = workspace / "secret"
    secret.mkdir()
    (workspace / "messages" / "escape").symlink_to(secret, target_is_directory=True)
    result = CliRunner().invoke(agent, ["list", "escape"])
    assert result.exit_code != 0
    assert "Invalid folder name" in result.output


def test_read_marks_read(workspace: Path) -> None:
    name = _seed_inbox(workspace)
    result = CliRunner().invoke(agent, ["read", name])
    assert result.exit_code == 0
    assert "please advise" in result.output
    assert _frontmatter(workspace / "messages" / "inbox" / name)["read"] is True


def test_pending_lists_unanswered_then_clears_on_reply(workspace: Path) -> None:
    name = _seed_inbox(workspace)

    before = CliRunner().invoke(agent, ["pending"])
    assert "1 message(s) awaiting reply" in before.output
    assert name in before.output

    reply = CliRunner().invoke(agent, ["reply", name, "here is my advice"])
    assert reply.exit_code == 0, reply.output
    assert "Replied to bob: Re: Q" in reply.output

    # Inbox stamped replied + reply delivered + threaded.
    assert _frontmatter(workspace / "messages" / "inbox" / name)["replied"] is True
    out = _only_outbox_msg(workspace)
    assert _frontmatter(out)["in_reply_to"] == name

    after = CliRunner().invoke(agent, ["pending"])
    assert "No messages awaiting reply" in after.output


def test_reply_marks_original_completed_in_tracker(workspace: Path) -> None:
    name = _seed_inbox(workspace)
    CliRunner().invoke(agent, ["reply", name, "advice"])
    tracker = ConversationTracker(workspace / "messages" / ".tracking")
    state = tracker.get_message_state("agent:alice|bob", name)
    assert state is not None
    assert state.state == MessageState.COMPLETED


def test_pending_respects_reply_window(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An old message (well past the 7-day SLA) is not flagged.
    inbox = workspace / "messages" / "inbox"
    (inbox / "old.md").write_text(
        "---\nfrom: bob\nto: alice\ntimestamp: 2020-01-01T00:00:00Z\n"
        "subject: ancient\nread: false\n---\n\nold question\n"
    )
    result = CliRunner().invoke(agent, ["pending"])
    assert "No messages awaiting reply" in result.output


def test_reply_does_not_corrupt_subject_containing_read_false(workspace: Path) -> None:
    # Regression: _mark_replied must anchor its frontmatter rewrite. A subject
    # containing the literal "read: false" must survive replying intact.
    inbox = workspace / "messages" / "inbox"
    name = "20260613-000000-000000-bob-tricky.md"
    (inbox / name).write_text(
        "---\nfrom: bob\nto: alice\ntimestamp: 2026-06-13T00:00:00Z\n"
        'subject: "Auto-retry if read: false"\nread: false\n---\n\nplease advise\n'
    )
    result = CliRunner().invoke(agent, ["reply", name, "advice"])
    assert result.exit_code == 0, result.output
    fm = _frontmatter(inbox / name)
    assert fm["subject"] == "Auto-retry if read: false"  # subject untouched
    assert fm["read"] is True
    assert fm["replied"] is True


def test_status_reports_counts(workspace: Path) -> None:
    _seed_inbox(workspace)
    result = CliRunner().invoke(agent, ["status"])
    assert result.exit_code == 0
    assert "Agent:    alice" in result.output
    assert "Inbox:    1" in result.output
    assert "Pending:  1" in result.output
