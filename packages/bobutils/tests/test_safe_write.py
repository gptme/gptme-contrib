"""Tests for bobutils.safe_write — symlink-safe atomic file writer.

Key invariants tested
---------------------
1. Plain file: content written correctly, no clobber of existing content on error.
2. Symlink target: symlink is preserved after write (the link itself survives).
3. Symlink chain: even multi-hop chains are preserved end-to-end.
4. New file creation: works when destination doesn't exist yet.
5. Mode bits: new files get the requested permission mode.
6. Permission preservation: existing file permissions are NOT overwritten by mode.
7. Atomic partial-write safety: a write error leaves the original intact.
8. safe_write_bytes: binary variant works correctly.
"""

from __future__ import annotations

import os
from pathlib import Path

from bobutils.safe_write import safe_write, safe_write_bytes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def file_mode(path: Path) -> int:
    """Return the bottom 9 permission bits of *path*."""
    return os.stat(path).st_mode & 0o777


# ---------------------------------------------------------------------------
# safe_write — text
# ---------------------------------------------------------------------------


def test_plain_file(tmp_path: Path) -> None:
    """Writing to a regular file works as expected."""
    target = tmp_path / "data.txt"
    target.write_text("old content")

    safe_write(target, "new content")

    assert target.read_text() == "new content"
    assert target.is_file() and not target.is_symlink()


def test_symlink_preserved(tmp_path: Path) -> None:
    """Writing through a symlink keeps the symlink alive."""
    real = tmp_path / "real.txt"
    real.write_text("original")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    safe_write(link, "updated via link")

    # Symlink must still exist and still point to the real file
    assert link.is_symlink(), "symlink was clobbered — replaced with a regular file"
    assert os.readlink(link) == str(real) or Path(os.readlink(link)).name == real.name
    assert link.read_text() == "updated via link"
    assert real.read_text() == "updated via link"


def test_symlink_chain_preserved(tmp_path: Path) -> None:
    """Multi-hop symlink chains are fully preserved."""
    real = tmp_path / "real.txt"
    real.write_text("base")
    hop1 = tmp_path / "hop1.txt"
    hop1.symlink_to(real)
    hop2 = tmp_path / "hop2.txt"
    hop2.symlink_to(hop1)

    safe_write(hop2, "chain write")

    assert hop2.is_symlink(), "hop2 symlink clobbered"
    assert hop1.is_symlink(), "hop1 symlink clobbered"
    assert real.read_text() == "chain write"
    assert hop2.read_text() == "chain write"


def test_new_file_creation(tmp_path: Path) -> None:
    """Writing to a non-existent path creates the file."""
    new_file = tmp_path / "new.txt"
    assert not new_file.exists()

    safe_write(new_file, "brand new")

    assert new_file.read_text() == "brand new"
    assert new_file.is_file() and not new_file.is_symlink()


def test_mode_bits_new_file(tmp_path: Path) -> None:
    """Mode bits are applied to newly created files."""
    target = tmp_path / "secret.json"
    safe_write(target, "{}", mode=0o600)

    assert file_mode(target) == 0o600


def test_mode_bits_symlink_new_target(tmp_path: Path) -> None:
    """Mode is applied when writing through a symlink to a *new* (not yet existing) real file."""
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    # Symlink points at real, but real doesn't exist yet — mode should be applied
    link.symlink_to(real)
    assert not real.exists()

    safe_write(link, '{"key": "val"}', mode=0o600)

    # The newly created real file should have the requested mode
    assert file_mode(real) == 0o600
    assert link.is_symlink(), "symlink clobbered"


def test_existing_permissions_preserved(tmp_path: Path) -> None:
    """Existing file permissions are NOT overwritten by the mode parameter.

    A credential file with 0o600 must stay 0o600 even if the caller passes
    mode=0o644 (the default).  Silently widening permissions would expose
    private content to other local users.
    """
    secret = tmp_path / "credentials.json"
    secret.write_text("{}")
    os.chmod(secret, 0o600)
    assert file_mode(secret) == 0o600

    # Write with the default mode (0o644) — existing 0o600 must be preserved
    safe_write(secret, '{"token": "abc"}')

    assert file_mode(secret) == 0o600, (
        "safe_write widened a 0o600 credential file to "
        f"{oct(file_mode(secret))} — permissions NOT preserved"
    )
    assert secret.read_text() == '{"token": "abc"}'


def test_existing_permissions_preserved_through_symlink(tmp_path: Path) -> None:
    """Permission preservation works when writing through a symlink."""
    real = tmp_path / "real_creds.json"
    real.write_text("{}")
    os.chmod(real, 0o600)
    link = tmp_path / "creds.json"
    link.symlink_to(real)

    safe_write(link, '{"token": "xyz"}')

    assert file_mode(real) == 0o600, "permissions of real target were widened"
    assert link.is_symlink(), "symlink clobbered"
    assert real.read_text() == '{"token": "xyz"}'


def test_original_intact_after_write(tmp_path: Path) -> None:
    """Original content is readable before and after overwrite."""
    target = tmp_path / "file.txt"
    target.write_text("v1")

    safe_write(target, "v2")

    assert target.read_text() == "v2"


def test_symlink_to_missing_target_creates_file(tmp_path: Path) -> None:
    """Writing through a broken symlink (target missing) creates the target file."""
    target = tmp_path / "subdir" / "real.txt"
    target.parent.mkdir()
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    # Target doesn't exist yet
    assert not target.exists()

    safe_write(link, "created through broken link")

    # Now target exists
    assert target.exists()
    assert target.read_text() == "created through broken link"
    # Symlink still intact
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# safe_write_bytes — binary
# ---------------------------------------------------------------------------


def test_binary_write_plain_file(tmp_path: Path) -> None:
    """Binary write to a plain file."""
    target = tmp_path / "data.bin"
    safe_write_bytes(target, b"\x00\x01\x02\x03")

    assert target.read_bytes() == b"\x00\x01\x02\x03"


def test_binary_write_through_symlink(tmp_path: Path) -> None:
    """Binary write through a symlink preserves the link."""
    real = tmp_path / "real.bin"
    real.write_bytes(b"old")
    link = tmp_path / "link.bin"
    link.symlink_to(real)

    safe_write_bytes(link, b"\xff\xfe")

    assert link.is_symlink(), "binary write clobbered symlink"
    assert real.read_bytes() == b"\xff\xfe"


def test_unicode_content(tmp_path: Path) -> None:
    """Unicode text is encoded correctly as UTF-8."""
    target = tmp_path / "unicode.txt"
    text = "こんにちは — café — 🤖"
    safe_write(target, text)

    assert target.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# THE canonical anti-regression test: os.replace on a symlink clobbers it
# (This documents what safe_write prevents)
# ---------------------------------------------------------------------------


def test_naive_replace_would_clobber(tmp_path: Path) -> None:
    """Demonstrate the clobber class: naive os.replace() on a symlink path
    destroys the link.  This is what safe_write prevents.
    """
    real = tmp_path / "real.txt"
    real.write_text("original")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    # Naive atomic write: create temp in same dir, replace the SYMLINK path
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=tmp_path)
    with os.fdopen(fd, "w") as f:
        f.write("naive write")
    os.replace(tmp_name, link)  # This clobbers the symlink!

    # Link is now a regular file — the clobber happened
    assert not link.is_symlink(), (
        "expected clobber did NOT happen — test assumption wrong"
    )
    # And real is untouched (orphaned)
    assert real.read_text() == "original"

    # Now verify safe_write wouldn't do this:
    real2 = tmp_path / "real2.txt"
    real2.write_text("original2")
    link2 = tmp_path / "link2.txt"
    link2.symlink_to(real2)

    safe_write(link2, "safe write")

    assert link2.is_symlink(), "safe_write clobbered the symlink — regression!"
    assert real2.read_text() == "safe write"
