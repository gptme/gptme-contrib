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


def test_cli_refuses_utf16_input_with_bom(tmp_path, capsys) -> None:
    src = tmp_path / "utf16.txt"
    src.write_bytes(f"token {GH}\n".encode("utf-16"))  # starts with a BOM
    assert main(["--no-env-secrets", str(src)]) == 2
    assert "not valid UTF-8" in capsys.readouterr().err


def test_cli_out_writes_through_a_symlink_and_keeps_the_mode(tmp_path) -> None:
    src = tmp_path / "in.txt"
    src.write_text("plain text\n", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert main(["--no-env-secrets", "--out", str(link), str(src)]) == 0
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "plain text\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_cli_out_write_failure_keeps_existing_file(tmp_path) -> None:
    """Encode happens before any truncate, so a surrogate cannot destroy --out.

    The CLI refuses invalid UTF-8 at read time, so this pins `_write_atomic`
    itself: `text.encode("utf-8")` must run before the target is opened.
    """
    from pathlib import Path

    from redact.cli import _write_atomic

    out = tmp_path / "doc.md"
    out.write_text("original precious content\n", encoding="utf-8")
    try:
        _write_atomic(Path(out), "plain text \udcff more\n")
    except UnicodeEncodeError:
        pass
    else:
        raise AssertionError("expected UnicodeEncodeError from encode-before-open")
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
    # 120k hex chars took ~7s, 240k ~30s (quadratic). Compare growth rather than
    # wall-clock time: 4x the input costs ~4x when linear and ~16x when quadratic.
    import time

    def best_of_three(blob: str) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            redact(blob, literals=[])
            times.append(time.perf_counter() - start)
        return min(times)

    small = best_of_three("deadbeef" * 5000)
    large = best_of_three("deadbeef" * 20000)
    assert large / small < 8


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


def test_out_to_dev_null_writes_through_the_device(tmp_path) -> None:
    import os
    import stat

    src = tmp_path / "in.txt"
    src.write_text("plain text, nothing secret\n", encoding="utf-8")
    assert main(["--no-env-secrets", "--out", os.devnull, str(src)]) == 0
    assert stat.S_ISCHR(os.stat(os.devnull).st_mode)


def test_out_to_fifo_writes_through_the_fifo(tmp_path) -> None:
    import os
    import stat
    import threading

    src = tmp_path / "in.txt"
    src.write_text("plain text, nothing secret\n", encoding="utf-8")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    received: list[bytes] = []

    def reader() -> None:
        with open(fifo, "rb") as fh:
            received.append(fh.read())

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    assert main(["--no-env-secrets", "--out", str(fifo), str(src)]) == 0
    t.join(timeout=10)
    assert received == [b"plain text, nothing secret\n"]
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)


def test_short_http_basic_credential_is_redacted() -> None:
    _contained("Authorization: Basic dTpw", "dTpw")  # base64("u:p")


def test_quoted_http_basic_authorization_header_is_redacted() -> None:
    # The header name is quoted in source and logs, so the colon never follows
    # `Authorization` directly.
    b64 = "dXNlcjpwYXNzd29yZA=="  # base64("user:password")
    _contained(f'headers={{"Authorization": "Basic {b64}"}}', b64)


def test_out_to_dev_stdout_under_a_redirect_keeps_later_writes(tmp_path) -> None:
    import os
    from pathlib import Path

    from redact.cli import _write_atomic

    target = tmp_path / "report.txt"
    saved = os.dup(1)
    try:
        with open(target, "wb") as fh:
            os.dup2(fh.fileno(), 1)
            _write_atomic(Path("/dev/stdout"), "body\n")
            os.write(1, b"footer\n")
    finally:
        os.dup2(saved, 1)
        os.close(saved)
    # A rename would leave fd 1 on an unlinked inode and lose the footer.
    assert target.read_text(encoding="utf-8") == "body\nfooter\n"


def test_url_encoded_comma_joined_emails_are_both_redacted() -> None:
    r = redact("mailto:alice@x.com%2Cbob@y.org", literals=[])
    assert "alice" not in r.redacted
    assert "bob@y.org" not in r.redacted
    assert all("bob" not in f.preview for f in r.findings)


def test_dash_joined_emails_are_both_redacted() -> None:
    r = redact("a@x.com-bob@y.org", literals=[])
    assert "bob@y.org" not in r.redacted


def test_cli_refuses_invalid_utf8_from_stdin(capsys, monkeypatch) -> None:
    """The same bytes must not behave differently by how they were supplied.

    `sys.stdin.read()` uses surrogateescape, so `0xff` arrived as "\\udcff",
    matched no pattern and was reported as a clean artifact, while a file with
    that byte was refused as invalid UTF-8.
    """

    class _SurrogateStdin(io.TextIOBase):
        """What a POSIX stdin looks like: text read with surrogateescape."""

        buffer = io.BytesIO(b"plain \xff text\n")

        def read(self, *a):  # noqa: D102 - mirrors TextIOWrapper.read
            return self.buffer.getvalue().decode("utf-8", "surrogateescape")

    monkeypatch.setattr("sys.stdin", _SurrogateStdin())
    assert main(["--no-env-secrets", "-"]) == 2
    assert "not valid UTF-8" in capsys.readouterr().err


def test_out_falls_back_to_in_place_write_in_a_non_writable_dir(
    tmp_path, monkeypatch
) -> None:
    """mkstemp beside the target fails on a writable file in a read-only
    directory; the old in-place write worked, so it stays the fallback."""
    import tempfile
    from pathlib import Path

    from redact.cli import _write_atomic

    out = tmp_path / "redacted.txt"
    out.write_text("old\n", encoding="utf-8")

    def _deny(*a, **kw):
        raise PermissionError(13, "Permission denied", str(tmp_path))

    monkeypatch.setattr(tempfile, "mkstemp", _deny)
    _write_atomic(Path(out), "new\n")
    assert out.read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["redacted.txt"]


def test_out_falls_back_to_in_place_write_when_replace_is_denied(
    tmp_path, monkeypatch
) -> None:
    """Replacing another user's file in a sticky directory is EPERM; writing
    the file itself is allowed."""
    import os

    from redact.cli import _write_atomic

    out = tmp_path / "shared.txt"
    out.write_text("old\n", encoding="utf-8")

    def _deny(*a, **kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "replace", _deny)
    _write_atomic(out, "new\n")
    assert out.read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["shared.txt"]


def test_out_in_place_fallback_restores_original_on_write_failure(
    tmp_path, monkeypatch
) -> None:
    """A disk-full write after truncate must not leave an empty --out file."""
    import builtins
    import tempfile
    from pathlib import Path

    from redact.cli import _write_atomic

    out = tmp_path / "doc.md"
    original = "original precious content\n"
    out.write_text(original, encoding="utf-8")

    def _deny(*a, **kw):
        raise PermissionError(13, "Permission denied", str(tmp_path))

    monkeypatch.setattr(tempfile, "mkstemp", _deny)

    real_open = builtins.open
    writes = {"n": 0}

    def _open(file, mode="r", *a, **kw):
        path = str(file)
        if path == str(out) and "w" in str(mode):
            writes["n"] += 1
            if writes["n"] == 1:
                # Truncate as `"wb"` does, then fail the write.
                fh = real_open(file, mode, *a, **kw)
                fh.close()

                class _Boom:
                    def write(self, data):
                        raise OSError(28, "No space left on device")

                    def flush(self):
                        return None

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                return _Boom()
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", _open)
    try:
        _write_atomic(Path(out), "new content that must not land\n")
    except OSError:
        pass
    else:
        raise AssertionError("expected the injected write failure")
    assert out.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]
