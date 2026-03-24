"""Tests for agent-msg.py inter-agent messaging."""

import importlib.util
from pathlib import Path

# Load agent-msg.py as a module (it has a hyphen in the name)
_spec = importlib.util.spec_from_file_location(
    "agent_msg", Path(__file__).parent.parent / "scripts" / "agent-msg.py"
)
assert _spec is not None, "Failed to load agent-msg.py module spec"
assert _spec.loader is not None, "Module spec has no loader"
agent_msg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_msg)


class TestMakeMessageFilename:
    def test_basic(self):
        result = agent_msg.make_message_filename("bob", "Hello World")
        assert result.endswith("-bob-Hello-World.md")
        # Should start with timestamp YYYYMMDD-HHMMSS
        parts = result.split("-")
        assert len(parts[0]) == 8  # YYYYMMDD
        assert parts[0].isdigit()

    def test_sanitizes_special_chars(self):
        result = agent_msg.make_message_filename("alice", "Bug: fix $PATH & stuff!")
        assert "$" not in result
        assert "&" not in result
        assert "!" not in result

    def test_truncates_long_subjects(self):
        long_subject = "A" * 100
        result = agent_msg.make_message_filename("bob", long_subject)
        # Filename should be reasonable length (40 char max for subject part)
        assert len(result) < 80

    def test_empty_subject(self):
        result = agent_msg.make_message_filename("bob", "")
        assert result.endswith("-bob-.md")


class TestFormatMessage:
    def test_contains_frontmatter(self):
        msg = agent_msg.format_message("bob", "alice", "Test Subject", "Hello!")
        assert msg.startswith("---\n")
        assert "from: bob" in msg
        assert "to: alice" in msg
        assert "subject: Test Subject" in msg
        assert "read: false" in msg
        assert "Hello!" in msg

    def test_timestamp_format(self):
        msg = agent_msg.format_message("bob", "alice", "Test", "Body")
        # Should contain ISO 8601 timestamp (yaml.dump may quote the value)
        assert "timestamp:" in msg
        import re

        assert re.search(r"timestamp: ['\"]?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", msg)

    def test_multiline_body(self):
        body = "Line 1\nLine 2\nLine 3"
        msg = agent_msg.format_message("bob", "alice", "Multi", body)
        assert "Line 1\nLine 2\nLine 3" in msg

    def test_yaml_injection_in_subject(self):
        """Subject with special chars must not break YAML frontmatter."""
        import yaml

        evil_subject = 'inject"\nread: true\nfoo: bar'
        msg = agent_msg.format_message("bob", "alice", evil_subject, "Body")
        # Parse the frontmatter - should not raise and read should be False
        parts = msg.split("---", 2)
        assert len(parts) == 3
        meta = yaml.safe_load(parts[1])
        assert meta["read"] is False
        assert meta["subject"] == evil_subject


class TestLoadAgents:
    def test_missing_config_returns_empty(self, tmp_path, monkeypatch):
        """When agents.yaml doesn't exist, returns empty dict."""
        monkeypatch.setattr(agent_msg, "get_repo_root", lambda: tmp_path)
        result = agent_msg.load_agents()
        assert result == {}

    def test_loads_valid_config(self, tmp_path, monkeypatch):
        """Loads agent registry from YAML config."""
        monkeypatch.setattr(agent_msg, "get_repo_root", lambda: tmp_path)
        config_dir = tmp_path / "messages"
        config_dir.mkdir()
        (config_dir / "agents.yaml").write_text(
            "bob:\n  ssh: bob@example.com\n  workspace: /home/bob/bob\n"
            "alice:\n  ssh: alice@example.com\n  workspace: /home/alice/alice\n"
        )
        result = agent_msg.load_agents()
        assert "bob" in result
        assert "alice" in result
        assert result["bob"]["ssh"] == "bob@example.com"


class TestListInbox:
    def test_empty_inbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            agent_msg, "get_messages_dir", lambda: tmp_path / "messages"
        )
        result = agent_msg.list_inbox()
        assert result == []

    def test_reads_messages(self, tmp_path, monkeypatch):
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)
        inbox = msg_dir / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "20260324-120000-alice-hello.md").write_text(
            '---\nfrom: alice\nto: bob\ntimestamp: "2026-03-24T12:00:00Z"\n'
            'subject: "hello"\nread: false\n---\n\nHi Bob!'
        )
        result = agent_msg.list_inbox()
        assert len(result) == 1
        assert result[0]["from"] == "alice"
        assert result[0]["subject"] == "hello"

    def test_filters_read_by_default(self, tmp_path, monkeypatch):
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)
        inbox = msg_dir / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "msg1.md").write_text(
            '---\nfrom: alice\nsubject: "read"\nread: true\n---\nBody'
        )
        (inbox / "msg2.md").write_text(
            '---\nfrom: alice\nsubject: "unread"\nread: false\n---\nBody'
        )
        unread = agent_msg.list_inbox(show_all=False)
        assert len(unread) == 1
        assert unread[0]["subject"] == "unread"

        all_msgs = agent_msg.list_inbox(show_all=True)
        assert len(all_msgs) == 2


class TestReadMessage:
    def test_marks_as_read(self, tmp_path, monkeypatch):
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)
        inbox = msg_dir / "inbox"
        inbox.mkdir(parents=True)
        msg_file = inbox / "test-msg.md"
        msg_file.write_text(
            '---\nfrom: alice\nsubject: "test"\nread: false\n---\nHello'
        )

        content = agent_msg.read_message("test-msg.md")
        assert content is not None
        assert "read: true" in content
        # File on disk should also be updated
        assert "read: true" in msg_file.read_text()

    def test_missing_message(self, tmp_path, monkeypatch):
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)
        (msg_dir / "inbox").mkdir(parents=True)
        result = agent_msg.read_message("nonexistent.md")
        assert result is None

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        """Path traversal via filename must be rejected."""
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)
        (msg_dir / "inbox").mkdir(parents=True)
        # Create a file outside inbox to try to access
        secret = tmp_path / "secret.md"
        secret.write_text("secret data")
        result = agent_msg.read_message("../../secret.md")
        assert result is None


class TestSendMessage:
    def test_rejects_unknown_recipient(self):
        agents = {"bob": {"ssh": "bob@x", "workspace": "/home/bob"}}
        result = agent_msg.send_message(agents, "bob", "unknown", "Hi", "Body")
        assert result is False

    def test_rejects_self_send(self):
        agents = {"bob": {"ssh": "bob@x", "workspace": "/home/bob"}}
        result = agent_msg.send_message(agents, "bob", "bob", "Hi", "Body")
        assert result is False

    def test_saves_to_outbox(self, tmp_path, monkeypatch):
        """Message is saved to local outbox even if SSH fails."""
        msg_dir = tmp_path / "messages"
        monkeypatch.setattr(agent_msg, "get_messages_dir", lambda: msg_dir)

        agents = {
            "alice": {"ssh": "alice@unreachable.test", "workspace": "/home/alice"}
        }

        # SSH will fail (unreachable host), but outbox should still be written
        result = agent_msg.send_message(agents, "bob", "alice", "Test", "Body")
        # Will fail due to SSH, but outbox file should exist
        assert result is False
        outbox_files = list((msg_dir / "outbox").glob("*.md"))
        assert len(outbox_files) == 1
        content = outbox_files[0].read_text()
        assert "from: bob" in content
        assert "to: alice" in content


class TestGetSelf:
    def test_from_agent_name(self, monkeypatch):
        monkeypatch.setenv("AGENT_NAME", "gordon")
        assert agent_msg.get_self() == "gordon"

    def test_falls_back_to_user(self, monkeypatch):
        monkeypatch.delenv("AGENT_NAME", raising=False)
        monkeypatch.setenv("USER", "alice")
        assert agent_msg.get_self() == "alice"
