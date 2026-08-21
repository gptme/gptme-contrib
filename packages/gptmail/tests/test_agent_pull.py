"""Tests for ``gptmail agent pull`` — pull-only recipient delivery path.

Covers the missing human-side delivery introduced in issue #1476:
- ``pull`` fetches messages from SSH-reachable agents' outboxes into local inbox
- ``pull`` deduplicates by filename (re-pull is idempotent)
- ``pull --json`` emits machine-readable count + file list
- ``pull --notify-cmd`` invokes the hook with NEW_COUNT + SUMMARY env vars
- ``pull --dry-run`` shows what would be fetched without writing
- ``pull --as IDENTITY`` overrides the self-name for cross-identity polls
- Agents missing ssh/workspace keys are skipped silently
- Pull-only agents in the registry are skipped (they have no outbox to SSH into)
"""

import json
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gptmail import agent_cli
from gptmail.agent_cli import agent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_outbox_msg(
    outbox_dir: Path,
    *,
    sender: str,
    recipient: str,
    subject: str,
    mailbox: str = "default",
) -> str:
    """Write a message file into ``outbox_dir``; return filename."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    name = f"{ts}-{sender}-{subject[:20].replace(' ', '_')}.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (outbox_dir / name).write_text(
        f"---\nfrom: {sender}\nto: {recipient}\n"
        f"timestamp: {timestamp}\nsubject: {subject}\nmailbox: {mailbox}\n"
        f"reply_expected: true\n---\n\nPlease advise.\n"
    )
    return name


@pytest.fixture
def pull_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace simulating 'erik' (pull-only) pulling from 'gordon' (SSH-reachable).

    Layout:
      tmp_path/
        erik/messages/inbox/          ← local inbox (where pull writes)
        gordon/messages/outbox/       ← remote outbox (what pull reads)
      erik/messages/agents.yaml       ← gordon registered with ssh+workspace
    """
    erik_dir = tmp_path / "erik"
    gordon_dir = tmp_path / "gordon"

    # Erik's local workspace
    (erik_dir / "messages" / "inbox").mkdir(parents=True)
    (erik_dir / "messages" / "outbox").mkdir(parents=True)
    (erik_dir / "messages" / "agents.yaml").write_text(
        yaml.dump(
            {
                "gordon": {
                    "ssh": "gordon@gordon.lxc",
                    "workspace": str(gordon_dir),
                },
                "alice": {
                    "delivery": "pull-only",
                },
            }
        )
    )

    # Gordon's remote outbox (local stub — no real SSH needed)
    (gordon_dir / "messages" / "outbox").mkdir(parents=True)

    # Patch seams: _repo_root → erik_dir, AGENT_NAME → erik
    monkeypatch.setenv("AGENT_NAME", "erik")
    monkeypatch.setattr(agent_cli, "_repo_root", lambda: erik_dir)

    # Replace _remote_pending_rows with a local-filesystem scan of gordon's outbox
    # (avoids SSH; returns the same dict shape _remote_pending_rows produces).
    def _local_remote_pending_rows(agent_name, agent_cfg, *, recipient, mailboxes, all_mailboxes):
        outbox = Path(agent_cfg["workspace"]) / "messages" / "outbox"
        rows = []
        if not outbox.exists():
            return rows
        for f in sorted(outbox.glob("*.md")):
            content = f.read_text()
            if not content.startswith("---"):
                continue
            parts = content.split("---", 2)
            if len(parts) < 3:
                continue
            import yaml as _yaml

            meta = _yaml.safe_load(parts[1]) or {}
            to = meta.get("to")
            hit = (isinstance(to, list) and recipient.lower() in [str(t).lower() for t in to]) or (
                isinstance(to, str) and to.lower() == recipient.lower()
            )
            if not hit:
                continue
            rows.append(
                {
                    "file": f.name,
                    "subject": str(meta.get("subject", "")),
                    "from": str(meta.get("from", "")),
                    "timestamp": str(meta.get("timestamp", "")),
                    "mailbox": "default",
                    "agent": agent_name,
                    "workspace": agent_cfg["workspace"],
                }
            )
        return rows

    monkeypatch.setattr(agent_cli, "_remote_pending_rows", _local_remote_pending_rows)

    # Replace the SCP subprocess.run inside _fetch_from_agent with a local copy.
    # We intercept subprocess.run and, for SCP commands, do a Path.copy instead.
    original_run = __import__("subprocess").run

    def _stub_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "scp":
            # Find src (contains ':') and dest (last arg)
            src_arg = next((a for a in cmd[1:] if ":" in a and not a.startswith("-")), None)
            dest_arg = cmd[-1]
            if src_arg:
                # Parse "host:/path/to/file" → local equivalent path. SCP's
                # remote path is shell-quoted because the real command crosses
                # a remote shell boundary.
                _, remote_path = src_arg.split(":", 1)
                parsed_path = shlex.split(remote_path)
                src_path = Path(parsed_path[0]) if len(parsed_path) == 1 else Path()
                if src_path.exists():
                    shutil.copy2(src_path, dest_arg)
                    import subprocess

                    return subprocess.CompletedProcess(cmd, 0)
            import subprocess

            return subprocess.CompletedProcess(cmd, 0)
        return original_run(cmd, **kwargs)

    import subprocess

    monkeypatch.setattr(subprocess, "run", _stub_run)

    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pull_fetches_addressed_messages(pull_workspace: Path) -> None:
    """``pull`` copies outbox messages addressed to self into local inbox."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(
        gordon_outbox, sender="gordon", recipient="erik", subject="Action required"
    )

    result = CliRunner().invoke(agent, ["pull"])
    assert result.exit_code == 0, result.output
    assert "1 new message(s) fetched" in result.output
    assert "Action required" in result.output

    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert (local_inbox / name).exists(), "message file must be present in local inbox"


def test_pull_skips_messages_for_others(pull_workspace: Path) -> None:
    """``pull`` ignores outbox messages addressed to a different recipient."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    _write_outbox_msg(gordon_outbox, sender="gordon", recipient="alice", subject="Not for Erik")

    result = CliRunner().invoke(agent, ["pull"])
    assert result.exit_code == 0, result.output
    assert "No new messages." in result.output

    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert not list(local_inbox.glob("*.md")), "inbox must remain empty"


def test_pull_deduplicates_on_repull(pull_workspace: Path) -> None:
    """Re-running ``pull`` does not duplicate already-fetched messages."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    _write_outbox_msg(gordon_outbox, sender="gordon", recipient="erik", subject="Dedup test")

    CliRunner().invoke(agent, ["pull"])  # first pull
    result = CliRunner().invoke(agent, ["pull"])  # second pull
    assert result.exit_code == 0, result.output
    assert "No new messages." in result.output

    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert len(list(local_inbox.glob("*.md"))) == 1, "only one copy must exist"


def test_pull_json_output(pull_workspace: Path) -> None:
    """``pull --json`` emits machine-readable new_count + files list."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(gordon_outbox, sender="gordon", recipient="erik", subject="JSON test")

    result = CliRunner().invoke(agent, ["pull", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["new_count"] == 1
    assert name in payload["files"]
    assert "gordon" in payload["agents_polled"]


def test_pull_json_zero_when_empty(pull_workspace: Path) -> None:
    """``pull --json`` returns new_count=0 when outbox is empty."""
    result = CliRunner().invoke(agent, ["pull", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["new_count"] == 0
    assert payload["files"] == []


def test_pull_notify_cmd_invoked_with_env(
    pull_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--notify-cmd`` is invoked with NEW_COUNT and SUMMARY env vars when there are new messages."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    _write_outbox_msg(gordon_outbox, sender="gordon", recipient="erik", subject="Urgent")

    sentinel = tmp_path / "notify_fired.txt"
    notify_cmd = f'sh -c \'printf "%s %s" "$NEW_COUNT" "$SUMMARY" > {sentinel}\''

    result = CliRunner().invoke(agent, ["pull", "--notify-cmd", notify_cmd])
    assert result.exit_code == 0, result.output
    assert sentinel.exists(), "--notify-cmd must have been invoked"
    content = sentinel.read_text()
    assert content.startswith("1 ")
    assert "Urgent" in content


def test_pull_json_still_invokes_notify_cmd(pull_workspace: Path, tmp_path: Path) -> None:
    """Machine-readable output and notification hooks can be combined."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    _write_outbox_msg(gordon_outbox, sender="gordon", recipient="erik", subject="Urgent")
    sentinel = tmp_path / "notify_fired.txt"

    result = CliRunner().invoke(agent, ["pull", "--json", "--notify-cmd", f"touch {sentinel}"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["new_count"] == 1
    assert sentinel.exists(), "--json must not suppress --notify-cmd"


def test_pull_notify_cmd_failure_warns_after_fetch(pull_workspace: Path) -> None:
    """A missing notification executable must not discard a successful pull."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(
        gordon_outbox, sender="gordon", recipient="erik", subject="Still fetched"
    )

    result = CliRunner().invoke(agent, ["pull", "--notify-cmd", "missing-notifier"])

    assert result.exit_code == 0, result.output
    assert "Warning: --notify-cmd failed" in result.output
    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert (local_inbox / name).exists()


def test_pull_notify_cmd_malformed_value_warns_after_fetch(pull_workspace: Path) -> None:
    """Malformed notification syntax cannot turn a successful pull into failure."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(
        gordon_outbox, sender="gordon", recipient="erik", subject="Still fetched"
    )

    result = CliRunner().invoke(agent, ["pull", "--notify-cmd", "unterminated '"])

    assert result.exit_code == 0, result.output
    assert "Warning: --notify-cmd failed" in result.output
    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert (local_inbox / name).exists()


def test_pull_notify_cmd_not_invoked_when_empty(pull_workspace: Path, tmp_path: Path) -> None:
    """``--notify-cmd`` must NOT be invoked when there are no new messages."""
    sentinel = tmp_path / "notify_fired.txt"
    notify_cmd = f"touch {sentinel}"

    result = CliRunner().invoke(agent, ["pull", "--notify-cmd", notify_cmd])
    assert result.exit_code == 0, result.output
    assert not sentinel.exists(), "--notify-cmd must not fire when new_count=0"


def test_pull_notify_cmd_does_not_shell_expand_message_subject(
    pull_workspace: Path, tmp_path: Path
) -> None:
    """Message subjects stay data even when the hook invokes a shell explicitly."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    injected = tmp_path / "injected"
    filename = _write_outbox_msg(
        gordon_outbox,
        sender="gordon",
        recipient="erik",
        subject="safe filename",
    )
    message = gordon_outbox / filename
    message.write_text(message.read_text().replace("safe filename", f"$(touch {injected})"))

    sentinel = tmp_path / "summary.txt"
    notify_cmd = f"sh -c 'printf %s \"$SUMMARY\" > {sentinel}'"
    result = CliRunner().invoke(agent, ["pull", "--notify-cmd", notify_cmd])

    assert result.exit_code == 0, result.output
    assert not injected.exists()
    assert f"$(touch {injected})" in sentinel.read_text()


def test_pull_dry_run_does_not_write(pull_workspace: Path) -> None:
    """``pull --dry-run`` shows what would be fetched but does not write to inbox."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(
        gordon_outbox, sender="gordon", recipient="erik", subject="Dry run msg"
    )

    result = CliRunner().invoke(agent, ["pull", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "Dry run msg" in result.output

    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert not (local_inbox / name).exists(), "dry-run must not write files"


def test_pull_dry_run_json_is_machine_readable(pull_workspace: Path) -> None:
    """``--dry-run --json`` emits JSON only and lists prospective messages."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    name = _write_outbox_msg(
        gordon_outbox, sender="gordon", recipient="erik", subject="Dry run msg"
    )

    result = CliRunner().invoke(agent, ["pull", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "new_count": 1,
        "files": [name],
        "agents_polled": ["gordon"],
    }
    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    assert not (local_inbox / name).exists()


def test_pull_skips_pull_only_agents(pull_workspace: Path) -> None:
    """Agents with ``delivery: pull-only`` are skipped (they have no SSH outbox to poll)."""
    # alice is registered as pull-only in the fixture — polling alice should be a no-op.
    # Drop a file in a fake alice outbox to confirm nothing is fetched.
    alice_outbox = pull_workspace / "alice" / "messages" / "outbox"
    _write_outbox_msg(alice_outbox, sender="alice", recipient="erik", subject="Skipped")

    result = CliRunner().invoke(agent, ["pull", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "alice" not in payload["agents_polled"]
    assert payload["new_count"] == 0


def test_pull_as_override(pull_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``pull --as IDENTITY`` overrides the effective self-name for the pull."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    _write_outbox_msg(gordon_outbox, sender="gordon", recipient="bob", subject="For Bob")

    # Self is still 'erik', but we pull --as bob — should fetch messages to bob.
    result = CliRunner().invoke(agent, ["pull", "--as", "bob", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["new_count"] == 1, "pull --as bob must fetch message addressed to bob"


@pytest.mark.parametrize("filename", ["../../../../escaped.md", "subdir/message.md"])
def test_pull_refuses_unsafe_remote_filename(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """A remote row cannot escape either the source outbox or local inbox."""
    outside = pull_workspace / "escaped.md"
    scp_calls: list[list[str]] = []
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [{"file": filename, "mailbox": "default"}],
    )
    monkeypatch.setattr(
        agent_cli.subprocess,
        "run",
        lambda cmd, **kwargs: scp_calls.append(cmd),
    )

    result = CliRunner().invoke(agent, ["pull"])

    assert result.exit_code == 0, result.output
    assert "refusing unsafe filename" in result.output
    assert not outside.exists()
    assert scp_calls == []


def test_pull_all_mailboxes_preserves_mailbox_destination(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows from a named mailbox are copied into that mailbox's local inbox."""
    gordon_root = pull_workspace / "gordon" / "messages"
    default_name = _write_outbox_msg(
        gordon_root / "outbox",
        sender="gordon",
        recipient="erik",
        subject="Default",
    )
    ops_name = _write_outbox_msg(
        gordon_root / "mailboxes" / "ops" / "outbox",
        sender="gordon",
        recipient="erik",
        subject="Ops",
        mailbox="ops",
    )
    (pull_workspace / "erik" / "messages" / "mailboxes" / "ops").mkdir(parents=True)
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [
            {"file": default_name, "mailbox": "default"},
            {"file": ops_name, "mailbox": "ops"},
        ],
    )

    result = CliRunner().invoke(agent, ["pull", "--all-mailboxes"])

    assert result.exit_code == 0, result.output
    messages = pull_workspace / "erik" / "messages"
    assert (messages / "inbox" / default_name).exists()
    assert (messages / "mailboxes" / "ops" / "inbox" / ops_name).exists()
    assert not (messages / "inbox" / ops_name).exists()


def test_pull_all_mailboxes_queries_remote_for_remote_only_mailboxes(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--all-mailboxes`` reaches mailboxes that do not exist locally yet."""
    calls: list[tuple[list[str], bool]] = []

    def _remote_rows(*args, **kwargs):
        calls.append((kwargs["mailboxes"], kwargs["all_mailboxes"]))
        return [{"file": "remote-only.md", "mailbox": "ops"}]

    monkeypatch.setattr(agent_cli, "_remote_pending_rows", _remote_rows)

    result = CliRunner().invoke(agent, ["pull", "--all-mailboxes", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls == [([], True)]


def test_pull_quotes_remote_workspace_with_spaces(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SCP remote source remains one path when the workspace contains spaces."""
    filename = "message.md"
    workspace = "/Users/John Doe/workspace"
    scp_calls: list[list[str]] = []
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [{"file": filename, "mailbox": "default"}],
    )

    def _record_scp(cmd, **kwargs):
        scp_calls.append(cmd)
        Path(cmd[-1]).write_text("message")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agent_cli.subprocess, "run", _record_scp)

    new_files = agent_cli._fetch_from_agent(
        "gordon",
        {"ssh": "gordon@example", "workspace": workspace},
        self_name="erik",
        mailboxes=["default"],
        all_mailboxes=False,
    )

    assert len(new_files) == 1
    assert (
        scp_calls[0][-2]
        == f"gordon@example:{shlex.quote(f'{workspace}/messages/outbox/{filename}')}"
    )


def test_missing_scp_warns_and_preserves_partial_results(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing SCP executable is a per-agent warning rather than a CLI crash."""
    filename = "message.md"
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [{"file": filename, "mailbox": "default"}],
    )
    monkeypatch.setattr(
        agent_cli.subprocess,
        "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(FileNotFoundError("scp")),
    )

    result = CliRunner().invoke(agent, ["pull"])

    assert result.exit_code == 0, result.output
    assert "Warning: failed to fetch" in result.output
    destination = pull_workspace / "erik" / "messages" / "inbox" / filename
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{filename}.*"))


def test_failed_scp_removes_partial_destination(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed transfer cannot leave a file that suppresses the next retry."""
    filename = "partial.md"
    destination = pull_workspace / "erik" / "messages" / "inbox" / filename
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [{"file": filename, "mailbox": "default"}],
    )

    def _partial_scp(cmd, **kwargs):
        Path(cmd[-1]).write_text("partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(agent_cli.subprocess, "run", _partial_scp)

    result = CliRunner().invoke(agent, ["pull"])

    assert result.exit_code == 0, result.output
    assert "failed to fetch" in result.output
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{filename}.*"))


def test_pull_concurrent_publish_deduplicates_atomically(
    pull_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent winner is preserved rather than overwritten by this pull."""
    filename = "concurrent.md"
    destination = pull_workspace / "erik" / "messages" / "inbox" / filename
    monkeypatch.setattr(
        agent_cli,
        "_remote_pending_rows",
        lambda *args, **kwargs: [{"file": filename, "mailbox": "default"}],
    )

    def _concurrent_scp(cmd, **kwargs):
        Path(cmd[-1]).write_text("this pull")
        destination.write_text("concurrent winner")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agent_cli.subprocess, "run", _concurrent_scp)

    result = CliRunner().invoke(agent, ["pull", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["new_count"] == 0
    assert destination.read_text() == "concurrent winner"
    assert not list(destination.parent.glob(f".{filename}.*"))


def test_pull_multiple_messages_fetched(pull_workspace: Path) -> None:
    """``pull`` fetches multiple messages in one run."""
    gordon_outbox = pull_workspace / "gordon" / "messages" / "outbox"
    names = [
        _write_outbox_msg(gordon_outbox, sender="gordon", recipient="erik", subject=f"Msg {i}")
        for i in range(3)
    ]

    result = CliRunner().invoke(agent, ["pull"])
    assert result.exit_code == 0, result.output
    assert "3 new message(s) fetched" in result.output

    local_inbox = pull_workspace / "erik" / "messages" / "inbox"
    for name in names:
        assert (local_inbox / name).exists()
