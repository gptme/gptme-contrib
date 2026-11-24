"""Regressions from the 2026-11-24 verified bug hunt (synthetic fixtures only)."""

from __future__ import annotations

import io

from redact import redact
from redact.cli import main

GH = "ghp_" + "a" * 36
PW = "Zq9" + "x" * 13


def _contained(text: str, secret: str) -> None:
    r = redact(text, literals=[])
    assert r.blocked, [f.category for f in r.findings]
    assert secret not in r.redacted


def test_telegram_token_in_bot_api_url_is_redacted() -> None:
    token = "123456789:AA" + "b" * 33
    _contained(f"curl https://api.telegram.org/bot{token}/sendMessage", token)


def test_postgres_sqlalchemy_driver_url_password_is_redacted() -> None:
    _contained(
        f"create_engine('postgresql+psycopg2://app:{PW}@db.internal:5432/prod')", PW
    )


def test_truncated_private_key_body_is_redacted() -> None:
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + (body + "\n") * 3
        + "... [truncated]\n"
    )
    _contained(text, body)


def test_complete_private_key_still_stops_at_end_line() -> None:
    text = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\n"
        + "A" * 64
        + "\n-----END RSA PRIVATE KEY-----\nafter\n"
    )
    r = redact(text, literals=[])
    assert r.blocked
    assert r.redacted.endswith("after\n")


def test_cli_refuses_bomless_utf16_input(tmp_path, capsys) -> None:
    # BOM-less UTF-16LE of ASCII is valid UTF-8 with interleaved NULs, which
    # split every token past the patterns.
    f = tmp_path / "leak.txt"
    f.write_bytes(f"token {GH}\n".encode("utf-16-le"))
    out = tmp_path / "out.txt"
    assert main(["--no-env-secrets", "--out", str(out), str(f)]) == 2
    assert not out.exists()
    assert "NUL" in capsys.readouterr().err


def test_cli_out_write_failure_keeps_existing_file(tmp_path, monkeypatch) -> None:
    out = tmp_path / "doc.md"
    out.write_text("original precious content\n", encoding="utf-8")
    # A lone surrogate (surrogateescape stdin) cannot be encoded as UTF-8.
    monkeypatch.setattr("sys.stdin", io.StringIO("plain text \udcff more\n"))
    try:
        main(["--no-env-secrets", "--out", str(out), "-"])
    except UnicodeEncodeError:
        pass
    assert out.read_text(encoding="utf-8") == "original precious content\n"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]


def test_cli_out_writes_redacted_file(tmp_path) -> None:
    src = tmp_path / "in.txt"
    src.write_text("plain text, nothing secret\n", encoding="utf-8")
    out = tmp_path / "out.txt"
    assert main(["--no-env-secrets", "--out", str(out), str(src)]) == 0
    assert out.read_text(encoding="utf-8") == "plain text, nothing secret\n"
