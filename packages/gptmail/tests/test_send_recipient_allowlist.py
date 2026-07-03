"""Tests for the outbound recipient allowlist on gptmail.send()."""

from pathlib import Path

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path) -> AgentEmail:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    return AgentEmail(str(tmp_path), "bob@gptme.org")


# ---------------------------------------------------------------------------
# _is_allowlisted_recipient unit tests
# ---------------------------------------------------------------------------


def test_default_allowlist_allows_erik(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert agent._is_allowlisted_recipient("erik@bjareho.lt")


def test_default_allowlist_blocks_stranger(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert not agent._is_allowlisted_recipient("attacker@evil.com")


def test_default_allowlist_allows_own_email(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    """Agent can send email to itself (self-reply pattern)."""
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert agent._is_allowlisted_recipient("bob@gptme.org")


def test_env_override_allows_custom_recipient(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", "custom@example.com,other@example.com")
    assert agent._is_allowlisted_recipient("custom@example.com")
    assert not agent._is_allowlisted_recipient("erik@bjareho.lt")


def test_wildcard_allows_all(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", "*")
    assert agent._is_allowlisted_recipient("anyone@anywhere.net")


def test_empty_recipient_blocked(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert not agent._is_allowlisted_recipient("")


def test_name_plus_addr_format_allowed(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    """'Display Name <email>' format is parsed and checked correctly."""
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert agent._is_allowlisted_recipient("Erik Bjäreholt <erik@bjareho.lt>")


def test_name_plus_addr_format_blocked(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert not agent._is_allowlisted_recipient("Attacker <attacker@evil.com>")


def test_plus_tag_stripped(agent: AgentEmail, monkeypatch: pytest.MonkeyPatch):
    """erik+tag@bjareho.lt matches the base address."""
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)
    assert agent._is_allowlisted_recipient("erik+newsletter@bjareho.lt")


# ---------------------------------------------------------------------------
# Integration: send() blocks non-allowlisted recipient
# ---------------------------------------------------------------------------


def test_send_blocks_non_allowlisted(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """send() raises ValueError when recipient is not allowlisted."""
    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)

    # Write a minimal draft
    draft_dir = tmp_path / "email" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_id = "test-send-block"
    draft_path = draft_dir / f"{draft_id}.md"
    draft_path.write_text("To: attacker@evil.com\n" "Subject: Test\n" "\n" "Hello\n")

    with pytest.raises(ValueError, match="not in the send allowlist"):
        agent.send(draft_id)


def test_send_allows_allowlisted_recipient(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """send() passes the allowlist check and reaches the SMTP stage.

    We mock subprocess.run so no real email is sent; the absence of a
    'not in the send allowlist' ValueError is the assertion.
    """
    import subprocess

    monkeypatch.delenv("EMAIL_SEND_ALLOWLIST", raising=False)

    draft_dir = tmp_path / "email" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_id = "test-send-ok"
    draft_path = draft_dir / f"{draft_id}.md"
    draft_path.write_text("To: erik@bjareho.lt\n" "Subject: Hello\n" "\n" "Hi Erik!\n")

    # Stub out the actual SMTP delivery so no real email is sent.
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})())

    # Should NOT raise "not in the send allowlist"
    try:
        agent.send(draft_id)
    except ValueError as e:
        assert "not in the send allowlist" not in str(e), f"Unexpected block: {e}"
