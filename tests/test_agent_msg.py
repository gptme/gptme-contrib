"""Tests for agent-msg.py shim and gptmail.agent_cli messaging API."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from gptmail.agent_cli import (
    _addressed_to,
    _mark_read,
    _mark_replied,
    _pending_messages,
)
from gptmail.transport.agent import meta_of

# Load the shim module to test its _translate() CLI-compat mapping.
# The shim delegates real work to gptmail.agent_cli; _translate() is the only
# logic that lives in the shim itself.
_shim_spec = importlib.util.spec_from_file_location(
    "agent_msg_shim", Path(__file__).parent.parent / "scripts" / "agent-msg.py"
)
assert _shim_spec is not None and _shim_spec.loader is not None
_shim = importlib.util.module_from_spec(_shim_spec)
_shim_spec.loader.exec_module(_shim)


# ---------------------------------------------------------------------------
# _translate(): legacy agent-msg.py CLI → gptmail agent subcommand mapping
# ---------------------------------------------------------------------------


class TestTranslate:
    def test_list_needs_reply_becomes_pending(self):
        assert _shim._translate(["list", "--needs-reply"]) == ["pending"]

    def test_list_without_flag_passes_through(self):
        assert _shim._translate(["list"]) == ["list"]

    def test_empty_argv(self):
        assert _shim._translate([]) == []

    def test_send_passes_through(self):
        assert _shim._translate(["send", "alice", "Hi", "body"]) == [
            "send",
            "alice",
            "Hi",
            "body",
        ]

    def test_status_passes_through(self):
        assert _shim._translate(["status"]) == ["status"]

    def test_broadcast_passes_through(self):
        assert _shim._translate(["broadcast", "Subject", "Body"]) == [
            "broadcast",
            "Subject",
            "Body",
        ]

    def test_extra_flags_carried_to_pending(self):
        # Additional flags after --needs-reply must be forwarded to `pending`
        result = _shim._translate(["list", "--needs-reply", "--mailbox", "work"])
        assert result == ["pending", "--mailbox", "work"]

    def test_other_list_flags_preserved(self):
        assert _shim._translate(["list", "--all"]) == ["list", "--all"]

    def test_list_unread_is_noop_alias(self):
        assert _shim._translate(["list", "--unread"]) == ["list"]

    def test_list_unread_preserves_other_args(self):
        assert _shim._translate(["list", "--unread", "--mailbox", "ops"]) == [
            "list",
            "--mailbox",
            "ops",
        ]

    def test_needs_reply_wins_over_unread_alias(self):
        # --unread must be stripped when routing to `pending` (pending doesn't accept it)
        assert _shim._translate(["list", "--unread", "--needs-reply"]) == ["pending"]


# ---------------------------------------------------------------------------
# meta_of(): YAML frontmatter parser (from transport.agent)
# ---------------------------------------------------------------------------


class TestMetaOf:
    def test_parses_frontmatter(self, tmp_path):
        msg = tmp_path / "m.md"
        msg.write_text(
            "---\nfrom: bob\nto: alice\nsubject: hi\nread: false\n---\n\nBody"
        )
        meta = meta_of(msg)
        assert meta is not None
        assert meta["from"] == "bob"
        assert meta["to"] == "alice"
        assert meta["read"] is False

    def test_returns_none_for_plain_text(self, tmp_path):
        (tmp_path / "m.md").write_text("Just plain text without frontmatter")
        assert meta_of(tmp_path / "m.md") is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert meta_of(tmp_path / "nonexistent.md") is None


# ---------------------------------------------------------------------------
# _mark_read(): stamp inbox messages as read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def _msg(self, path: Path) -> Path:
        path.write_text("---\nfrom: bob\nto: alice\nread: false\n---\n\nBody")
        return path

    def test_flips_read_false_to_true(self, tmp_path):
        msg = self._msg(tmp_path / "m.md")
        _mark_read(msg)
        content = msg.read_text()
        assert "read: true" in content
        assert "read: false" not in content

    def test_preserves_read_false_in_body(self, tmp_path):
        """read: false appearing in the message body must not be rewritten."""
        msg = tmp_path / "m.md"
        msg.write_text(
            "---\nfrom: bob\nto: alice\nread: false\n---\n\nThis has read: false in body"
        )
        _mark_read(msg)
        content = msg.read_text()
        assert "read: true" in content
        assert "This has read: false in body" in content

    def test_idempotent(self, tmp_path):
        msg = self._msg(tmp_path / "m.md")
        _mark_read(msg)
        _mark_read(msg)
        assert msg.read_text().count("read: true") == 1

    def test_missing_file_is_noop(self, tmp_path):
        _mark_read(tmp_path / "nonexistent.md")  # must not raise


# ---------------------------------------------------------------------------
# _mark_replied(): stamp inbox messages as replied (also marks read)
# ---------------------------------------------------------------------------


class TestMarkReplied:
    def _msg(self, path: Path) -> Path:
        path.write_text("---\nfrom: bob\nto: alice\nread: false\n---\n\nBody")
        return path

    def test_stamps_replied_and_read(self, tmp_path):
        msg = self._msg(tmp_path / "m.md")
        _mark_replied(msg)
        content = msg.read_text()
        assert "replied: true" in content
        assert "read: true" in content

    def test_idempotent(self, tmp_path):
        msg = self._msg(tmp_path / "m.md")
        _mark_replied(msg)
        _mark_replied(msg)
        assert msg.read_text().count("replied: true") == 1

    def test_missing_file_is_noop(self, tmp_path):
        _mark_replied(tmp_path / "nonexistent.md")  # must not raise


# ---------------------------------------------------------------------------
# _addressed_to(): addressing predicate
# ---------------------------------------------------------------------------


class TestAddressedTo:
    def test_scalar_match(self):
        assert _addressed_to({"to": "alice"}, "alice") is True

    def test_scalar_no_match(self):
        assert _addressed_to({"to": "gordon"}, "alice") is False

    def test_list_match(self):
        assert _addressed_to({"to": ["alice", "gordon"]}, "alice") is True

    def test_list_no_match(self):
        assert _addressed_to({"to": ["gordon", "sven"]}, "alice") is False

    def test_missing_to_is_addressed(self):
        # Legacy frontmatter without a `to:` field is treated as addressed-to-self.
        assert _addressed_to({}, "alice") is True

    def test_scalar_case_insensitive_to(self):
        # `to:` is stored lowercase; the comparison lowercases `to`, not self_name.
        # In production, _self_name() always lowercases the agent name, so pass
        # lowercase here to match real usage.
        assert _addressed_to({"to": "alice"}, "alice") is True

    def test_list_case_insensitive_to(self):
        assert _addressed_to({"to": ["ALICE", "gordon"]}, "alice") is True


# ---------------------------------------------------------------------------
# _pending_messages(): core pending-reply detection
# ---------------------------------------------------------------------------


class TestPendingMessages:
    @staticmethod
    def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
        messages_dir = tmp_path / "messages"
        inbox = messages_dir / "inbox"
        outbox = messages_dir / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)
        return messages_dir, inbox, outbox

    @staticmethod
    def _write_inbox(path: Path, *, from_: str = "bob", to: str = "alice") -> None:
        path.write_text(
            f'---\nfrom: {from_}\nto: {to}\ntimestamp: "2026-06-13T10:00:00Z"\n'
            "subject: hi\nread: false\n---\n\nBody"
        )

    def test_unread_message_appears(self, tmp_path):
        messages_dir, inbox, _ = self._setup(tmp_path)
        self._write_inbox(inbox / "m.md")
        result = _pending_messages(messages_dir, "alice", window=0)
        assert len(result) == 1

    def test_no_inbox_returns_empty(self, tmp_path):
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir()
        assert _pending_messages(messages_dir, "alice", window=0) == []

    def test_replied_stamp_clears_pending(self, tmp_path):
        messages_dir, inbox, _ = self._setup(tmp_path)
        msg = inbox / "m.md"
        self._write_inbox(msg)
        _mark_replied(msg)
        assert _pending_messages(messages_dir, "alice", window=0) == []

    def test_message_from_self_excluded(self, tmp_path):
        messages_dir, inbox, _ = self._setup(tmp_path)
        (inbox / "m.md").write_text(
            '---\nfrom: alice\nto: alice\ntimestamp: "2026-06-13T10:00:00Z"\n'
            "subject: hi\nread: false\n---\n\nBody"
        )
        assert _pending_messages(messages_dir, "alice", window=0) == []

    def test_outbox_reply_clears_pending(self, tmp_path):
        messages_dir, inbox, outbox = self._setup(tmp_path)
        self._write_inbox(inbox / "m.md")
        (outbox / "reply.md").write_text(
            '---\nfrom: alice\nto: bob\nsubject: "Re: hi"\n'
            "in_reply_to: m.md\ndelivered: true\n---\n\nok"
        )
        assert _pending_messages(messages_dir, "alice", window=0) == []

    def test_failed_delivery_stays_pending_for_push_reachable(self, tmp_path):
        """delivered:false for a push-reachable recipient must not clear pending.

        Regression: a failed SCP delivery was incorrectly clearing the pending
        flag, causing Bob to silently drop the reply requirement.
        """
        messages_dir, inbox, outbox = self._setup(tmp_path)
        self._write_inbox(inbox / "m.md")
        (outbox / "reply.md").write_text(
            '---\nfrom: alice\nto: bob\nsubject: "Re: hi"\n'
            "in_reply_to: m.md\ndelivered: false\n---\n\nok"
        )
        agents = {"bob": {"ssh": "bob@x", "workspace": "/home/bob"}}
        result = _pending_messages(messages_dir, "alice", window=0, agents=agents)
        assert len(result) == 1

    def test_broadcast_message_appears(self, tmp_path):
        messages_dir, inbox, _ = self._setup(tmp_path)
        (inbox / "m.md").write_text(
            '---\nfrom: bob\nto: [alice, gordon]\ntimestamp: "2026-06-13T10:00:00Z"\n'
            "subject: hi\nread: false\n---\n\nBody"
        )
        result = _pending_messages(messages_dir, "alice", window=0)
        assert len(result) == 1

    def test_case_insensitive_self_name(self, tmp_path):
        """Mixed-case AGENT_NAME must match the stored lowercase to: field.

        Regression for Greptile P1: the comparison was case-sensitive, silently
        dropping all pending output for agents with capitalised AGENT_NAME.
        _self_name() normalises to lowercase in the CLI; this test guards that
        the pending path still works when called with a lowercase name.
        """
        messages_dir, inbox, _ = self._setup(tmp_path)
        self._write_inbox(inbox / "m.md", to="alice")
        # _self_name() lowercases in production — pass lowercase to match real usage
        result = _pending_messages(messages_dir, "alice", window=0)
        assert len(result) == 1

    def test_broadcast_case_insensitive_self_name(self, tmp_path):
        messages_dir, inbox, _ = self._setup(tmp_path)
        (inbox / "m.md").write_text(
            '---\nfrom: bob\nto: [alice, gordon]\ntimestamp: "2026-06-13T10:00:00Z"\n'
            "subject: hi\nread: false\n---\n\nBody"
        )
        result = _pending_messages(messages_dir, "alice", window=0)
        assert len(result) == 1
