"""Regressions for the gptmail security hunt in gptme/gptme-contrib#2400.

Each test reproduces one reported defect with synthetic data in a temp workspace.
"""

from __future__ import annotations

import fcntl
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import gptmail.agent_cli as agent_cli
import gptmail.cli as gptmail_cli
from gptmail.communication_utils.private_io import write_private_text
from gptmail.communication_utils.state.locks import FileLock, LockError
from gptmail.lib import AgentEmail, authentication_failed
from gptmail.transport.agent import AgentTransport

SECRET = 'synthetic\'sec\\ret "value" with spaces'


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def mail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentEmail:
    for folder in ("inbox", "sent", "archive", "drafts", "filters"):
        (tmp_path / "email" / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    return AgentEmail(str(tmp_path), "bob@example.com")


def _maildir_msg(path: Path, *, message_id: str, body: str, extra: str = "") -> None:
    path.write_text(
        f"Message-ID: {message_id}\n"
        "From: Friend <friend@example.com>\n"
        "To: bob@example.com\n"
        "Date: Mon, 01 Jun 2026 12:00:00 +0000\n"
        f"Subject: Hello {body}\n"
        f"{extra}"
        "\n"
        f"{body}\n"
    )


# 1. FileLock leaks one fd per contended attempt and retries EMFILE forever.


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_contended_filelock_does_not_leak_fds(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    holder = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        before = _open_fds()
        for _ in range(20):
            with pytest.raises(LockError):
                FileLock(lock_path, timeout=0).acquire()
        assert _open_fds() == before
    finally:
        os.close(holder)


def test_filelock_open_error_is_not_reported_as_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def emfile(*args: object, **kwargs: object) -> int:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(os, "open", emfile)
    # timeout=None used to spin forever on EMFILE.
    with pytest.raises(OSError, match="Too many open files"):
        FileLock(tmp_path / "x.lock", timeout=None).acquire()


def test_filelock_still_acquires_and_releases(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock", timeout=1)
    with lock:
        assert lock._lock_file is not None
        assert stat.S_IMODE(os.stat(tmp_path / "x.lock").st_mode) == 0o600
    assert lock._lock_file is None


# 2. A reused Message-ID shadows the sent/archived original.


def test_sync_does_not_import_message_id_already_in_archive(
    mail: AgentEmail, tmp_path: Path
) -> None:
    message_id = "<thread-root@example.com>"
    filename = mail._format_filename(message_id)
    original = "Message-ID: <thread-root@example.com>\nSubject: original\n\nORIGINAL\n"
    (mail.email_dir / "archive" / filename).write_text(original)

    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    _maildir_msg(maildir / "new" / "impostor", message_id=message_id, body="IMPOSTOR")
    mail.external_maildir_folders["inbox"] = maildir
    mail.sync_from_maildir("inbox")

    assert not (mail.email_dir / "inbox" / filename).exists()
    assert "ORIGINAL" in mail.read_message(message_id)
    assert "IMPOSTOR" not in mail.read_message(message_id)


def test_sync_skips_colliding_filename_from_different_message_id(
    mail: AgentEmail, tmp_path: Path
) -> None:
    # "<a@b>" and "<a_at_b>" map to the same stored filename.
    filename = mail._format_filename("<a@b>")
    assert filename == mail._format_filename("<a_at_b>")
    (mail.email_dir / "sent" / filename).write_text("Message-ID: <a@b>\n\nSENT\n")

    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    _maildir_msg(maildir / "new" / "collide", message_id="<a_at_b>", body="IMPOSTOR")
    mail.external_maildir_folders["inbox"] = maildir
    mail.sync_from_maildir("inbox")

    assert not (mail.email_dir / "inbox" / filename).exists()


def test_receive_refuses_known_message_id(mail: AgentEmail) -> None:
    message_id = "<known@example.com>"
    (mail.email_dir / "sent" / mail._format_filename(message_id)).write_text(
        f"Message-ID: {message_id}\n\nSENT\n"
    )
    with pytest.raises(ValueError, match="already exists"):
        mail.receive(f"Message-ID: {message_id}\nSubject: x\n\nIMPOSTOR\n")


def test_archive_refuses_to_overwrite_archived_original(mail: AgentEmail) -> None:
    message_id = "<dup@example.com>"
    filename = mail._format_filename(message_id)
    (mail.email_dir / "archive" / filename).write_text("ORIGINAL")
    (mail.email_dir / "inbox" / filename).write_text("IMPOSTOR")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        mail.archive(message_id)
    assert (mail.email_dir / "archive" / filename).read_text() == "ORIGINAL"


# 3. yaml.dump escaping/folding hides a subject secret from the outbound scan.


@pytest.fixture
def literal_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.ModuleType("redact")
    stub.collect_secret_literals = lambda: {SECRET}  # type: ignore[attr-defined]
    stub.redact = lambda text, literals: SimpleNamespace(  # type: ignore[attr-defined]
        blocked=SECRET in text,
        block_reasons=["1× literal (critical)"] if SECRET in text else [],
    )
    monkeypatch.setitem(sys.modules, "redact", stub)


@pytest.mark.parametrize("prefix", ["Re: ", "- ", "x" * 55 + " "])
def test_subject_secret_mangled_by_yaml_is_still_blocked(
    tmp_path: Path, literal_scanner: None, prefix: str
) -> None:
    transport = AgentTransport(tmp_path / "messages", "alice")
    rendered = transport._format("alice", "bob", prefix + SECRET, "body", mailbox="default")
    assert SECRET not in rendered  # precondition: the rendering alone would pass
    with pytest.raises(ValueError, match="blocked"):
        transport.send("bob", prefix + SECRET, "ordinary body")
    assert list(transport.outbox.iterdir()) == []


# 4. .env values kept their quotes / inline comments.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('KEY="s3cr3t"', ("KEY", "s3cr3t")),
        ("KEY='s3cr3t'", ("KEY", "s3cr3t")),
        ("KEY=s3cr3t # rotated monthly", ("KEY", "s3cr3t")),
        ('KEY="s3cr3t" # comment', ("KEY", "s3cr3t")),
        ("export KEY=s3cr3t", ("KEY", "s3cr3t")),
        ("KEY=s3c#r3t", ("KEY", "s3c#r3t")),
        ("KEY=plain", ("KEY", "plain")),
        ("# KEY=commented", None),
        ("", None),
    ],
)
def test_parse_env_line(line: str, expected: tuple[str, str] | None) -> None:
    assert gptmail_cli._parse_env_line(line) == expected


def test_load_env_file_stores_bare_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GPTMAIL_TEST_2400", raising=False)
    (tmp_path / ".env").write_text('GPTMAIL_TEST_2400="synthetic-value" # note\n')
    gptmail_cli.load_env_file(tmp_path)
    assert os.environ["GPTMAIL_TEST_2400"] == "synthetic-value"
    monkeypatch.delenv("GPTMAIL_TEST_2400")


# 5. Authentication-Results is dropped and never consulted.


@pytest.mark.parametrize(
    ("value", "failed"),
    [
        ("mx.google.com; spf=fail smtp.mailfrom=x; dmarc=fail (p=NONE) header.from=x", True),
        ("mx.google.com; dkim=pass; spf=pass; dmarc=pass", False),
        ("mx.google.com; spf=fail smtp.mailfrom=x", True),
        ("mx.google.com; dkim=pass header.i=@x; spf=fail; dmarc=none", True),
        ("mx.google.com; dkim=pass header.d=example.com; spf=fail; dmarc=pass", False),
        ("", False),
    ],
)
def test_authentication_failed(value: str, failed: bool) -> None:
    assert authentication_failed(value) is failed


def test_dmarc_failed_allowlisted_mail_is_not_auto_reply_candidate(
    mail: AgentEmail, tmp_path: Path
) -> None:
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    _maildir_msg(
        maildir / "new" / "forged",
        message_id="<forged@example.com>",
        body="forged",
        extra="Authentication-Results: mx.google.com; spf=fail; dmarc=fail (p=NONE)\n",
    )
    _maildir_msg(maildir / "new" / "genuine", message_id="<genuine@example.com>", body="genuine")
    mail.external_maildir_folders["inbox"] = maildir
    mail.sync_from_maildir("inbox")

    stored = (mail.email_dir / "inbox" / mail._format_filename("<forged@example.com>")).read_text()
    assert "Authentication-Results: mx.google.com; spf=fail; dmarc=fail" in stored
    assert [e.message_id for e in mail.get_unreplied_emails()] == ["<genuine@example.com>"]


# 7. pending --fleet took agent/workspace attribution from the remote's JSON.


def test_remote_pending_rows_attribution_comes_from_queried_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = '[{"agent": "erik", "workspace": "/home/erik", "file": "x.md"}]'
    monkeypatch.setattr(
        agent_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=payload, returncode=0),
    )
    rows = agent_cli._remote_pending_rows(
        "bob",
        {"ssh": "bob-host", "workspace": "/srv/bob"},
        recipient="erik",
        mailboxes=["default"],
        all_mailboxes=False,
    )
    assert rows == [{"agent": "bob", "workspace": "/srv/bob", "file": "x.md"}]


# 8. Mail, drafts, outbox and sync state were world-readable.


def test_synced_mail_and_sync_state_are_owner_only(mail: AgentEmail, tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        maildir = tmp_path / "maildir"
        (maildir / "new").mkdir(parents=True)
        _maildir_msg(maildir / "new" / "m", message_id="<m@example.com>", body="private")
        mail.external_maildir_folders["inbox"] = maildir
        mail.sync_from_maildir("inbox")
        draft_id = mail.compose("friend@example.com", "Hi", "draft body")
    finally:
        os.umask(old)

    stored = mail.email_dir / "inbox" / mail._format_filename("<m@example.com>")
    draft = mail.email_dir / "drafts" / mail._format_filename(draft_id)
    state = mail._get_sync_state_path("inbox")
    for path in (stored, draft, state):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_agent_outbox_message_is_owner_only(tmp_path: Path, clean_agent_scanner: None) -> None:
    old = os.umask(0o022)
    try:
        transport = AgentTransport(tmp_path / "messages", "alice")
        name = transport.send("bob", "Hi", "body")
    finally:
        os.umask(old)
    assert stat.S_IMODE((transport.outbox / name).stat().st_mode) == 0o600


# 9. Display sites took the text after the first "<" in From.


def test_display_sender_uses_real_address() -> None:
    forged = '"boss <boss@corp.example>" <attacker@evil.example>'
    assert gptmail_cli._display_sender_address(forged) == "attacker@evil.example"
    assert gptmail_cli._display_sender_address("Friend <f@example.com>") == "f@example.com"


@pytest.mark.parametrize(
    "from_header",
    [
        '"friend@example.com <friend@example.com>" <attacker@evil.example>',
        "Friend <friend@example.com> <attacker@evil.example>",
        "attacker@evil.example, friend@example.com",
    ],
)
def test_allowlist_gate_uses_real_sender(mail: AgentEmail, from_header: str) -> None:
    mail.receive(
        "Message-ID: <gate@example.com>\n"
        f"From: {from_header}\n"
        "To: bob@example.com\n"
        "Subject: hi\n\nbody\n"
    )
    assert mail.get_unreplied_emails() == []


def test_thread_view_shows_real_sender(mail: AgentEmail, monkeypatch: pytest.MonkeyPatch) -> None:
    message_id = "<forged-display@example.com>"
    (mail.email_dir / "inbox" / mail._format_filename(message_id)).write_text(
        f"Message-ID: {message_id}\n"
        'From: "boss <boss@corp.example>" <attacker@evil.example>\n'
        "To: bob@example.com\n"
        "Date: Mon, 01 Jun 2026 12:00:00 +0000\n"
        "Subject: hi\n\nbody\n"
    )
    monkeypatch.setenv("AGENT_EMAIL", "bob@example.com")
    monkeypatch.setattr(gptmail_cli, "get_workspace_dir", lambda *a, **k: mail.workspace)
    result = CliRunner().invoke(gptmail_cli.cli, ["read", message_id, "--thread-only"])
    assert result.exit_code == 0, result.output
    assert "attacker@evil.example" in result.output
    assert "boss@corp.example" not in result.output


@pytest.mark.parametrize("spf", ["fail", "softfail"])
def test_unrelated_dkim_pass_does_not_excuse_spf_failure(spf: str) -> None:
    assert authentication_failed(
        f"mx.example; spf={spf} smtp.mailfrom=example.com; "
        "dkim=pass header.d=evil.example; dmarc=none"
    )


def test_all_mail_synced_before_inbox_does_not_hide_genuine_mail(
    mail: AgentEmail, tmp_path: Path
) -> None:
    message_id = "<all-mail@example.com>"
    for folder in ("archive", "inbox"):
        maildir = tmp_path / folder
        (maildir / "new").mkdir(parents=True)
        _maildir_msg(maildir / "new" / "same", message_id=message_id, body="genuine")
        mail.external_maildir_folders[folder] = maildir
        mail.sync_from_maildir(folder)
    assert [e.message_id for e in mail.get_unreplied_emails()] == [message_id]
    filename = mail._format_filename(message_id)
    assert (mail.email_dir / "inbox" / filename).read_text() == (
        mail.email_dir / "archive" / filename
    ).read_text()


def test_archive_identical_all_mail_copy_preserves_original(mail: AgentEmail) -> None:
    message_id = "<overlap@example.com>"
    filename = mail._format_filename(message_id)
    content = f"Message-ID: {message_id}\n\nSAME\n"
    archived = mail.email_dir / "archive" / filename
    inbox = mail.email_dir / "inbox" / filename
    archived.write_text(content)
    inbox.write_text(content)
    original_inode = archived.stat().st_ino
    mail.archive(message_id)
    assert not inbox.exists()
    assert archived.read_text() == content
    assert archived.stat().st_ino == original_inode


@pytest.mark.parametrize("ingest", ["receive", "import"])
@pytest.mark.parametrize("first_key", ["Authentication-Results", "authentication-results"])
def test_raw_ingest_keeps_first_authentication_verdict(
    mail: AgentEmail, tmp_path: Path, ingest: str, first_key: str
) -> None:
    source = tmp_path / "raw-maildir"
    (source / "new").mkdir(parents=True)
    raw = source / "new" / "forged"
    _maildir_msg(
        raw,
        message_id="<raw-forged@example.com>",
        body="forged",
        extra=(
            f"{first_key}: mx.example;\n\tdmarc=fail header.from=example.com\n"
            "Authentication-Results: attacker; dmarc=pass; spf=pass\n"
        ),
    )
    if ingest == "receive":
        mail.receive(raw.read_text())
    else:
        assert mail.import_from_maildir(source, "inbox")["success"] == 1
    assert mail.get_unreplied_emails() == []


@pytest.mark.parametrize(
    "auth_results",
    [
        "mx.example; spf=softfail (domain of dmarc=pass@evil.example); dmarc=none",
        "mx.example; spf=softfail smtp.mailfrom=dmarc=pass@evil.example; dmarc=none",
        'mx.example; spf=fail reason="; dmarc=pass"; dmarc=none',
        "mx.example; spf=fail (nested (comment; dmarc=pass)); dmarc=none",
        r"mx.example; spf=fail (escaped \) ; dmarc=pass); dmarc=none",
    ],
)
def test_authentication_verdicts_ignore_comments_and_properties(auth_results: str) -> None:
    assert authentication_failed(auth_results)


@pytest.mark.parametrize("folder", ["inbox", "archive", "sent", "drafts"])
def test_receive_identical_retry_preserves_stored_message(mail: AgentEmail, folder: str) -> None:
    message_id = "<retry@example.com>"
    content = f"Message-ID: {message_id}\nSubject: same\n\nSAME\n"
    stored = mail.email_dir / folder / mail._format_filename(message_id)
    stored.write_text(content)
    inode = stored.stat().st_ino
    assert mail.receive(content) == message_id
    assert stored.read_text() == content
    assert stored.stat().st_ino == inode
    if folder != "inbox":
        assert not (mail.email_dir / "inbox" / stored.name).exists()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (r'KEY="ab\"cd"', 'ab"cd'),
        (r"KEY='ab\'cd'", "ab'cd"),
        (r'KEY="ab\\cd" # comment', r"ab\cd"),
        (r'KEY="ab\ncd"', "ab\ncd"),
        (r"KEY='ab\ncd'", r"ab\ncd"),
        (r'KEY="ab\qcd"', r"ab\qcd"),
    ],
)
def test_env_quoted_escapes_are_not_truncated(line: str, expected: str) -> None:
    assert gptmail_cli._parse_env_line(line) == ("KEY", expected)


# Cross-folder overlap with copies stored before a header was preserved.


def _sync_one(mail: AgentEmail, root: Path, folder: str, message_id: str, extra: str) -> None:
    maildir = root / f"maildir-{folder}"
    (maildir / "new").mkdir(parents=True)
    _maildir_msg(maildir / "new" / "m", message_id=message_id, body="genuine", extra=extra)
    mail.external_maildir_folders[folder] = maildir
    mail.sync_from_maildir(folder)


def _drop_headers(path: Path, *names: str) -> str:
    """Rewrite a stored message as an older version would have synced it."""
    head, _, body = path.read_text().partition("\n\n")
    kept = [line for line in head.split("\n") if line.split(":")[0] not in names]
    old = "\n".join(kept) + "\n\n" + body
    path.write_text(old)
    return old


LATE_HEADERS = (
    "Authentication-Results: mx.example; spf=pass; dmarc=pass\n"
    "Delivered-To: bob@example.com\n"
    "Reply-To: friend@example.com\n"
)
LATE_NAMES = ("Authentication-Results", "Delivered-To", "Reply-To")


def test_inbox_sync_accepts_overlap_with_legacy_archive_copy(
    mail: AgentEmail, tmp_path: Path
) -> None:
    message_id = "<legacy@example.com>"
    filename = mail._format_filename(message_id)
    _sync_one(mail, tmp_path, "archive", message_id, LATE_HEADERS)
    legacy = _drop_headers(mail.email_dir / "archive" / filename, *LATE_NAMES)

    _sync_one(mail, tmp_path, "inbox", message_id, LATE_HEADERS)

    inbox = mail.email_dir / "inbox" / filename
    assert "dmarc=pass" in inbox.read_text()
    assert [e.message_id for e in mail.get_unreplied_emails()] == [message_id]
    assert (mail.email_dir / "archive" / filename).read_text() == legacy

    # Archiving keeps the richer copy instead of refusing as a conflict.
    richer = inbox.read_text()
    mail.archive(message_id)
    assert not inbox.exists()
    assert (mail.email_dir / "archive" / filename).read_text() == richer


def test_archive_drops_legacy_source_when_archive_copy_is_richer(
    mail: AgentEmail, tmp_path: Path
) -> None:
    message_id = "<legacy-inbox@example.com>"
    filename = mail._format_filename(message_id)
    inbox = mail.email_dir / "inbox" / filename
    _sync_one(mail, tmp_path, "inbox", message_id, LATE_HEADERS)
    _drop_headers(inbox, *LATE_NAMES)
    _sync_one(mail, tmp_path, "archive", message_id, LATE_HEADERS)
    archived = mail.email_dir / "archive" / filename
    richer = archived.read_text()

    mail.archive(message_id)

    assert not inbox.exists()
    assert archived.read_text() == richer


@pytest.mark.parametrize("original_folder", ["sent", "archive"])
def test_import_does_not_shadow_message_in_another_folder(
    mail: AgentEmail, tmp_path: Path, original_folder: str
) -> None:
    message_id = "<imported@example.com>"
    filename = mail._format_filename(message_id)
    original = mail.email_dir / original_folder / filename
    original.write_text(f"Message-ID: {message_id}\n\nORIGINAL\n")
    source = tmp_path / "import-maildir"
    (source / "new").mkdir(parents=True)
    raw = source / "new" / "m"
    _maildir_msg(raw, message_id=message_id, body="IMPOSTOR")

    assert mail.import_from_maildir(source, "inbox")["skipped"] == 1
    assert not (mail.email_dir / "inbox" / filename).exists()

    raw.write_text(original.read_text())
    assert mail.import_from_maildir(source, "inbox")["success"] == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("dmarc=pass", "dmarc=fail"),  # a late header both copies have must match
        ("Subject: Hello genuine", "Subject: Changed"),
        ("\n\ngenuine", "\n\nIMPOSTOR"),
    ],
)
def test_legacy_tolerance_still_rejects_conflicts(
    mail: AgentEmail, tmp_path: Path, old: str, new: str
) -> None:
    message_id = "<conflict@example.com>"
    filename = mail._format_filename(message_id)
    _sync_one(mail, tmp_path, "archive", message_id, LATE_HEADERS)
    archived = mail.email_dir / "archive" / filename
    content = archived.read_text()
    assert old in content
    archived.write_text(content.replace(old, new))

    _sync_one(mail, tmp_path, "inbox", message_id, LATE_HEADERS)

    assert not (mail.email_dir / "inbox" / filename).exists()


def test_header_missing_from_incoming_copy_is_still_a_conflict(
    mail: AgentEmail, tmp_path: Path
) -> None:
    message_id = "<stripped@example.com>"
    _sync_one(mail, tmp_path, "archive", message_id, LATE_HEADERS)
    _sync_one(mail, tmp_path, "inbox", message_id, "")
    assert not (mail.email_dir / "inbox" / mail._format_filename(message_id)).exists()


def test_legacy_tolerance_never_drops_lines_without_a_header_separator(
    mail: AgentEmail,
) -> None:
    message_id = "<crlf@example.com>"
    filename = mail._format_filename(message_id)
    archived = mail.email_dir / "archive" / filename
    original = f"Message-ID: {message_id}\r\n\r\nbody\r\n"
    archived.write_bytes(original.encode())
    (mail.email_dir / "inbox" / filename).write_bytes((original + "\nCc: injected").encode())

    with pytest.raises(ValueError, match="refusing to overwrite"):
        mail.archive(message_id)
    assert archived.read_bytes() == original.encode()


# Several Authentication-Results instances from the receiving MTA.


@pytest.mark.parametrize(
    ("values", "failed"),
    [
        (("mx.example; spf=pass", "mx.example; dmarc=fail"), True),
        (("mx.example 1; spf=pass", "MX.Example; dmarc=fail"), True),
        (("mx.example; dkim=pass", "mx.example; spf=softfail"), True),
        (("mx.example; dmarc=pass", "mx.example; spf=fail"), False),
        # A lower instance may be sender-supplied: it cannot excuse a failure.
        (("mx.example; spf=fail", "mx.example; dmarc=pass"), True),
        # Another authserv-id is not the receiving MTA's verdict.
        (("mx.example; spf=pass", "relay.other; dmarc=fail"), False),
        # Some receivers omit the authserv-id.
        (("spf=fail smtp.mailfrom=x; dkim=none",), True),
        (("spf=pass smtp.mailfrom=x", "dmarc=fail header.from=x"), True),
        ((), False),
    ],
)
def test_authentication_failed_across_instances(values: tuple[str, ...], failed: bool) -> None:
    assert authentication_failed(*values) is failed


@pytest.mark.parametrize("ingest", ["sync", "receive", "import"])
def test_later_receiver_dmarc_failure_is_not_lost(
    mail: AgentEmail, tmp_path: Path, ingest: str
) -> None:
    source = tmp_path / "multi-maildir"
    (source / "new").mkdir(parents=True)
    raw = source / "new" / "forged"
    _maildir_msg(
        raw,
        message_id="<multi@example.com>",
        body="forged",
        extra=(
            "Authentication-Results: mx.example; spf=pass\n"
            "Authentication-Results: relay.other; dmarc=pass\n"
            "Authentication-Results: mx.example; dmarc=fail header.from=example.com\n"
        ),
    )
    if ingest == "sync":
        mail.external_maildir_folders["inbox"] = source
        mail.sync_from_maildir("inbox")
        stored = (
            mail.email_dir / "inbox" / mail._format_filename("<multi@example.com>")
        ).read_text()
        assert stored.count("Authentication-Results:") == 2
        assert "relay.other" not in stored
    elif ingest == "receive":
        mail.receive(raw.read_text())
    else:
        assert mail.import_from_maildir(source, "inbox")["success"] == 1
    assert len(list((mail.email_dir / "inbox").glob("*.md"))) == 1
    assert mail.get_unreplied_emails() == []


# Message-ID check and publication are one step.


def test_concurrent_receive_cannot_substitute_content(
    mail: AgentEmail, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_write(path: Path, text: str) -> None:
        time.sleep(0.3)  # widen the window between the check and publication
        write_private_text(path, text)

    monkeypatch.setattr("gptmail.lib.write_private_text", slow_write)
    barrier = threading.Barrier(2)
    accepted: list[str] = []
    refused: list[str] = []

    def deliver(body: str) -> None:
        barrier.wait()
        try:
            mail.receive(f"Message-ID: <race@example.com>\nSubject: x\n\n{body}\n")
            accepted.append(body)
        except ValueError:
            refused.append(body)

    threads = [threading.Thread(target=deliver, args=(body,)) for body in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(accepted) == 1 and len(refused) == 1
    stored = mail.email_dir / "inbox" / mail._format_filename("<race@example.com>")
    assert stored.read_text().endswith(f"\n\n{accepted[0]}\n")


@pytest.mark.parametrize("delivered_to", ["inbox", "archive"])
def test_sync_rechecks_store_before_publishing(
    mail: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivered_to: str
) -> None:
    message_id = "<midsync@example.com>"
    filename = mail._format_filename(message_id)
    first = f"Message-ID: {message_id}\nSubject: first\n\nFIRST\n"

    def deliver_mid_sync(*args: object) -> bool:
        # Runs after the sync preload and before publication.
        mail.receive(first)
        if delivered_to != "inbox":
            mail.archive(message_id)
        return False

    monkeypatch.setattr(mail, "_is_duplicate_message", deliver_mid_sync)
    _sync_one(mail, tmp_path, "inbox", message_id, "")

    assert (mail.email_dir / delivered_to / filename).read_text() == first
    if delivered_to != "inbox":
        assert not (mail.email_dir / "inbox" / filename).exists()


def test_interrupted_private_write_leaves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "message.md"
    write_private_text(target, "ORIGINAL")

    def interrupted(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_private_text(target, "PARTIAL")
    assert target.read_text() == "ORIGINAL"
    assert list(tmp_path.iterdir()) == [target]
