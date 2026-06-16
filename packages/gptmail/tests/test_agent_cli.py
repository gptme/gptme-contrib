"""Tests for the ``gptmail agent`` CLI subgroup (agent_cli).

Step 3 of folding agent-msg into gptmail: the CLI surface over ``AgentTransport``
plus ``ConversationTracker`` stamping. Exercised over a temp ``messages/`` dir
with no network and no git — ``_repo_root`` is patched to the temp workspace and
the SSH deliver hook is replaced with a local-copy stub (single-host delivery).

Parity target: ``scripts/agent-msg.py`` (send/broadcast/list/read/reply/pending).
See task: fold-agent-msg-into-gptmail-single-comms-tool.
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gptmail import agent_cli
from gptmail.agent_cli import agent
from gptmail.communication_utils.state.tracking import ConversationTracker, MessageState
from gptmail.transport.agent import meta_of


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
    def _local_deliver(agents, *, mailbox="default"):
        from gptmail.transport.agent import AgentTransport

        def _deliver(local_path: Path, _recipient: str) -> bool:
            meta = meta_of(local_path) or {}
            target_mailbox = str(meta.get("mailbox", mailbox))
            if target_mailbox == "default":
                destination = peer_inbox
            else:
                destination = tmp_path / "bob" / "messages" / "mailboxes" / target_mailbox / "inbox"
            return AgentTransport.local_deliver(destination)(local_path, _recipient)

        return _deliver

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


def test_send_to_self_blocked(workspace: Path) -> None:
    """Self-send is rejected (regression: agent-msg.py had this guard, port lost it)."""
    result = CliRunner().invoke(agent, ["send", "alice", "Hi", "x"])
    assert result.exit_code == 1
    assert "yourself" in result.output.lower()
    # Nothing written to the outbox.
    assert not list((workspace / "messages" / "outbox").glob("*.md"))


def test_send_reports_delivery_failure_and_exits_nonzero(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed delivery must NOT print 'Sent' and must exit non-zero.

    Core-infra reliability: the transport stamps ``delivered: false`` on a failed
    deliver hook; the CLI must surface that rather than reporting false success.
    """

    def _failing_deliver(_agents, *, mailbox="default"):
        def _deliver(_local_path: Path, _recipient: str) -> bool:
            return False

        return _deliver

    monkeypatch.setattr(agent_cli, "_ssh_deliver", _failing_deliver)
    result = CliRunner().invoke(agent, ["send", "bob", "Hello", "body"])
    assert result.exit_code == 1, result.output
    assert "Sent to bob" not in result.output
    assert "failed" in result.output.lower()
    # The message is still saved to the outbox, stamped delivered: false.
    out = _only_outbox_msg(workspace)
    assert _frontmatter(out)["delivered"] is False


def test_reply_reports_delivery_failure_and_keeps_pending(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reply delivery must NOT mark the original as replied and must exit non-zero.

    Mirror of test_send_reports_delivery_failure_and_exits_nonzero for the reply command.
    The inbox message must stay in ``pending`` so the sender still has a reminder to follow up.
    """

    def _failing_deliver(_agents, *, mailbox="default"):
        def _deliver(_local_path: Path, _recipient: str) -> bool:
            return False

        return _deliver

    name = _seed_inbox(workspace)
    monkeypatch.setattr(agent_cli, "_ssh_deliver", _failing_deliver)
    result = CliRunner().invoke(agent, ["reply", name, "here is my advice"])
    assert result.exit_code == 1, result.output
    assert "Replied to bob" not in result.output
    assert "failed" in result.output.lower()
    # The original inbox message must NOT be marked replied — it stays in pending.
    fm = _frontmatter(workspace / "messages" / "inbox" / name)
    assert not fm.get("replied"), "inbox message must stay unreplied on delivery failure"
    # The ConversationTracker must NOT mark the original as COMPLETED — delivery never happened.
    tracker = ConversationTracker(workspace / "messages" / ".tracking")
    state = tracker.get_message_state("agent:alice|bob", name)
    assert (
        state is None or state.state != MessageState.COMPLETED
    ), "tracker must not mark original as COMPLETED when delivery failed"


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


def test_ssh_deliver_pull_only_returns_true(tmp_path: Path) -> None:
    """A pull-only registry entry returns True without any SSH/SCP attempt.

    Pull-only recipients (e.g. Erik, who polls outboxes) have no inbound SSH
    target.  Returning True signals successful 'delivery' so the outbox message
    is NOT stamped ``delivered: false``.
    """
    deliver = agent_cli._ssh_deliver({"erik": {"delivery": "pull-only"}})
    msg = tmp_path / "msg.md"
    msg.write_text("---\nto: erik\n---\n\nbody\n")
    assert deliver(msg, "erik") is True


def test_send_to_pull_only_recipient_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``send`` to a pull-only recipient writes to outbox, exits 0, no failure stamp.

    Regression for the 'unknown agent' exit-1 bug: Erik is registered as pull-only
    (no ssh/workspace keys), so ``send``/``reply`` must write to the outbox and
    return 0 rather than failing with "unknown agent" or stamping delivered:false.
    """
    messages = tmp_path / "messages"
    (messages / "inbox").mkdir(parents=True)
    (messages / "outbox").mkdir(parents=True)
    (messages / "agents.yaml").write_text(yaml.dump({"erik": {"delivery": "pull-only"}}))
    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)
    # Do NOT patch _ssh_deliver — we want the real pull-only path to run.

    result = CliRunner().invoke(agent, ["send", "erik", "Hello Erik", "body text"])
    assert result.exit_code == 0, result.output
    assert "Sent to erik" in result.output

    files = list((messages / "outbox").glob("*.md"))
    assert len(files) == 1
    meta = yaml.safe_load(files[0].read_text().split("---", 2)[1])
    assert meta["to"] == "erik"
    assert "delivered" not in meta  # pull-only returns True → no failure stamp


def test_send_stamps_tracker_channel_agent(workspace: Path) -> None:
    CliRunner().invoke(agent, ["send", "bob", "Hello", "body"])
    tracker = ConversationTracker(workspace / "messages" / ".tracking")
    pending = tracker.get_pending_messages("agent:alice|bob")
    assert len(pending) == 1
    assert pending[0].channel == "agent"


def _seed_inbox(
    workspace: Path,
    sender: str = "bob",
    subject: str = "Q",
    *,
    mailbox: str = "default",
) -> str:
    """Drop a message from ``sender`` into alice's inbox; return its filename."""
    inbox = workspace / "messages" / "inbox"
    if mailbox != "default":
        inbox = workspace / "messages" / "mailboxes" / mailbox / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{ts}-000000-{sender}-{subject}.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (inbox / name).write_text(
        f"---\nfrom: {sender}\nto: alice\n"
        f"timestamp: {timestamp}\nsubject: {subject}\nread: false\nmailbox: {mailbox}\n---\n\nplease advise\n"
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


def test_pending_excludes_replies_to_my_messages(workspace: Path) -> None:
    """Replies to my own sent messages must not appear in pending.

    If bob replies to something I sent (in_reply_to points at my outbox ID), that
    message is bob answering me — not a new request requiring my reply. Without this
    filter, every ack or response from a peer inflates the pending count.
    """
    # Alice sends first, producing an outbox message.
    CliRunner().invoke(agent, ["send", "bob", "Question", "what do you think?"])
    outbox_files = list((workspace / "messages" / "outbox").glob("*.md"))
    assert len(outbox_files) == 1
    my_msg_id = outbox_files[0].name

    # Bob replies — inbox message whose in_reply_to points at alice's outbox message.
    inbox = workspace / "messages" / "inbox"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply_name = "20260613-120000-000000-bob-Re-Question.md"
    (inbox / reply_name).write_text(
        f"---\nfrom: bob\nto: alice\ntimestamp: {now_iso}\n"
        f"subject: Re: Question\nread: false\nin_reply_to: {my_msg_id}\n---\n\nbob's answer\n"
    )

    result = CliRunner().invoke(agent, ["pending"])
    assert "No messages awaiting reply" in result.output, result.output


def test_pending_reply_once_for_pull_based_recipient(workspace: Path) -> None:
    """An undelivered reply to a pull-based recipient satisfies reply-once.

    Regression for the duplicate-reply bug: a recipient absent from the registry
    (e.g. Erik, who polls the outbox) can never be delivered to, so replies stay
    ``delivered: false`` permanently. Counting only delivered replies made every
    poll re-compose a fresh, non-identical reply. A draft reply to a pull-based
    recipient must clear the inbound message from pending.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inbox = workspace / "messages" / "inbox"
    msg = "20260615-120000-000000-erik-question.md"
    (inbox / msg).write_text(
        f"---\nfrom: erik\nto: alice\ntimestamp: {now_iso}\n"
        "subject: question\nread: false\n---\n\nplease advise\n"
    )
    # We drafted a reply, but delivery is impossible (erik is not in the registry).
    outbox = workspace / "messages" / "outbox"
    (outbox / "20260615-120100-000000-alice-Re-question.md").write_text(
        f"---\nfrom: alice\nto: erik\ntimestamp: {now_iso}\n"
        f"subject: 'Re: question'\nin_reply_to: {msg}\ndelivered: false\n---\n\nmy reply\n"
    )
    result = CliRunner().invoke(agent, ["pending"])
    assert "No messages awaiting reply" in result.output, result.output


def test_pending_keeps_undelivered_reply_to_push_recipient(workspace: Path) -> None:
    """An undelivered reply to a push-reachable recipient stays pending (retry).

    ``bob`` is registered with ssh+workspace, so ``delivered: false`` means a
    transient delivery failure, not a permanent pull-based state — the message
    must remain pending so the next cycle retries delivery.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inbox = workspace / "messages" / "inbox"
    msg = "20260615-120000-000000-bob-question.md"
    (inbox / msg).write_text(
        f"---\nfrom: bob\nto: alice\ntimestamp: {now_iso}\n"
        "subject: question\nread: false\n---\n\nplease advise\n"
    )
    outbox = workspace / "messages" / "outbox"
    (outbox / "20260615-120100-000000-alice-Re-question.md").write_text(
        f"---\nfrom: alice\nto: bob\ntimestamp: {now_iso}\n"
        f"subject: 'Re: question'\nin_reply_to: {msg}\ndelivered: false\n---\n\nmy reply\n"
    )
    result = CliRunner().invoke(agent, ["pending"])
    assert "1 message(s) awaiting reply" in result.output, result.output
    assert msg in result.output


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


def test_send_named_mailbox_writes_mailbox_outbox_and_list_shows_it(workspace: Path) -> None:
    result = CliRunner().invoke(agent, ["send", "--mailbox", "ops", "bob", "Hello", "body text"])
    assert result.exit_code == 0, result.output

    outbox = workspace / "messages" / "mailboxes" / "ops" / "outbox"
    files = list(outbox.glob("*.md"))
    assert len(files) == 1
    meta = _frontmatter(files[0])
    assert meta["mailbox"] == "ops"

    listed = CliRunner().invoke(agent, ["list", "outbox", "--mailbox", "ops", "--all"])
    assert listed.exit_code == 0, listed.output
    assert files[0].name in listed.output
    assert "[ops]" in listed.output


def test_pending_named_mailbox_reply_preserves_mailbox_and_clears_only_that_mailbox(
    workspace: Path,
) -> None:
    default_msg = _seed_inbox(workspace, subject="Default")
    ops_msg = _seed_inbox(workspace, subject="Ops", mailbox="ops")

    before = CliRunner().invoke(agent, ["pending", "--mailbox", "ops"])
    assert before.exit_code == 0, before.output
    assert ops_msg in before.output
    assert default_msg not in before.output

    reply = CliRunner().invoke(agent, ["reply", ops_msg, "handled"])
    assert reply.exit_code == 0, reply.output
    assert "Replied to bob: Re: Ops" in reply.output

    ops_inbox = workspace / "messages" / "mailboxes" / "ops" / "inbox" / ops_msg
    assert _frontmatter(ops_inbox)["replied"] is True

    ops_outbox = list((workspace / "messages" / "mailboxes" / "ops" / "outbox").glob("*.md"))
    assert len(ops_outbox) == 1
    assert _frontmatter(ops_outbox[0])["mailbox"] == "ops"

    after_ops = CliRunner().invoke(agent, ["pending", "--mailbox", "ops"])
    assert "No messages awaiting reply" in after_ops.output

    after_default = CliRunner().invoke(agent, ["pending"])
    assert default_msg in after_default.output


def test_pending_all_mailboxes_combines_default_and_named(workspace: Path) -> None:
    default_msg = _seed_inbox(workspace, subject="Default")
    ops_msg = _seed_inbox(workspace, subject="Ops", mailbox="ops")

    result = CliRunner().invoke(agent, ["pending", "--all-mailboxes"])
    assert result.exit_code == 0, result.output
    assert "2 message(s) awaiting reply" in result.output
    assert default_msg in result.output
    assert ops_msg in result.output
    assert "[default]" in result.output
    assert "[ops]" in result.output


def test_pending_stays_silent_without_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = tmp_path / "messages"
    (messages / "inbox").mkdir(parents=True)
    (messages / "outbox").mkdir(parents=True)
    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)

    result = CliRunner().invoke(agent, ["pending"])
    assert result.exit_code == 0
    assert "Warning: no agent registry" not in result.output
    assert "No messages awaiting reply." in result.output


def test_status_warns_once_without_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = tmp_path / "messages"
    (messages / "inbox").mkdir(parents=True)
    (messages / "outbox").mkdir(parents=True)
    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)

    result = CliRunner().invoke(agent, ["status"])
    assert result.exit_code == 0
    assert result.output.count("Warning: no agent registry") == 1


@pytest.fixture
def multi_peer_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with two peers (bob, gordon) plus self (alice) in the registry.

    Delivery is routed locally per-recipient so ``broadcast`` can be exercised
    without SSH or spamming real agents (the task's stated reason it went
    untested live). ``alice`` appears in agents.yaml to verify self-exclusion.
    """
    messages = tmp_path / "messages"
    (messages / "inbox").mkdir(parents=True)
    (messages / "outbox").mkdir(parents=True)

    peers = ["bob", "gordon"]
    inboxes = {}
    registry = {"alice": {"ssh": "alice@alice", "workspace": str(tmp_path)}}
    for name in peers:
        inbox = tmp_path / name / "messages" / "inbox"
        inbox.mkdir(parents=True)
        inboxes[name] = inbox
        registry[name] = {"ssh": f"{name}@{name}", "workspace": str(tmp_path / name)}
    (messages / "agents.yaml").write_text(yaml.dump(registry))

    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: tmp_path)

    def _routing_deliver(_agents, *, mailbox="default"):
        def _deliver(local_path: Path, recipient: str) -> bool:
            dest = inboxes.get(recipient)
            if dest is None:
                return False
            meta = meta_of(local_path) or {}
            target_mailbox = str(meta.get("mailbox", mailbox))
            if target_mailbox == "default":
                final_dest = dest
            else:
                final_dest = (
                    tmp_path / recipient / "messages" / "mailboxes" / target_mailbox / "inbox"
                )
            final_dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, final_dest / local_path.name)
            return True

        return _deliver

    monkeypatch.setattr(agent_cli, "_ssh_deliver", _routing_deliver)
    return tmp_path


def test_broadcast_delivers_to_all_peers_except_self(multi_peer_workspace: Path) -> None:
    """Broadcast reaches every registry peer but not self; outbox has one per peer."""
    result = CliRunner().invoke(agent, ["broadcast", "Standup", "all hands"])
    assert result.exit_code == 0, result.output
    assert "Sent to bob: Standup" in result.output
    assert "Sent to gordon: Standup" in result.output

    # One outbox record per peer (bob + gordon), none addressed to self.
    outbox = list((multi_peer_workspace / "messages" / "outbox").glob("*.md"))
    assert len(outbox) == 2
    recipients = {_frontmatter(f)["to"] for f in outbox}
    assert recipients == {"bob", "gordon"}
    assert "alice" not in recipients

    # Each peer inbox received exactly one message.
    for name in ("bob", "gordon"):
        delivered = list((multi_peer_workspace / name / "messages" / "inbox").glob("*.md"))
        assert len(delivered) == 1, name
        assert "delivered" not in _frontmatter(delivered[0])  # no failure stamp


def test_broadcast_reports_partial_delivery_failure(
    multi_peer_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If one recipient's delivery fails, broadcast exits non-zero and names it.

    Core-infra reliability: a partially-failed broadcast must not look like full
    success. The reachable peer still gets the message; the failed one is stamped
    ``delivered: false`` and surfaced.
    """

    def _bob_only_deliver(_agents, *, mailbox="default"):
        def _deliver(local_path: Path, recipient: str) -> bool:
            if recipient != "bob":
                return False  # gordon unreachable
            meta = meta_of(local_path) or {}
            target_mailbox = str(meta.get("mailbox", mailbox))
            if target_mailbox == "default":
                dest = multi_peer_workspace / "bob" / "messages" / "inbox"
            else:
                dest = (
                    multi_peer_workspace
                    / "bob"
                    / "messages"
                    / "mailboxes"
                    / target_mailbox
                    / "inbox"
                )
                dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest / local_path.name)
            return True

        return _deliver

    monkeypatch.setattr(agent_cli, "_ssh_deliver", _bob_only_deliver)
    result = CliRunner().invoke(agent, ["broadcast", "Standup", "all hands"])
    assert result.exit_code == 1, result.output
    assert "Sent to bob: Standup" in result.output
    assert "Delivery to gordon FAILED" in result.output
    assert "Broadcast incomplete: 1 delivery failure(s)." in result.output

    # gordon's outbox record is stamped delivered: false; bob's is not.
    by_to = {
        _frontmatter(f)["to"]: _frontmatter(f)
        for f in (multi_peer_workspace / "messages" / "outbox").glob("*.md")
    }
    assert by_to["gordon"]["delivered"] is False
    assert "delivered" not in by_to["bob"]
