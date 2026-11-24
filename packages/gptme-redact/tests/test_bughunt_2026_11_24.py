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


def test_long_at_less_run_is_linear_time() -> None:
    # The email pattern used to rescan an @-less run from every offset:
    # 120k hex chars took ~7s, 240k ~30s (quadratic). Now ~0.02s.
    import time

    blob = "deadbeef" * 15000
    start = time.perf_counter()
    redact(blob, literals=[])
    assert time.perf_counter() - start < 2.0


def test_email_still_matched_after_long_local_part_run() -> None:
    r = redact("contact " + "a" * 500 + "@example.org now", literals=[])
    assert any(f.category == "email" for f in r.findings)
    assert "a" * 500 not in r.redacted


SECRET = "Zq9xVb7mN3kL2pR8"


def test_php_ruby_hash_rocket_password_is_redacted() -> None:
    _contained(f"'password' => '{SECRET}',", SECRET)


def test_go_short_var_decl_token_is_redacted() -> None:
    _contained(f'token := "{SECRET}"', SECRET)


def test_http_basic_authorization_header_is_redacted() -> None:
    b64 = "dXNlcjpaczl4VmI3bU4za0wycFI4"  # base64("user:" + synthetic pw)
    _contained(f"Authorization: Basic {b64}", b64)


def test_long_sk_key_leaves_no_tail() -> None:
    tail = "Zz" * 30
    key = "sk-ant-sid01-" + "Ab_-" * 16 + tail
    r = redact(key, literals=[])
    assert r.blocked
    assert tail not in r.redacted
