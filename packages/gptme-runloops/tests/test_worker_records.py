"""Golden tests for the PM worker post-session bookkeeping port.

The fixtures under ``tests/goldens/worker_records/`` are the *captured
behavior of the actual heredoc blocks* in ErikBjare/bob
``scripts/github/project-monitoring-worker.sh`` @
98e7da0eb18543c53ca7f7ee207eb679010ccffb, run standalone (sed-extracted
verbatim, ``${var@Q}`` expansion performed by bash) against deterministic
stub packages standing in for ``gptme_sessions`` / ``metaproductivity`` /
``agent_events`` and a PATH-stubbed ``gh``. The stub callables defined in
this module mirror those stub packages exactly — the fixtures enforce the
mirroring: any drift between them and the generator's stubs fails these
tests. This is what lets the later brain-side shim PR prove "same records
out".

Fixture conventions:

- ``<WS>`` is the workspace-root placeholder; tests substitute their own
  ``tmp_path`` for it on both inputs and expected outputs.
- ``expected_* = null`` (where applicable) means the heredoc *died* on that
  input (killed by the bash ``|| true``) — the module function must raise.

Regeneration: run the generator against a brain checkout (it sed-extracts
the heredoc bodies by line number, so update the line ranges if worker.sh
moved). The generator script is kept with the brain-side migration notes;
its stub sources are the authoritative copy of the stubs below.

Where the locked-in behavior is a quirk rather than intent, the module
marks it ``# NOTE(parity):`` — these tests pin parity, they do not bless
the quirk.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from gptme_runloops.worker_records import (
    append_wait_merge_gate_log,
    append_worker_latency_records,
    apply_pr_state_diff,
    apply_worker_result_to_payload,
    augment_with_outcome_subtype,
    build_wait_merge_gate_entry,
    collect_commit_oids,
    compute_latency_outcome,
    dedupe_deliverables,
    detect_worker_outcome_subtype,
    extract_delivery_field,
    fallback_outcome,
    fetch_pr_snapshot,
    finalize_post_session_record,
    normalize_delivery_outcome,
    normalize_manifest_deliverables,
    normalize_pr_snapshot,
    parse_latency_inputs,
    parse_pr_snapshot,
    parse_rate_limit_rejection,
    read_record_pr_state_after,
    resolve_trajectory,
    split_item_types,
    update_record_pr_state,
    write_fallback_session_record,
    write_post_session_record,
    write_worker_result_manifest,
)

GOLDEN_DIR = Path(__file__).parent / "goldens" / "worker_records"

# Trajectory files the golden generator created under its workspace; tests
# recreate the same set so existence checks resolve identically.
TRAJ_FILES = ("cc-session.jsonl", "fallback-traj.jsonl", "wr-traj.jsonl")


def load_cases(name: str) -> list[dict[str, Any]]:
    payload = json.loads((GOLDEN_DIR / f"{name}.json").read_text())
    return payload["cases"]


def case_ids(name: str) -> list[str]:
    return [c["name"] for c in load_cases(name)]


def subst(obj: Any, ws: Path) -> Any:
    """Recursively substitute the <WS> placeholder with a real workspace root."""
    if isinstance(obj, str):
        return obj.replace("<WS>", str(ws))
    if isinstance(obj, list):
        return [subst(v, ws) for v in obj]
    if isinstance(obj, dict):
        return {k: subst(v, ws) for k, v in obj.items()}
    return obj


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    for sub in ("records", "traj", "logs"):
        (tmp_path / sub).mkdir()
    for name in TRAJ_FILES:
        (tmp_path / "traj" / name).write_text("x\n")
    return tmp_path


# --- Stub collaborators (mirror the golden generator's stub packages) ---


class StubStore:
    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir


class _StubRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class _StubResult:
    def __init__(self, record: _StubRecord, grade: str) -> None:
        self.record = record
        self.grade = grade


def stub_post_session(
    *,
    store: StubStore,
    harness: str,
    model: str | None,
    run_type: str,
    trigger: str,
    category: str,
    exit_code: int,
    duration_seconds: int,
    trajectory_path: Path | None,
    session_id: str,
) -> _StubResult:
    data = {
        "harness": harness,
        "model": model,
        "run_type": run_type,
        "trigger": trigger,
        "category": category,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "trajectory_path": str(trajectory_path) if trajectory_path else None,
        "session_id": session_id,
        "outcome": "stub-outcome",
        "sessions_dir": str(store.sessions_dir),
    }
    return _StubResult(_StubRecord(data), "stub-grade")


def stub_make_record(
    *,
    harness: str,
    model: str,
    run_type: str,
    category: str,
    outcome: str,
    duration_seconds: int,
) -> dict[str, Any]:
    return {
        "harness": harness,
        "model": model,
        "run_type": run_type,
        "category": category,
        "outcome": outcome,
        "duration_seconds": duration_seconds,
        "source": "stub-session-record",
    }


def stub_upgrade_outcome(payload: dict[str, Any]) -> None:
    payload["outcome_upgrade_seen"] = True
    if (
        payload.get("pr_state_after") == "MERGED"
        and payload.get("outcome") == "unknown"
    ):
        payload["outcome"] = "productive"


def stub_build_worker_result(**kwargs: Any) -> dict[str, Any]:
    exit_code = kwargs.get("exit_code")
    status: str | None = "completed" if exit_code == 0 else "failed"
    if exit_code == 99:
        status = None
    blocked_reason = "stub-blocked" if exit_code == 75 else None
    return {
        "schema_version": 3,
        "status": status,
        "blocked_reason": blocked_reason,
        "worker": {"kind": kwargs.get("worker_kind"), "id": kwargs.get("worker_id")},
        "work_item_id": kwargs.get("work_item_id"),
        "session_id": kwargs.get("session_id"),
        "model": kwargs.get("model"),
        "exit_code": exit_code,
        "duration_seconds": kwargs.get("duration_seconds"),
        "git_refs": {
            "start_commit": kwargs.get("start_commit"),
            "end_commit": kwargs.get("end_commit"),
            "commit_oids": [],
        },
        "task": {
            "intended_category": kwargs.get("intended_category"),
            "repo": kwargs.get("repo"),
            "issue_number": kwargs.get("issue_number"),
        },
        "artifact_paths": {
            "draft_path": str(kwargs.get("draft_path")),
            "trajectory_path": kwargs.get("trajectory_path"),
        },
        "deliverables": list(kwargs.get("deliverables") or []),
    }


def stub_write_worker_result(path: Path, manifest: Any) -> None:
    Path(path).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def stub_load_worker_result(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# --- Golden: CC rate-limit parser (worker.sh:110-127) ---


@pytest.mark.parametrize("case", load_cases("rate_limit"), ids=case_ids("rate_limit"))
def test_rate_limit_golden(case: dict[str, Any]) -> None:
    # Mirror the bash contract: heredoc stdout on success, "" when the
    # parser dies (`|| _cc_rl_info=""`).
    try:
        rejection = parse_rate_limit_rejection(case["log_lines"])
        info = rejection.render() if rejection else ""
    except Exception:
        info = ""
    assert info == case["expected_info"]


def test_rate_limit_accepts_file_style_lines() -> None:
    # The heredoc iterates open(path) — lines carry trailing newlines.
    lines = [
        '{"type":"rate_limit_event","rate_limit_info":'
        '{"status":"rejected","rateLimitType":"five_hour","resetsAt":9}}\n'
    ]
    rejection = parse_rate_limit_rejection(lines)
    assert rejection is not None
    assert rejection.render() == "five_hour\t9"


# --- Golden: post_session record write (worker.sh:283-323) ---


@pytest.mark.parametrize(
    "case", load_cases("post_session_record"), ids=case_ids("post_session_record")
)
def test_post_session_record_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    record_file = Path(v["item_record_file"])
    write_post_session_record(
        record_file,
        harness=v["BACKEND"],
        model=v["item_model"],
        session_id=v["item_session_id"],
        exit_code=int(v["item_exit_code"]),
        duration_seconds=int(v["item_duration"]),
        item_timeout=int(v["ITEM_TIMEOUT"]),
        trajectory_path=v["item_trajectory_path"],
        post_session=stub_post_session,
        make_store=StubStore,
    )
    assert json.loads(record_file.read_text()) == subst(case["expected_record"], ws)


# --- Golden: legacy fallback record write (worker.sh:325-373) ---


@pytest.mark.parametrize(
    "case", load_cases("fallback_record"), ids=case_ids("fallback_record")
)
def test_fallback_record_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    exit_code = int(v["item_exit_code"])
    outcome = fallback_outcome(exit_code)
    assert outcome == case["expected_fallback_outcome"]
    record_file = Path(v["item_record_file"])
    write_fallback_session_record(
        record_file,
        harness=v["BACKEND"],
        model=v["item_model"],
        outcome=outcome,
        session_id=v["item_session_id"],
        exit_code=exit_code,
        duration_seconds=int(v["item_duration"]),
        item_timeout=int(v["ITEM_TIMEOUT"]),
        trajectory_path=v["item_trajectory_path"],
        make_record=stub_make_record,
    )
    assert json.loads(record_file.read_text()) == subst(case["expected_record"], ws)


# --- Golden: PR-state diff (worker.sh:377-502) ---


@pytest.mark.parametrize(
    "case", load_cases("pr_state_diff"), ids=case_ids("pr_state_diff")
)
def test_pr_state_diff_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    record_file = Path(v["item_record_file"])
    before = case["record_before"]
    if isinstance(before, str):
        record_file.write_text(before)
    else:
        record_file.write_text(json.dumps(subst(before, ws)))

    gh_fail = case["gh_fail"]
    gh_output = case["gh_output"] or ""

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, returncode=1 if gh_fail else 0, stdout=gh_output, stderr=""
        )

    update_record_pr_state(
        record_file,
        repo=v["item_repo"],
        number=v["item_number"],
        before_json=v["item_pr_before_json"],
        cwd=ws,
        fetch=lambda repo, num: fetch_pr_snapshot(repo, num, cwd=ws, runner=runner),
        upgrade_outcome=stub_upgrade_outcome if case["upgrade_hook"] else None,
    )
    assert json.loads(record_file.read_text()) == subst(case["expected_record"], ws)


# --- Golden: worker-result manifest (worker.sh:505-627) ---


@pytest.mark.parametrize(
    "case", load_cases("worker_result"), ids=case_ids("worker_result")
)
def test_worker_result_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    record_file = Path(v["item_record_file"])
    record_file.write_text(json.dumps(subst(case["record_before"], ws)))
    write_worker_result_manifest(
        record_file,
        repo=v["item_repo"],
        number=v["item_number"],
        session_id=v["item_session_id"],
        exit_code=int(v["item_exit_code"]),
        duration_seconds=int(v["item_duration"]),
        model=v["item_model"],
        item_types=split_item_types(v["item_types"]),
        build_worker_result=stub_build_worker_result,
        write_worker_result=stub_write_worker_result,
        load_worker_result=stub_load_worker_result,
    )
    manifest_file = record_file.with_name(f"{record_file.stem}.worker-result.json")
    assert json.loads(record_file.read_text()) == subst(case["expected_record"], ws)
    assert json.loads(manifest_file.read_text()) == subst(case["expected_manifest"], ws)


# --- Golden: delivery-field extractor (worker.sh:651-666) ---


@pytest.mark.parametrize(
    "case", load_cases("delivery_field"), ids=case_ids("delivery_field")
)
def test_delivery_field_golden(case: dict[str, Any]) -> None:
    assert extract_delivery_field(case["raw_json"], case["field"]) == case["expected"]


# --- Golden: latency append (worker.sh:692-718) ---


@pytest.mark.parametrize(
    "case", load_cases("latency_append"), ids=case_ids("latency_append")
)
def test_latency_append_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        kwargs["repo_root"] = str(kwargs["repo_root"])
        calls.append(kwargs)

    kwargs = dict(
        repo_root=v["WORKSPACE"],
        item_json=v["item_json"],
        latency_context_json=v["item_latency_context_json"],
        ack_result_json=v["item_ack_result_json"],
        handled_at=v["_w_started_iso"],
        session_id=v["item_session_id"],
        outcome=v["_latency_outcome"],
        append_latency_records=capture,
    )
    if case["expected_call"] is None:
        # The heredoc died on this input (bash `|| true` swallows it).
        with pytest.raises(json.JSONDecodeError):
            append_worker_latency_records(**kwargs)
        assert calls == []
    else:
        append_worker_latency_records(**kwargs)
        assert calls == [subst(case["expected_call"], ws)]


# --- Golden: wait-and-merge gate log (worker.sh:743-766) ---


@pytest.mark.parametrize(
    "case", load_cases("wait_merge_gate"), ids=case_ids("wait_merge_gate")
)
def test_wait_merge_gate_golden(case: dict[str, Any], ws: Path) -> None:
    v = subst(case["vars"], ws)
    timestamp = "2026-07-10T15:00:00+00:00"
    kwargs = dict(
        timestamp=timestamp,
        repo=v["item_repo"],
        pr_number=v["item_number"],
        item_types=split_item_types(v["item_types"]),
        session_id=v["item_session_id"],
        session_start=v["_w_started_iso"],
        gate_exit_code=case["gate_exit_code"],
    )
    log_path = Path(v["_wait_merge_gate_log"])
    if case["expected_entry_sans_timestamp"] is None:
        with pytest.raises(json.JSONDecodeError):
            build_wait_merge_gate_entry(case["gate_json"], **kwargs)
        return
    entry = build_wait_merge_gate_entry(case["gate_json"], **kwargs)
    append_wait_merge_gate_log(log_path, entry)
    line = json.loads(log_path.read_text().strip())
    assert line.pop("timestamp") == timestamp
    assert line == subst(case["expected_entry_sans_timestamp"], ws)
    # The heredoc writes sorted keys (worker.sh:765).
    raw = log_path.read_text().strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


# --- Golden: arc pr_state_after reader (worker.sh:808-815) ---


@pytest.mark.parametrize(
    "case", load_cases("pr_state_after"), ids=case_ids("pr_state_after")
)
def test_pr_state_after_golden(case: dict[str, Any], ws: Path) -> None:
    record = ws / "records" / "arc.json"
    if case["content"] is not None:
        record.write_text(case["content"])
    assert read_record_pr_state_after(record) == case["expected"]


# --- Golden: item_types splitter (worker.sh:70) ---


@pytest.mark.parametrize("case", load_cases("item_types"), ids=case_ids("item_types"))
def test_item_types_golden(case: dict[str, Any]) -> None:
    assert split_item_types(case["text"]) == case["expected_json"]


# --- Table-driven: pure helpers ---


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, "unknown"), (124, "unknown"), (1, "failed"), (75, "failed"), (2, "failed")],
)
def test_fallback_outcome(exit_code: int, expected: str) -> None:
    # NOTE(parity) under test: timeout (124) records "unknown", not "failed".
    assert fallback_outcome(exit_code) == expected


def test_resolve_trajectory(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text("x")
    assert resolve_trajectory(str(f)) == f
    assert resolve_trajectory(str(tmp_path / "missing")) is None
    assert resolve_trajectory("") is None
    assert resolve_trajectory(None) is None
    # A directory is not a trajectory file.
    assert resolve_trajectory(str(tmp_path)) is None


def test_finalize_post_session_record_zero_timeout_omitted() -> None:
    out = finalize_post_session_record({"a": 1}, "B", 0)
    assert out == {"a": 1, "grade": "B"}
    out = finalize_post_session_record({"a": 1}, None, 60)
    assert out == {"a": 1, "grade": None, "timeout_seconds": 60}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"state": "open", "headRefOid": "ABC", "mergeCommit": {"oid": "DeF"}},
            {"state": "OPEN", "headRefOid": "abc", "mergeCommit": "def"},
        ),
        (
            {"state": None, "headRefOid": None, "mergeCommit": None},
            {"state": "", "headRefOid": "", "mergeCommit": ""},
        ),
        # Non-dict mergeCommit values pass through normalize_oid as-is.
        (
            {"state": "MERGED", "headRefOid": " X ", "mergeCommit": "OID"},
            {"state": "MERGED", "headRefOid": "x", "mergeCommit": "oid"},
        ),
        ("not-a-dict", {}),
        ([1, 2], {}),
    ],
)
def test_normalize_pr_snapshot(payload: Any, expected: dict[str, str]) -> None:
    assert normalize_pr_snapshot(payload) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("   ", {}),
        ("{bad", {}),
        ('"scalar"', {}),
        (
            '{"state":"open","headRefOid":"A","mergeCommit":null}',
            {"state": "OPEN", "headRefOid": "a", "mergeCommit": ""},
        ),
    ],
)
def test_parse_pr_snapshot(raw: str, expected: dict[str, str]) -> None:
    assert parse_pr_snapshot(raw) == expected


def test_dedupe_semantics_differ_between_blocks() -> None:
    # NOTE(parity) under test: the PR-state diff dedupes case-INsensitively
    # (first spelling wins); the manifest builder case-SENSITIVELY.
    values = ["abc", "ABC", " abc ", "", None]
    assert dedupe_deliverables(values) == ["abc"]
    assert normalize_manifest_deliverables(["abc", "ABC", " abc ", ""]) == [
        "abc",
        "ABC",
    ]
    assert normalize_manifest_deliverables("not-a-list") == []


def test_apply_pr_state_diff_does_not_clear_fields_on_empty_after() -> None:
    payload: dict[str, Any] = {"deliverables": []}
    apply_pr_state_diff(
        payload,
        {"state": "OPEN", "headRefOid": "aaa", "mergeCommit": ""},
        {},
    )
    assert payload["pr_state_before"] == "OPEN"
    assert payload["pr_head_oid_before"] == "aaa"
    assert "pr_state_after" not in payload
    # No head advance recorded without an after-head.
    assert payload["deliverables"] == []


def test_collect_commit_oids() -> None:
    oids = collect_commit_oids(
        ["Pushed deadbeefcafe twice: deadbeefcafe", "short ab12345"],
        "abc1234",
        "def5678",
    )
    assert oids == ["deadbeefcafe", "ab12345", "def5678"]
    # Head already present is not duplicated; same head consumes nothing.
    assert collect_commit_oids(["def5678"], "abc1234", "def5678") == ["def5678"]
    assert collect_commit_oids([], "same111", "same111") == []
    assert collect_commit_oids([], None, "def5678") == []


def test_apply_worker_result_to_payload_filters_falsy_artifacts() -> None:
    payload: dict[str, Any] = {}
    normalized = {
        "status": None,
        "schema_version": 2,
        "blocked_reason": "",
        "artifact_paths": {"a": "/x", "b": None, "c": ""},
        "git_refs": {"commit_oids": ["x"]},
        "task": {"intended_category": ""},
    }
    apply_worker_result_to_payload(payload, normalized, "/m.json")
    assert payload["worker_status"] == "failed"
    assert payload["worker_manifest_schema"] == "bob.worker-result.v2"
    assert "worker_blocked_reason" not in payload
    assert payload["worker_artifact_paths"] == {"a": "/x"}
    assert payload["worker_git_refs"] == {"commit_oids": ["x"]}
    assert "worker_intended_category" not in payload


def test_write_worker_result_manifest_raises_on_non_numeric_number(
    ws: Path,
) -> None:
    # NOTE(parity) under test: int("") dies in the heredoc (bash || true).
    record = ws / "records" / "r.json"
    record.write_text(json.dumps({"deliverables": []}))
    with pytest.raises(ValueError):
        write_worker_result_manifest(
            record,
            repo="gptme/gptme",
            number="",
            session_id="s",
            exit_code=0,
            duration_seconds=1,
            model=None,
            item_types=[],
            build_worker_result=stub_build_worker_result,
            write_worker_result=stub_write_worker_result,
            load_worker_result=stub_load_worker_result,
        )


def test_update_record_pr_state_preserves_concurrent_manifest_fields(ws: Path) -> None:
    record = ws / "records" / "r.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"outcome": "handled"}))

    def fetch(repo: str, number: int) -> dict[str, str]:
        # Simulate the manifest writer landing while the network fetch runs.
        payload = json.loads(record.read_text())
        payload["worker_status"] = "succeeded"
        record.write_text(json.dumps(payload))
        return {"state": "OPEN", "headRefOid": "after123"}

    update_record_pr_state(
        record,
        repo="gptme/gptme",
        number=1,
        before_json=json.dumps({"state": "OPEN", "headRefOid": "before12"}),
        cwd=ws,
        fetch=fetch,
    )

    payload = json.loads(record.read_text())
    assert payload["worker_status"] == "succeeded"
    assert payload["pr_head_oid_after"] == "after123"


def test_write_worker_result_manifest_preserves_concurrent_record_fields(
    ws: Path,
) -> None:
    record = ws / "records" / "r.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"outcome": "handled", "deliverables": []}))

    def write_manifest(path: Path, manifest: Any) -> None:
        # Simulate the PR-state writer landing during manifest construction.
        payload = json.loads(record.read_text())
        payload["pr_state_after"] = "OPEN"
        record.write_text(json.dumps(payload))
        stub_write_worker_result(path, manifest)

    write_worker_result_manifest(
        record,
        repo="gptme/gptme",
        number=1,
        session_id="s",
        exit_code=0,
        duration_seconds=1,
        model=None,
        item_types=[],
        build_worker_result=stub_build_worker_result,
        write_worker_result=write_manifest,
        load_worker_result=stub_load_worker_result,
    )

    payload = json.loads(record.read_text())
    assert payload["pr_state_after"] == "OPEN"
    assert payload["worker_status"] == "completed"


def test_write_record_failure_preserves_last_valid_json(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = ws / "records" / "r.json"
    record.write_text(json.dumps({"outcome": "handled"}))

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated crash before replace")

    monkeypatch.setattr("gptme_runloops.utils.state.os.replace", fail_replace)
    with pytest.raises(OSError):
        update_record_pr_state(
            record,
            repo="gptme/gptme",
            number=1,
            before_json="{}",
            cwd=ws,
            fetch=lambda repo, number: {"state": "OPEN"},
        )

    assert json.loads(record.read_text()) == {"outcome": "handled"}


def test_update_record_pr_state_missing_record_is_noop(ws: Path) -> None:
    update_record_pr_state(
        ws / "records" / "missing.json",
        repo="gptme/gptme",
        number=1,
        before_json="",
        cwd=ws,
        fetch=lambda repo, num: {},
    )
    assert not (ws / "records" / "missing.json").exists()


def test_fetch_pr_snapshot_nonzero_exit_is_empty(ws: Path) -> None:
    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(args, returncode=1, stdout="{}", stderr="")

    assert fetch_pr_snapshot("gptme/gptme", 1, cwd=ws, runner=runner) == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("handled", "handled"),
        ("  handled\n", "handled"),
        ("orphan_no_delivery", "orphan_no_delivery"),
        ("no_action_needed", "no_action_needed"),
        ("failed", "failed"),
        ("", "handled"),
        ("something_else", "handled"),
        # NOTE(parity) under test: tr -d '[:space:]' removes interior
        # whitespace too — "no action" → "noaction" (no match), while a
        # space INSIDE a known outcome still whitelist-matches.
        ("no action", "handled"),
        ("or phan_no_delivery", "orphan_no_delivery"),
    ],
)
def test_normalize_delivery_outcome(raw: str, expected: str) -> None:
    assert normalize_delivery_outcome(raw) == expected


@pytest.mark.parametrize(
    ("delivery", "exit_code", "needs", "posted", "expected"),
    [
        # exit 0 → delivery outcome passes through.
        ("handled", 0, "false", "false", "handled"),
        ("orphan_no_delivery", 0, "true", "false", "orphan_no_delivery"),
        # non-zero exit but fallback reply posted → passes through.
        ("handled", 1, "false", "true", "handled"),
        # non-zero exit, orphan + fallback wanted → orphan.
        ("orphan_no_delivery", 1, "true", "false", "orphan_no_delivery"),
        # non-zero exit, orphan but no fallback wanted → failed.
        ("orphan_no_delivery", 1, "false", "false", "failed"),
        # non-zero exit, anything else → failed.
        ("handled", 124, "false", "false", "failed"),
        ("no_action_needed", 2, "true", "", "failed"),
    ],
)
def test_compute_latency_outcome(
    delivery: str, exit_code: int, needs: str, posted: str, expected: str
) -> None:
    assert (
        compute_latency_outcome(
            delivery,
            exit_code,
            needs_fallback_reply=needs,
            fallback_reply_posted=posted,
        )
        == expected
    )


def test_parse_latency_inputs_defaults() -> None:
    item, ctx, ack = parse_latency_inputs('{"repo":"r"}', "", "")
    assert item == {"repo": "r"}
    assert ctx == []
    assert ack is None
    with pytest.raises(json.JSONDecodeError):
        parse_latency_inputs("", "", "")


# ---------------------------------------------------------------------------
# detect_worker_outcome_subtype / augment_with_outcome_subtype
# ---------------------------------------------------------------------------


def _traj(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """Write a trajectory JSONL fixture and return its path."""
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _cc_bash(cmd: str) -> str:
    """One CC-format assistant entry whose Bash tool_use runs ``cmd``."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": cmd},
                    }
                ]
            },
        }
    )


def _gptme_bash_at(cmd: str) -> str:
    """One gptme-format assistant entry with AT-style tool call."""
    return json.dumps(
        {
            "role": "assistant",
            "content": f'@shell(abc123): {{"command": {json.dumps(cmd)}}}',
        }
    )


def _gptme_bash_fence(cmd: str) -> str:
    """One gptme-format assistant entry with fence-style bash block."""
    return json.dumps(
        {
            "role": "assistant",
            "content": f"```bash\n{cmd}\n```",
        }
    )


def test_detect_subtype_no_trajectory() -> None:
    assert detect_worker_outcome_subtype(None) == "observe"


def test_detect_subtype_missing_file(tmp_path: Path) -> None:
    assert detect_worker_outcome_subtype(tmp_path / "ghost.jsonl") == "observe"


def test_detect_subtype_empty_trajectory(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "empty.jsonl", [])
    assert detect_worker_outcome_subtype(traj) == "observe"


def test_detect_subtype_cc_ship_pr_create(tmp_path: Path) -> None:
    traj = _traj(
        tmp_path, "ship.jsonl", [_cc_bash("gh pr create --title 'foo' --body 'bar'")]
    )
    assert detect_worker_outcome_subtype(traj) == "ship"


def test_detect_subtype_cc_ship_pr_merge(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "merge.jsonl", [_cc_bash("gh pr merge 42 --squash")])
    assert detect_worker_outcome_subtype(traj) == "ship"


def test_detect_subtype_cc_engage_git_push(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "push.jsonl", [_cc_bash("git push origin HEAD")])
    assert detect_worker_outcome_subtype(traj) == "engage"


def test_detect_subtype_cc_engage_pr_comment(tmp_path: Path) -> None:
    traj = _traj(
        tmp_path, "comment.jsonl", [_cc_bash("gh pr comment 99 --body 'LGTM'")]
    )
    assert detect_worker_outcome_subtype(traj) == "engage"


def test_detect_subtype_cc_engage_pr_review(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "review.jsonl", [_cc_bash("gh pr review 7 --approve")])
    assert detect_worker_outcome_subtype(traj) == "engage"


def test_detect_subtype_cc_engage_gh_api_comment(tmp_path: Path) -> None:
    traj = _traj(
        tmp_path,
        "api_comment.jsonl",
        [_cc_bash("gh api repos/owner/repo/pulls/5/comments -f body='fixed'")],
    )
    assert detect_worker_outcome_subtype(traj) == "engage"


def test_detect_subtype_cc_observe_only(tmp_path: Path) -> None:
    """Read-only commands (gh pr view, gh pr checks, git log) → observe."""
    traj = _traj(
        tmp_path,
        "observe.jsonl",
        [
            _cc_bash("gh pr view 42 --json state"),
            _cc_bash("gh pr checks 42"),
            _cc_bash("git log --oneline -5"),
        ],
    )
    assert detect_worker_outcome_subtype(traj) == "observe"


def test_detect_subtype_ship_wins_over_engage(tmp_path: Path) -> None:
    """When both engage and ship commands appear, ship wins (early return)."""
    traj = _traj(
        tmp_path,
        "ship_over_engage.jsonl",
        [
            _cc_bash("git push origin HEAD"),  # engage
            _cc_bash("gh pr create --title 'X'"),  # ship
        ],
    )
    assert detect_worker_outcome_subtype(traj) == "ship"


def test_detect_subtype_gptme_at_format(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "at.jsonl", [_gptme_bash_at("gh pr create --title 'y'")])
    assert detect_worker_outcome_subtype(traj) == "ship"


def test_detect_subtype_gptme_fence_format_engage(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "fence.jsonl", [_gptme_bash_fence("git push origin HEAD")])
    assert detect_worker_outcome_subtype(traj) == "engage"


def test_detect_subtype_skips_system_entries(tmp_path: Path) -> None:
    """System-prompt / user entries that contain ship commands must not fire."""
    system_entry = json.dumps(
        {
            "type": "system",
            "message": {"content": "gh pr create --title 'ARCHITECTURE.md text'"},
        }
    )
    user_entry = json.dumps(
        {
            "role": "user",
            "content": "gh pr merge 1 --squash",
        }
    )
    traj = _traj(
        tmp_path,
        "injected.jsonl",
        [system_entry, user_entry, _cc_bash("git log --oneline -3")],
    )
    assert detect_worker_outcome_subtype(traj) == "observe"


def test_augment_adds_subtype_for_productive(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "a.jsonl", [_cc_bash("gh pr merge 1 --squash")])
    payload: dict[str, Any] = {"outcome": "productive", "session_id": "s1"}
    result = augment_with_outcome_subtype(payload, traj)
    assert result is payload  # mutates in place
    assert result["outcome_subtype"] == "ship"


def test_augment_skips_non_productive(tmp_path: Path) -> None:
    traj = _traj(tmp_path, "b.jsonl", [_cc_bash("gh pr merge 1 --squash")])
    for outcome in ("failed", "unknown", "noop", ""):
        payload: dict[str, Any] = {"outcome": outcome}
        augment_with_outcome_subtype(payload, traj)
        assert (
            "outcome_subtype" not in payload
        ), f"should not annotate outcome={outcome!r}"


def test_augment_observe_when_no_trajectory() -> None:
    payload: dict[str, Any] = {"outcome": "productive"}
    augment_with_outcome_subtype(payload, None)
    assert payload["outcome_subtype"] == "observe"
