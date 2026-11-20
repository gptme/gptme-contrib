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


# --- classify_stream_json: credential death vs edge block ---
#
# Synthetic `claude -p --output-format stream-json` events. The CLI labels an
# edge interstitial (a "403 Attention Required!" page) `authentication_failed`,
# exactly like an expired OAuth token, so the verdict has to come from the
# session's shape: a credential death fails on the first API call.

import json  # noqa: E402

INIT = {"type": "system", "subtype": "init", "session_id": "x"}
OK_TURN = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
}
TOOL_RESULT = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
}
EDGE_ERR = {
    "type": "assistant",
    "error": "authentication_failed",
    "api_error_status": 403,
    "message": {
        "content": [
            {
                "type": "text",
                "text": "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare",
            }
        ]
    },
}
EDGE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "terminal_reason": "api_error",
    "api_error_status": 403,
}
OAUTH_ERR = {
    "type": "assistant",
    "error": "authentication_failed",
    "api_error_status": 401,
    "message": {
        "content": [
            {
                "type": "text",
                "text": "Failed to authenticate. API Error: 401 OAuth token has expired",
            }
        ]
    },
}
OAUTH_RESULT = {
    "type": "result",
    "terminal_reason": "api_error",
    "result": "Please run /login. OAuth token expired",
}
CLEAN_RESULT = {"type": "result", "subtype": "success", "result": "done"}


def _lines(*events):
    return [json.dumps(e) + "\n" for e in events]


def test_stream_json_auth_error_on_first_call_is_oauth_dead():
    assert (
        mod.classify_stream_json(_lines(INIT, OAUTH_ERR, OAUTH_RESULT)) == "oauth_dead"
    )


def test_stream_json_result_only_oauth_signal_is_oauth_dead():
    assert mod.classify_stream_json(_lines(INIT, OAUTH_RESULT)) == "oauth_dead"


def test_stream_json_is_error_result_with_auth_text_is_oauth_dead():
    ev = {
        "type": "result",
        "is_error": True,
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }
    assert mod.classify_stream_json(_lines(INIT, ev)) == "oauth_dead"


@pytest.mark.parametrize(
    "envelope", [{"terminal_reason": "api_error"}, {"is_error": True}]
)
@pytest.mark.parametrize(
    "text",
    [
        "API Error: 401 Unauthorized",
        "Invalid bearer token",
        "authentication_error: token expired",
        "Error: Invalid API key",
        "Please run /login",
    ],
)
def test_stream_json_result_only_auth_signatures(envelope, text):
    ev = {"type": "result", "result": text, **envelope}
    assert mod.classify_stream_json(_lines(INIT, ev)) == "oauth_dead"
    assert mod.classify_stream_json(_lines(INIT, OK_TURN, ev)) == "edge_403"


@pytest.mark.parametrize(
    "envelope", [{"terminal_reason": "api_error"}, {"is_error": True}]
)
@pytest.mark.parametrize(
    "text", ["API Error: 403 Forbidden", "rate limit exceeded (429)", "read timed out"]
)
def test_stream_json_result_only_non_auth_errors_are_none(envelope, text):
    ev = {"type": "result", "result": text, **envelope}
    assert mod.classify_stream_json(_lines(INIT, ev)) == "none"


@pytest.mark.parametrize(
    "ev",
    [
        {"type": "result", "is_error": False, "result": "Fixed 401 Unauthorized"},
        {"type": "user", "is_error": True, "result": "401 Unauthorized"},
    ],
)
def test_stream_json_result_auth_matching_requires_error_envelope(ev):
    assert mod.classify_stream_json(_lines(INIT, ev)) == "none"


def test_stream_json_result_only_edge_auth_error_is_edge():
    ev = {
        "type": "result",
        "is_error": True,
        "result": "401 Unauthorized | Cloudflare Attention Required!",
    }
    assert mod.classify_stream_json(_lines(INIT, ev)) == "edge_403"


def test_stream_json_403_after_successful_turns_is_edge():
    events = (INIT, OK_TURN, TOOL_RESULT, OK_TURN, TOOL_RESULT, EDGE_ERR, EDGE_RESULT)
    assert mod.classify_stream_json(_lines(*events)) == "edge_403"


def test_stream_json_edge_page_on_first_call_is_edge():
    assert mod.classify_stream_json(_lines(INIT, EDGE_ERR, EDGE_RESULT)) == "edge_403"


def test_stream_json_html_page_on_first_call_is_edge():
    ev = dict(OAUTH_ERR)
    ev["message"] = {"content": "<!DOCTYPE html><html><body>blocked</body></html>"}
    assert mod.classify_stream_json(_lines(INIT, ev)) == "edge_403"


def test_stream_json_mixed_auth_error_after_success_is_edge():
    # Credentials worked earlier this session; a real expiry is caught by the
    # next session's first call, so this one must not page.
    events = (INIT, OK_TURN, TOOL_RESULT, OAUTH_ERR, OAUTH_RESULT)
    assert mod.classify_stream_json(_lines(*events)) == "edge_403"


def test_stream_json_errored_assistant_turn_is_not_success():
    # An assistant event that itself carries an error proves nothing about the
    # credential, so it must not flip a first-call death to edge.
    errored = {"type": "assistant", "error": "overloaded", "message": {"content": []}}
    assert mod.classify_stream_json(_lines(INIT, errored, OAUTH_ERR)) == "oauth_dead"


def test_stream_json_clean_session_is_none():
    events = (INIT, OK_TURN, TOOL_RESULT, CLEAN_RESULT)
    assert mod.classify_stream_json(_lines(*events)) == "none"


def test_stream_json_prose_about_401_is_none():
    # A successful session whose tool output and final text discuss 401s.
    tool = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "content": "HTTP 401 authentication_failed"}
            ]
        },
    }
    final = {
        "type": "result",
        "subtype": "success",
        "result": "fixed the OAuth 401 retry",
    }
    assert mod.classify_stream_json(_lines(INIT, OK_TURN, tool, final)) == "none"


def test_stream_json_empty_log_is_none():
    assert mod.classify_stream_json([]) == "none"


def test_stream_json_non_json_lines_ignored():
    lines = ["not json\n", "\n", "[1,2]\n", '"a string"\n'] + _lines(INIT)
    assert mod.classify_stream_json(lines) == "none"


def test_stream_json_non_json_lines_do_not_hide_auth_death():
    lines = ["garbage\n"] + _lines(INIT) + ["{broken\n"] + _lines(OAUTH_ERR)
    assert mod.classify_stream_json(lines) == "oauth_dead"


def test_stream_json_truncated_log_classified_on_what_was_written():
    # Hard kill mid-line: the last line is a partial JSON object.
    full = _lines(INIT, OAUTH_ERR)
    truncated = full + [json.dumps(OAUTH_RESULT)[:20]]
    assert mod.classify_stream_json(truncated) == "oauth_dead"
    assert mod.classify_stream_json(_lines(INIT, OK_TURN) + ['{"type": "res']) == "none"


def test_classify_stream_json_log_reads_file(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text("".join(_lines(INIT, OK_TURN, EDGE_ERR)))
    assert mod.classify_stream_json_log(log) == "edge_403"
    assert mod.classify_stream_json_log(str(log)) == "edge_403"


def test_classify_stream_json_log_missing_file_is_none(tmp_path):
    assert mod.classify_stream_json_log(tmp_path / "nope.jsonl") == "none"


@pytest.mark.parametrize(
    "events,verdict,rc",
    [
        ((INIT, OK_TURN, EDGE_ERR), "edge_403", 2),
        ((INIT, OAUTH_ERR), "oauth_dead", 0),
        (
            (INIT, {"type": "result", "is_error": True, "result": "401 Unauthorized"}),
            "oauth_dead",
            0,
        ),
        ((INIT, OK_TURN), "none", 1),
    ],
)
def test_cli_classify_stream_json_exit_codes(tmp_path, events, verdict, rc):
    log = tmp_path / "session.jsonl"
    log.write_text("".join(_lines(*events)))
    r = subprocess.run(
        [sys.executable, str(LIB), "--classify-stream-json", str(log)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == rc
    assert r.stdout.strip() == verdict


def test_cli_classify_stream_json_missing_file_is_none(tmp_path):
    r = subprocess.run(
        [sys.executable, str(LIB), "--classify-stream-json", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert r.stdout.strip() == "none"


# Same shape as the organization-disabled fixture in
# packages/gptme-sessions/tests/test_failure_capture.py.
ORG_REFUSED = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "model": "<synthetic>",
        "content": [
            {
                "type": "text",
                "text": "Your organization has disabled Claude subscription access for Claude Code",
            }
        ],
    },
    "error": "oauth_org_not_allowed",
    "isApiErrorMessage": True,
    "apiErrorStatus": 403,
    "apiErrorCode": "oauth_not_allowed_for_organization",
}


def test_stream_json_org_refusal_on_first_call_is_oauth_dead():
    assert mod.classify_stream_json(_lines(INIT, ORG_REFUSED)) == "oauth_dead"


def test_stream_json_org_refusal_api_code_alone_is_oauth_dead():
    ev = {k: v for k, v in ORG_REFUSED.items() if k != "error"}
    assert mod.classify_stream_json(_lines(INIT, ev)) == "oauth_dead"


def test_stream_json_org_refusal_after_success_is_still_oauth_dead():
    # The structured code cannot come from an edge page; retrying will not help.
    events = (INIT, OK_TURN, TOOL_RESULT, ORG_REFUSED)
    assert mod.classify_stream_json(_lines(*events)) == "oauth_dead"


def test_stream_json_bare_403_is_not_org_refusal():
    ev = {"type": "assistant", "apiErrorStatus": 403, "message": {"content": []}}
    assert mod.classify_stream_json(_lines(INIT, ev)) == "none"


def test_stream_json_success_in_an_earlier_session_does_not_count():
    # Two sessions appended to one log: the second dies on its first call.
    events = (INIT, OK_TURN, TOOL_RESULT, CLEAN_RESULT, INIT, OAUTH_ERR, OAUTH_RESULT)
    assert mod.classify_stream_json(_lines(*events)) == "oauth_dead"


def test_stream_json_only_the_last_session_is_classified():
    events = (INIT, OAUTH_ERR, OAUTH_RESULT, INIT, OK_TURN, CLEAN_RESULT)
    assert mod.classify_stream_json(_lines(*events)) == "none"
