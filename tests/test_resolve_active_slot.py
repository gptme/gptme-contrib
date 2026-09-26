"""Contract tests for scripts/lib/resolve-active-slot.sh.

The slot resolver is the primitive the whole auth-resilience marker subsystem
rides on: a stale-marker write is scoped to the *active credential slot* so a
401 in one slot never blocks another, and it must resolve to the literal
``unknown`` during a ``/login`` credential swap (regular-file drift) so the
launcher does NOT write a fleet-global block marker at the exact moment a
freshly re-authed quota is coming online.

These tests pin:

1. **Symlink resolution** — ``.credentials.json -> ....{slot}`` yields ``slot``.
2. **Regular-file / missing drift → ``unknown``** — the /login-window guard.
3. **Single-slot mode** — ``AUTH_SINGLE_SLOT`` canonical + ``BOB_SINGLE_SLOT``
   legacy back-compat, including the differing no-name defaults
   (canonical → ``unknown``; legacy → ``bob``) and the bad-character guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "resolve-active-slot.sh"


def _resolve(home: Path, env: dict[str, str] | None = None) -> str:
    """Source the lib and call resolve_active_slot with an explicit home dir."""
    full_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; resolve_active_slot "$1"', "_", str(home)],
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_creds(home: Path, *, link_target: str | None, regular: bool = False) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    cred = claude / ".credentials.json"
    if regular:
        cred.write_text("{}")
    elif link_target is not None:
        (claude / link_target).write_text("{}")
        cred.symlink_to(link_target)


def test_symlink_yields_slot(tmp_path: Path) -> None:
    _make_creds(tmp_path, link_target=".credentials.json.myslot")
    assert _resolve(tmp_path) == "myslot"


def test_regular_file_drift_is_unknown(tmp_path: Path) -> None:
    # During /login the symlink is replaced by a regular file — slot unknowable.
    _make_creds(tmp_path, link_target=None, regular=True)
    assert _resolve(tmp_path) == "unknown"


def test_missing_credentials_is_unknown(tmp_path: Path) -> None:
    assert _resolve(tmp_path) == "unknown"


def test_target_without_dot_suffix_is_unknown(tmp_path: Path) -> None:
    _make_creds(tmp_path, link_target="credentials")  # no dot-slot suffix
    assert _resolve(tmp_path) == "unknown"


def test_single_slot_canonical_named(tmp_path: Path) -> None:
    # Single-slot mode ignores the on-disk credential shape entirely.
    _make_creds(tmp_path, link_target=None, regular=True)
    got = _resolve(tmp_path, {"AUTH_SINGLE_SLOT": "1", "AUTH_SLOT_NAME": "primary"})
    assert got == "primary"


def test_single_slot_canonical_no_name_is_unknown(tmp_path: Path) -> None:
    # Canonical single-slot with no name resolves to the safe no-marker state.
    got = _resolve(tmp_path, {"AUTH_SINGLE_SLOT": "1"})
    assert got == "unknown"


def test_single_slot_legacy_back_compat_default_bob(tmp_path: Path) -> None:
    # Legacy BOB_SINGLE_SLOT with no name preserves its historical "bob" default.
    got = _resolve(tmp_path, {"BOB_SINGLE_SLOT": "1"})
    assert got == "bob"


def test_single_slot_legacy_named(tmp_path: Path) -> None:
    got = _resolve(
        tmp_path, {"BOB_SINGLE_SLOT": "1", "BOB_SINGLE_SLOT_NAME": "worker2"}
    )
    assert got == "worker2"


def test_canonical_name_wins_over_legacy(tmp_path: Path) -> None:
    got = _resolve(
        tmp_path,
        {
            "AUTH_SINGLE_SLOT": "1",
            "AUTH_SLOT_NAME": "canon",
            "BOB_SINGLE_SLOT_NAME": "legacy",
        },
    )
    assert got == "canon"


def test_single_slot_bad_char_is_unknown(tmp_path: Path) -> None:
    got = _resolve(tmp_path, {"AUTH_SINGLE_SLOT": "1", "AUTH_SLOT_NAME": "bad/slot"})
    assert got == "unknown"
