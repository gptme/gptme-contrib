"""Tests for the canonical transient-401 / auth-death classifier.

Covers the shared `scripts/auth_401.py` module that all 401-reactive surfaces
(operator loops, workers, subagents, autonomous runners) classify against.
Ported from Bob's suite (ErikBjare/bob#968) plus coverage for the configurable
403 policy that lets one classifier serve both Bob (403=quota) and Alice
(403=org entitlement death).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "auth_401.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("auth_401", LIB)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


# --- is_transient_401 (broad text classifier, no size gate) ---


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 401 Unauthorized",
        "Invalid bearer token",
        "authentication_error: token expired",
        'trajectory line: "error":"authentication_failed"',  # the gap the worker classifier had
        'trajectory line: "error_status":401',  # autonomous-run.sh marker
        "OAuth token has expired, please run /login",
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "Error: Invalid API key",
        "403 Forbidden",
        "Your credit balance is too low to access the API.",
    ],
)
def test_is_transient_401_matches_auth_signatures(text):
    assert mod.is_transient_401(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Done. No changes needed.",
        "ran fine, committed 3 files",
        "ConnectionError: read timed out",
        "rate limit exceeded (429)",  # capacity, not auth — must NOT match
    ],
)
def test_is_transient_401_ignores_non_auth(text):
    assert mod.is_transient_401(text) is False


def test_authentication_failed_now_covered():
    # Regression guard for the unification gap: the worker classifier previously
    # only had `authentication_error` and would miss the trajectory marker.
    assert mod.is_transient_401('"error":"authentication_failed"') is True


# --- is_auth_death (size-gated worker-output case) ---


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "worker.output"
    p.write_text(content, encoding="utf-8")
    return p


def test_tiny_output_with_signature_is_auth_death(tmp_path):
    assert mod.is_auth_death(_write(tmp_path, "401 Unauthorized\n")) is True


def test_large_output_mentioning_401_is_not_auth_death(tmp_path):
    big = "fixed the 401 handling in auth middleware\n" + ("x" * 5000)
    assert mod.is_auth_death(_write(tmp_path, big)) is False


def test_missing_file_is_not_auth_death(tmp_path):
    assert mod.is_auth_death(tmp_path / "nope.output") is False


def test_size_floor_boundary(tmp_path):
    sig = "401 unauthorized\n"
    pad = "z" * (mod.DEFAULT_MAX_BYTES - len(sig.encode()))
    at_floor = _write(tmp_path, sig + pad)
    assert len((sig + pad).encode()) == mod.DEFAULT_MAX_BYTES
    assert mod.is_auth_death(at_floor) is True
    assert mod.is_auth_death(_write(tmp_path, sig + pad + "z")) is False


# --- is_trajectory_auth_death (prose-safe, JSON-envelope-keyed) ---


@pytest.mark.parametrize(
    "text",
    [
        'trajectory: {"error_status":401,"msg":"..."}',  # current marker preserved
        'line: "error":"authentication_failed"',  # current marker preserved
        'raw API object: {"error":{"type":"authentication_error"}}',  # gap the inline grep missed
        'normalized: "error":"authentication_error"',
        '"error_status" : 401',  # whitespace-tolerant
    ],
)
def test_trajectory_auth_death_positive(text):
    assert mod.is_trajectory_auth_death(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # A successful 401-HARDENING session's trajectory discusses 401s in prose
        # — must NOT be flagged into a fleet-wide stale-marker.
        "I hardened the 401 path; the worker no longer burns an attempt on a 401.",
        "Updated AUTH_DEATH_PATTERNS to cover 401, 403, unauthorized.",
        'comment: "# matches a bare 401 anywhere"',
        # 403 is Bob's QUOTA signal by default — stays out of the auth-stale lane
        # even as a structured field (unless include_403=True; see below).
        '{"error_status":403,"msg":"quota"}',
        '"error":"rate_limit_error"',
        "ran fine, committed 3 files",
    ],
)
def test_trajectory_auth_death_prose_safe(text):
    assert mod.is_trajectory_auth_death(text) is False


# --- the configurable 403 policy (the one real per-agent difference) ---


def test_403_envelope_excluded_by_default_bob_policy():
    # Bob: 403 is quota, not a stale credential.
    assert mod.is_trajectory_auth_death('{"error_status":403,"msg":"quota"}') is False
    assert (
        mod.is_trajectory_auth_death('{"error_status":403}', include_403=False) is False
    )


def test_403_envelope_included_when_opted_in_alice_policy():
    # Alice: a 403 can be org entitlement death (oauth_org_not_allowed).
    assert (
        mod.is_trajectory_auth_death(
            '{"error_status":403,"msg":"..."}', include_403=True
        )
        is True
    )
    assert (
        mod.is_trajectory_auth_death(
            '{"error":"oauth_org_not_allowed"}', include_403=True
        )
        is True
    )


def test_401_envelope_still_matches_regardless_of_403_flag():
    # The 401-scoped patterns are always active; include_403 only ADDS 403.
    assert (
        mod.is_trajectory_auth_death('{"error_status":401}', include_403=True) is True
    )
    assert (
        mod.is_trajectory_auth_death('{"error_status":401}', include_403=False) is True
    )


def test_prose_403_never_matches_even_when_opted_in():
    # include_403 keys on the structured field, not a bare prose "403".
    assert (
        mod.is_trajectory_auth_death("discussed a 403 in the diff", include_403=True)
        is False
    )


# --- CLI (bash-surface interface) ---


def test_cli_classify_stdin_exit_codes():
    auth = subprocess.run(
        [sys.executable, str(LIB), "--classify-stdin"],
        input="API Error: 401 Unauthorized\n",
        text=True,
        capture_output=True,
    )
    assert auth.returncode == 0

    clean = subprocess.run(
        [sys.executable, str(LIB), "--classify-stdin"],
        input="ran fine, committed 3 files\n",
        text=True,
        capture_output=True,
    )
    assert clean.returncode == 1


def test_cli_classify_file_exit_codes(tmp_path):
    auth = _write(tmp_path, "401 Unauthorized\n")
    r = subprocess.run(
        [sys.executable, str(LIB), "--classify-file", str(auth)],
        capture_output=True,
    )
    assert r.returncode == 0

    legit = tmp_path / "legit.output"
    legit.write_text("ran fine\n" + ("y" * 5000), encoding="utf-8")
    r2 = subprocess.run(
        [sys.executable, str(LIB), "--classify-file", str(legit)],
        capture_output=True,
    )
    assert r2.returncode == 1


def test_cli_requires_a_mode():
    r = subprocess.run([sys.executable, str(LIB)], capture_output=True)
    assert r.returncode != 0  # argparse mutually-exclusive group is required


def test_cli_classify_trajectory_exit_codes(tmp_path):
    # Real auth-death envelope → exit 0, even in a large trajectory.
    death = tmp_path / "death.jsonl"
    death.write_text(
        ("x" * 50000) + '\n{"error":{"type":"authentication_error"}}\n',
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(LIB), "--classify-trajectory", str(death)],
        capture_output=True,
    )
    assert r.returncode == 0

    # Large successful auth-hardening trajectory mentioning 401 in prose → exit 1.
    legit = tmp_path / "legit.jsonl"
    legit.write_text(
        "hardened the 401 path; quota-403 routed separately\n" + ("y" * 50000),
        encoding="utf-8",
    )
    r2 = subprocess.run(
        [sys.executable, str(LIB), "--classify-trajectory", str(legit)],
        capture_output=True,
    )
    assert r2.returncode == 1

    # Unreadable path → exit 1 (no false stale-marker on a missing trajectory).
    r3 = subprocess.run(
        [sys.executable, str(LIB), "--classify-trajectory", str(tmp_path / "nope")],
        capture_output=True,
    )
    assert r3.returncode == 1


def test_cli_include_403_flag(tmp_path):
    # A 403 envelope → exit 1 by default, exit 0 with --include-403.
    traj = tmp_path / "403.jsonl"
    traj.write_text('{"error_status":403,"msg":"org disabled"}\n', encoding="utf-8")

    default = subprocess.run(
        [sys.executable, str(LIB), "--classify-trajectory", str(traj)],
        capture_output=True,
    )
    assert default.returncode == 1

    opted = subprocess.run(
        [sys.executable, str(LIB), "--classify-trajectory", str(traj), "--include-403"],
        capture_output=True,
    )
    assert opted.returncode == 0
