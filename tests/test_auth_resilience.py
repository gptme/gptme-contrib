"""Contract tests for scripts/lib/auth_resilience.sh.

``auth_resilience.sh`` is the reusable preflight → run → classify-401 →
write-marker → backoff → retry-once wrapper that five reactive launchers would
otherwise each reimplement. These tests pin the seams a consumer depends on:

- the three classifier wrappers delegate correctly to the shared ``auth_401.py``;
- ``with_auth_resilience`` returns 0 on success and honours preflight blocks (75);
- a classified 401 writes a slot-scoped stale marker and retries exactly once,
  while a non-401 failure does neither.

Sibling scripts resolve relative to the library's own directory, so these tests
also implicitly verify the contrib-layout path resolution (auth_401.py lives at
``scripts/auth_401.py``, not ``scripts/lib/``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "auth_resilience.sh"


def _bash(
    script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    if env:
        base.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env=base,
    )


# --- classifier wrappers delegate to auth_401.py ---


def test_classify_file_detects_401(tmp_path: Path) -> None:
    f = tmp_path / "out.txt"
    f.write_text("API Error: 401 Unauthorized\n")
    assert _bash(f'auth_resilience_classify_file "{f}"').returncode == 0


def test_classify_file_ignores_non_auth(tmp_path: Path) -> None:
    f = tmp_path / "out.txt"
    f.write_text("everything is fine\n")
    assert _bash(f'auth_resilience_classify_file "{f}"').returncode != 0


def test_classify_stdin_detects_401() -> None:
    res = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; auth_resilience_classify_stdin'],
        input="Invalid bearer token\n",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert res.returncode == 0


# --- with_auth_resilience orchestration ---


def test_happy_path_returns_zero() -> None:
    res = _bash("with_auth_resilience --skip-preflight -- true")
    assert res.returncode == 0, res.stderr


def test_preflight_block_returns_75(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    # A fresh scoped marker makes the preflight exit 75 before running anything.
    (stale / "claude-code-test-auth-stale.txt").write_text("blocked\n")
    marker_sentinel = tmp_path / "ran"
    res = _bash(
        f'with_auth_resilience -- bash -c "touch {marker_sentinel}"',
        env={
            "STALE_DIR": str(stale),
            "AUTH_SINGLE_SLOT": "1",
            "AUTH_SLOT_NAME": "test",
        },
    )
    assert res.returncode == 75
    assert not marker_sentinel.exists()  # command never ran


def test_classified_401_writes_marker_and_retries_once(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    classify = tmp_path / "child.log"
    classify.write_text("API Error: 401 Unauthorized\n")
    counter = tmp_path / "count"
    counter.write_text("")
    cmd = f'bash -c "echo x >> {counter}; exit 3"'
    res = _bash(
        f"with_auth_resilience --skip-preflight --backoff 0 "
        f'--classify-file "{classify}" -- {cmd}',
        env={
            "STALE_DIR": str(stale),
            "AUTH_SINGLE_SLOT": "1",
            "AUTH_SLOT_NAME": "test",
        },
    )
    assert res.returncode == 3  # original command exit propagated after retry
    # Ran twice: initial attempt + one retry.
    assert counter.read_text().count("x") == 2
    # A slot-scoped marker was written.
    assert (stale / "claude-code-test-auth-stale.txt").exists()


def test_classified_401_retry_recovers(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    classify = tmp_path / "child.log"
    classify.write_text("API Error: 401 Unauthorized\n")
    counter = tmp_path / "count"
    counter.write_text("")
    flag = tmp_path / "failed_once"
    # Fail (401) on the first attempt, succeed on the retry.
    cmd = (
        f'bash -c "echo x >> {counter}; '
        f'if [ -f {flag} ]; then exit 0; fi; touch {flag}; exit 3"'
    )
    res = _bash(
        f"with_auth_resilience --skip-preflight --backoff 0 "
        f'--classify-file "{classify}" -- {cmd}',
        env={
            "STALE_DIR": str(stale),
            "AUTH_SINGLE_SLOT": "1",
            "AUTH_SLOT_NAME": "test",
        },
    )
    assert res.returncode == 0, res.stderr  # retry recovered
    assert counter.read_text().count("x") == 2  # initial attempt + one retry
    # The first-attempt 401 still wrote a slot-scoped marker before recovery.
    assert (stale / "claude-code-test-auth-stale.txt").exists()


# --- write_marker skips the /login window ---


def test_write_marker_skips_when_slot_unknown(tmp_path: Path) -> None:
    # AUTH_SINGLE_SLOT with no AUTH_SLOT_NAME resolves the slot to "unknown"
    # (the /login credential-swap window). A marker written then would block
    # every slot fleet-wide, so write_marker must return 2 and write nothing.
    stale = tmp_path / "state"
    stale.mkdir()
    res = _bash(
        f'auth_resilience_write_marker --source test --dir "{stale}"',
        env={"AUTH_SINGLE_SLOT": "1"},
    )
    assert res.returncode == 2
    assert not list(stale.glob("*auth-stale*"))  # nothing written


def test_non_401_failure_does_not_retry_or_mark(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    classify = tmp_path / "child.log"
    classify.write_text("plain old failure, nothing to see\n")
    counter = tmp_path / "count"
    counter.write_text("")
    cmd = f'bash -c "echo x >> {counter}; exit 4"'
    res = _bash(
        f"with_auth_resilience --skip-preflight --backoff 0 "
        f'--classify-file "{classify}" -- {cmd}',
        env={
            "STALE_DIR": str(stale),
            "AUTH_SINGLE_SLOT": "1",
            "AUTH_SLOT_NAME": "test",
        },
    )
    assert res.returncode == 4
    assert counter.read_text().count("x") == 1  # ran once, no retry
    assert not (stale / "claude-code-test-auth-stale.txt").exists()  # no marker
