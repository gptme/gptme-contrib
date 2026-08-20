"""Tests for the run-item executor (step 4).

Structure:

- **Pure helpers** — slug/session-id/trajectory-path/timeout-tier/claim-key/
  lock-path derivations pinned against the bash expressions.
- **Table-driven plans** — one work-item fixture per merge-lifecycle decision
  path, asserting the resulting ExecutionPlan (prompt kind, timeout tier,
  claim key, skip flag) with a fake lifecycle IO; no subprocesses.
- **Execution paths** — run_work_file end-to-end with a fake ``run_cmd``
  dispatcher and fake record collaborators (no live LLM calls, no gh):
  ledger rows, claim acquire/deny/abandon, runner argv/env, rate-limit
  early-break, exit-code propagation.
- **Post-session composition** — the worker.sh shim order (record write +
  fallback, PR-state diff, manifest, delivery fields, latency append,
  wait-merge gate, arc update/close, state promotion) against recorded
  fixtures.
- **Rate-limit + trajectory resolution** — worker.sh:100-300 semantics on
  tmp dirs (confirmed-rejection-only blocking; per-backend snapshot diffs).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from gptme_runloops.merge_lifecycle import (
    LifecycleResult,
    SelfMergeCheckResult,
    run_merge_lifecycle,
)
from gptme_runloops.run_item import (
    PR_OBSERVE_TYPES,
    PR_STATE_TYPES,
    THREAD_DELIVERABLE_TYPES,
    ArcInfo,
    RunItem,
    RunItemConfig,
    RunItemHooks,
    _handle_cc_rate_limit,
    _inspect_cc_failure,
    build_execution_plan,
    clear_slot_event_markers,
    derive_lock_paths,
    derive_session_id,
    execute_plan,
    issue_coordination_key,
    item_slug,
    plan_item,
    predict_cc_trajectory_path,
    promote_item_state,
    redelivery_attempts_file,
    resolve_backend_trajectory,
    resolve_cc_sub_suffix,
    rollback_failed_delivery,
    run_post_session,
    run_work_file,
    snapshot_codex_rollouts,
    snapshot_copilot_dirs,
    timeout_tier,
    write_rate_limit_block_file,
)

# --- Fakes ---


class FakeRunCmd:
    """Argv-dispatching subprocess fake for the hooks.run_cmd seam."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rules: list[tuple] = []

    def on(self, needle: str, returncode: int = 0, stdout: str = "", stderr: str = ""):
        """Match any call whose argv contains *needle* in any element."""
        self.rules.append((needle, returncode, stdout, stderr))
        return self

    def argvs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls]

    def find(self, needle: str) -> list[dict]:
        return [c for c in self.calls if any(needle in str(a) for a in c["argv"])]

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, **kwargs})
        for needle, rc, out, err in self.rules:
            if any(needle in a for a in argv):
                return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")


@dataclass
class FakeLifecycleIO:
    check: SelfMergeCheckResult = field(
        default_factory=lambda: SelfMergeCheckResult(
            eligible=False, reasons=("CI red",)
        )
    )
    merge_ok: bool = False
    status: str = "already-reviewed"
    merged: list = field(default_factory=list)
    triggered: list = field(default_factory=list)
    promoted: list = field(default_factory=list)

    def self_merge_check(self, repo, number):
        return self.check

    def self_merge(self, repo, number):
        self.merged.append((repo, number))
        return self.merge_ok

    def greptile_status(self, repo, number):
        return self.status

    def trigger_review(self, repo, number):
        self.triggered.append((repo, number))

    def promote_item_state(self, repo, number):
        self.promoted.append((repo, number))


def make_config(tmp_path: Path, **kwargs) -> RunItemConfig:
    defaults = dict(
        workspace=tmp_path,
        author="TimeToBuildBob",
        agent_name="Bob",
        operator_name="Erik",
        primary_repo="ErikBjare/bob",
        greptile_repos_pattern="^(gptme/gptme|gptme/gptme-contrib)$",
        self_merge_repos="ErikBjare/bob",
        wait_merge_auto_enabled_repos="ErikBjare/bob",
        state_dir=tmp_path / "state-dir",
        pending_state_dir=tmp_path / "pending-state-dir",
        lock_dir=tmp_path,
        lock_stem="test-project-monitoring",
        lock_history=tmp_path / "lock-history.log",
        records_dir=tmp_path / "records",
        dispatch_ledger=tmp_path / "dispatch.jsonl",
        wait_merge_gate_log=tmp_path / "gates.jsonl",
        backend_quota_dir=tmp_path / "backend-quota",
        cc_projects_dir=tmp_path / "cc-projects",
        cc_credentials_path=tmp_path / "credentials.json",
        copilot_state_dir=tmp_path / "copilot-state",
        codex_sessions_dir=tmp_path / "codex-sessions",
        monitoring_rules_file=tmp_path / "monitoring-rules.md",
    )
    defaults.update(kwargs)
    return RunItemConfig(**defaults)


def make_item(**kwargs) -> RunItem:
    data = dict(
        repo="gptme/gptme-contrib",
        number=1234,
        title="fix: a PR",
        detail="review comment",
        types=["pr_update"],
        all_numbers=["1234"],
    )
    data.update(kwargs)
    line = json.dumps({**data, "type": "+".join(data["types"])})
    return RunItem.from_grouped_json(line)


def make_hooks(**kwargs) -> RunItemHooks:
    defaults = dict(
        runner=["/fake/run.sh"],
        run_cmd=FakeRunCmd(),
    )
    defaults.update(kwargs)
    return RunItemHooks(**defaults)


# --- Pure helpers ---


def test_item_slug_matches_bash_tr() -> None:
    # printf '%s_%s_%s' | tr '/# :' '----'
    assert item_slug("gptme/gptme-contrib", "1234", 1) == "gptme-gptme-contrib_1234_1"
    assert item_slug("a/b", "master-ci:check x", 2) == "a-b_master-ci-check-x_2"


def test_session_id_is_uuid5_salted() -> None:
    import uuid

    slug = "gptme-gptme_1_1"
    sid = derive_session_id(slug, 1720000000)
    assert sid == str(
        uuid.uuid5(uuid.NAMESPACE_DNS, "monitor-gptme-gptme_1_1-1720000000")
    )
    assert derive_session_id(slug, 1720000001) != sid
    # Stable within a run (retries get the same UUID)
    assert derive_session_id(slug, 1720000000) == sid


def test_predict_cc_trajectory_path() -> None:
    got = predict_cc_trajectory_path(
        Path("/home/x/.claude/projects"), "/home/bob/bob", "abc-123"
    )
    assert got == "/home/x/.claude/projects/-home-bob-bob/abc-123.jsonl"


@pytest.mark.parametrize(
    ("types", "has_fix", "expected"),
    [
        (["pr_update"], False, (900, "~10 minutes")),
        (["pr_update"], True, (2700, "~35 minutes")),
        (["assigned_issue"], False, (1500, "~20 minutes")),
        # assigned_issue wins over the greptile-fix tier (bash if/elif order)
        (["assigned_issue", "pr_update"], True, (1500, "~20 minutes")),
        (["greptile_needs_fix"], True, (2700, "~35 minutes")),
        (["notification"], False, (900, "~10 minutes")),
        (["merge_ready"], True, (900, "~10 minutes")),
    ],
)
def test_timeout_tiers(types, has_fix, expected, tmp_path) -> None:
    assert timeout_tier(types, has_fix, make_config(tmp_path)) == expected


@pytest.mark.parametrize(
    ("types", "repo", "number", "expected"),
    [
        (["pr_update"], "o/r", 12, "github:o/r#12"),
        (["assigned_issue"], "o/r", "7", "github:o/r#7"),
        (["merge_ready"], "o/r", 12, None),  # not in the claimable types
        (["master_ci_failure"], "o/r", 999, None),
        (["pr_update"], "o/r", 0, None),  # non-positive
        (["pr_update"], "o/r", "12abc", None),  # non-numeric
        (["pr_update"], "", 12, None),
        (["pr_update"], "o/r", None, None),
    ],
)
def test_issue_coordination_key(types, repo, number, expected) -> None:
    assert issue_coordination_key(types, repo, number) == expected


def test_derive_lock_paths(tmp_path) -> None:
    config = make_config(tmp_path)
    global_lock, scope = derive_lock_paths(config, None)
    assert global_lock == tmp_path / "test-project-monitoring.lock"
    assert scope == "global"
    slot_lock, scope = derive_lock_paths(config, "gptme/gptme#123")
    assert slot_lock == tmp_path / "test-project-monitoring-gptme-gptme-123.lock"
    assert scope == "slot:gptme/gptme#123"


def test_resolve_cc_sub_suffix(tmp_path) -> None:
    link = tmp_path / "credentials.json"
    target = tmp_path / ".credentials.json.bob"
    target.write_text("{}")
    link.symlink_to(target)
    assert resolve_cc_sub_suffix(link) == "bob-"
    # Non-suffixed target → no suffix
    plain = tmp_path / "plain.json"
    plain_target = tmp_path / ".credentials.json"
    plain_target.write_text("{}")
    plain.symlink_to(plain_target)
    assert resolve_cc_sub_suffix(plain) == ""
    # Not a symlink at all
    assert resolve_cc_sub_suffix(tmp_path / "missing") == ""


def test_rate_limit_block_file_seven_day_sonnet(tmp_path) -> None:
    path, msg = write_rate_limit_block_file(
        tmp_path / "quota", "seven_day_sonnet", "1760000000", "bob-"
    )
    assert path.name == "claude-code-bob-sonnet-rate-limited-until.txt"
    assert "blocked until" in msg
    written = path.read_text().strip()
    assert datetime.fromisoformat(written).timestamp() == 1760000000


def test_rate_limit_block_file_unknown_reset(tmp_path) -> None:
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    path, msg = write_rate_limit_block_file(
        tmp_path / "quota", "seven_day", "0", "", now=now
    )
    assert path.name == "claude-code-rate-limited-until.txt"
    assert "blocked 6h" in msg
    until = datetime.fromisoformat(path.read_text().strip())
    assert (until - now).total_seconds() == 6 * 3600


# --- RunItem parsing ---


def test_run_item_from_grouped_json_full_shape() -> None:
    line = json.dumps(
        {
            "repo": "o/r",
            "number": 5,
            "title": "t",
            "types": ["ci_failure", "pr_update"],
            "type": "ci_failure+pr_update",
            "detail": "d",
            "all_numbers": [5],
            "future_field": {"x": 1},
        }
    )
    item = RunItem.from_grouped_json(line)
    assert item.repo == "o/r"
    assert item.types == ("ci_failure", "pr_update")
    assert item.type_label == "ci_failure+pr_update"
    assert item.all_numbers == ("5",)
    assert item.raw["future_field"] == {"x": 1}  # unknown fields preserved
    assert item.raw_line == line


def test_run_item_falls_back_to_single_type() -> None:
    item = RunItem.from_grouped_json(
        '{"repo": "o/r", "number": 0, "type": "notification", "title": "", "detail": "mention"}'
    )
    assert item.types == ("notification",)
    assert item.number_str == "0"


# --- Table-driven decision-path plans ---


def _plan_for(
    tmp_path,
    item: RunItem,
    io: FakeLifecycleIO,
    *,
    claim_mode: str = "acquire",
    backend: str = "claude-code",
):
    config = make_config(tmp_path)
    lifecycle = run_merge_lifecycle(
        item.to_merge_lifecycle_item(), config.lifecycle_config(), io
    )
    plan = plan_item(
        item,
        index=1,
        config=config,
        backend=backend,
        model="claude-sonnet-4-6",
        monitoring_rules="RULES",
        lifecycle=lifecycle,
        arc=None,
        run_salt=1720000000,
        records_dir=tmp_path / "records",
        claim_mode=claim_mode,
        runner=["/fake/run.sh"],
        sysprompt_file="/tmp/sys.txt",
    )
    return plan, lifecycle, io


def test_plan_self_merge_eligible_skips_session(tmp_path) -> None:
    io = FakeLifecycleIO(check=SelfMergeCheckResult(eligible=True), merge_ok=True)
    plan, lifecycle, io = _plan_for(tmp_path, make_item(types=["merge_ready"]), io)
    assert plan.skip_item is True
    assert io.merged == [("gptme/gptme-contrib", 1234)]
    assert io.promoted == [("gptme/gptme-contrib", 1234)]
    assert [d["action"] for d in plan.lifecycle_decisions] == [
        "self_merge",
        "skip_item",
    ]


def test_plan_unresolved_threads_injects_local_fix(tmp_path) -> None:
    io = FakeLifecycleIO(
        check=SelfMergeCheckResult(
            eligible=False, reasons=("Greptile has 2 unresolved review thread(s)",)
        )
    )
    plan, _, _ = _plan_for(tmp_path, make_item(repo="ErikBjare/bob"), io)
    assert plan.skip_item is False
    assert plan.instruction_kind == "local_greptile_fix"
    assert plan.timeout == 2700 and plan.time_desc == "~35 minutes"
    assert "Address Greptile Review Findings" in plan.prompt
    assert "You have ~35 minutes available" in plan.prompt


def test_plan_score_below_floor_injects_local_fix(tmp_path) -> None:
    io = FakeLifecycleIO(
        check=SelfMergeCheckResult(
            eligible=False, reasons=("Greptile score 3/5 below floor 4/5",)
        )
    )
    plan, _, _ = _plan_for(tmp_path, make_item(repo="ErikBjare/bob"), io)
    assert plan.instruction_kind == "local_greptile_fix"
    assert plan.timeout == 2700


def test_plan_no_review_triggers_and_proceeds_default_tier(tmp_path) -> None:
    io = FakeLifecycleIO(
        check=SelfMergeCheckResult(
            eligible=False, reasons=("Greptile review not found",)
        ),
        # Phase B on the cross-repo item then reports in-progress (just triggered)
        status="in-progress",
    )
    plan, _, io = _plan_for(tmp_path, make_item(), io)
    assert io.triggered == [("gptme/gptme-contrib", 1234)]
    assert plan.instruction_kind is None
    assert plan.timeout == 900
    assert "Address Greptile Review Findings" not in plan.prompt


def test_plan_cross_repo_needs_re_review_injects_refresh(tmp_path) -> None:
    io = FakeLifecycleIO(status="needs-re-review")
    plan, _, _ = _plan_for(tmp_path, make_item(), io)
    assert plan.instruction_kind == "cross_repo_greptile_refresh"
    assert plan.timeout == 2700
    assert "Address Greptile Review Findings (cross-repo)" in plan.prompt


def test_plan_assigned_issue_tier_claim_and_ack(tmp_path) -> None:
    item = make_item(
        types=["assigned_issue"], detail="assigned; updated: 2026-07-11T00:00:00Z"
    )
    plan, _, _ = _plan_for(tmp_path, item, FakeLifecycleIO())
    assert plan.timeout == 1500 and plan.time_desc == "~20 minutes"
    assert plan.claim_key == "github:gptme/gptme-contrib#1234"
    assert plan.claim_agent == f"project-monitoring-claude-code-{plan.session_id}"
    assert plan.ack_intent is True


def test_plan_pending_reply_followup_skips_ack(tmp_path) -> None:
    item = make_item(
        types=["assigned_issue"], detail="pending_reply_followup; updated: x"
    )
    plan, _, _ = _plan_for(tmp_path, item, FakeLifecycleIO())
    assert plan.ack_intent is False


def test_plan_notification_has_no_claim(tmp_path) -> None:
    item = make_item(types=["notification"], number=0)
    plan, _, _ = _plan_for(tmp_path, item, FakeLifecycleIO())
    assert plan.claim_key is None
    assert plan.claim_agent is None
    assert plan.timeout == 900


def test_plan_direct_mention_injects_constraint(tmp_path) -> None:
    item = make_item(types=["notification"], number=0, detail="comment; mention")
    plan, _, _ = _plan_for(tmp_path, item, FakeLifecycleIO())
    assert "Required: Produce a Deliverable (Direct @Mention)" in plan.prompt


def test_plan_preheld_mode_renders_claim_block(tmp_path) -> None:
    plan, _, _ = _plan_for(
        tmp_path, make_item(), FakeLifecycleIO(), claim_mode="preheld"
    )
    assert plan.claim_mode == "preheld"
    assert "## Coordination Claim (pre-held)" in plan.prompt
    assert "`github:gptme/gptme-contrib#1234`" in plan.prompt


def test_plan_acquire_mode_renders_no_claim_block(tmp_path) -> None:
    plan, _, _ = _plan_for(tmp_path, make_item(), FakeLifecycleIO())
    assert "Coordination Claim (pre-held)" not in plan.prompt


def test_plan_runner_argv_and_env(tmp_path) -> None:
    plan, _, _ = _plan_for(tmp_path, make_item(), FakeLifecycleIO())
    argv = plan.runner_argv
    assert argv[0] == "/fake/run.sh"
    assert argv[-1] == plan.prompt  # prompt is the positional tail
    flags = argv[1:-1]
    assert flags[:7] == [
        "--backend",
        "claude-code",
        "--no-lock",
        "--no-pull",
        "--no-grade",
        "--sysprompt-file",
        "/tmp/sys.txt",
    ]
    assert flags[7:9] == ["--timeout", "900"]
    assert flags[9:11] == ["--model", "claude-sonnet-4-6"]
    assert plan.runner_env == {"CC_SESSION_ID": plan.session_id}
    assert plan.trajectory_path.endswith(f"/{plan.session_id}.jsonl")


def test_plan_gptme_env(tmp_path) -> None:
    plan, _, _ = _plan_for(tmp_path, make_item(), FakeLifecycleIO(), backend="gptme")
    assert plan.runner_env == {"BOB_SESSION_ID": plan.session_id}
    assert plan.trajectory_path == ""


def test_plan_grok_build_env(tmp_path) -> None:
    plan, _, _ = _plan_for(
        tmp_path, make_item(), FakeLifecycleIO(), backend="grok-build"
    )
    assert plan.runner_env == {"GROK_BUILD_SESSION_ID": plan.session_id}
    assert plan.trajectory_path == ""  # CC prediction only


# --- Dry-run ExecutionPlan ---


def test_build_execution_plan_dry_run(tmp_path) -> None:
    work_file = tmp_path / "slot.jsonl"
    work_file.write_text(make_item().raw_line + "\n")
    config = make_config(tmp_path)
    io = FakeLifecycleIO(check=SelfMergeCheckResult(eligible=True))
    hooks = make_hooks(merge_lifecycle_io=io)
    plan = build_execution_plan(
        work_file,
        config,
        hooks,
        backend="claude-code",
        model="claude-sonnet-4-6",
        lane="slow",
        dispatch_id="bob-pm-slow-slot-x",
        slot_key="gptme/gptme-contrib#1234",
        claim_mode="acquire",
    )
    # Dry run: the REAL merge was never attempted; the intent was recorded.
    assert io.merged == []
    assert plan.items[0].skip_item is True
    assert plan.items[0].dry_run_intents == [
        "would self-merge gptme/gptme-contrib#1234",
        "would promote item state for gptme/gptme-contrib#1234",
    ]
    payload = json.loads(plan.to_json())
    assert payload["lane"] == "slow"
    assert payload["lock_scope"] == "slot:gptme/gptme-contrib#1234"
    assert payload["items"][0]["session_id"]
    assert payload["items"][0]["prompt_chars"] == len(payload["items"][0]["prompt"])


def test_build_execution_plan_skips_malformed_lines(tmp_path) -> None:
    work_file = tmp_path / "slot.jsonl"
    work_file.write_text("not json\n" + make_item().raw_line + "\n")
    plan = build_execution_plan(
        work_file, make_config(tmp_path), make_hooks(), backend="claude-code"
    )
    assert len(plan.items) == 1


# --- run_work_file end-to-end (fake subprocesses) ---


def _write_work_file(tmp_path: Path, *items: RunItem) -> Path:
    work_file = tmp_path / "slot.jsonl"
    work_file.write_text("".join(i.raw_line + "\n" for i in items))
    return work_file


def _ledger_rows(config: RunItemConfig) -> list[dict]:
    path = config.resolved_dispatch_ledger
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_run_work_file_missing_work_file(tmp_path) -> None:
    rc = run_work_file(
        tmp_path / "nope.jsonl",
        make_config(tmp_path),
        make_hooks(),
        backend="claude-code",
    )
    assert rc == 1


def test_run_work_file_happy_path(tmp_path) -> None:
    (tmp_path / "monitoring-rules.md").write_text("RULES CONTENT")
    item = make_item()
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("rev-parse", stdout="abc123\n")
    run_cmd.on(
        "gh", stdout='{"state": "OPEN", "headRefOid": "AA", "mergeCommit": null}'
    )
    hooks = make_hooks(
        run_cmd=run_cmd,
        merge_lifecycle_io=FakeLifecycleIO(status="in-progress"),
        claim_tool=["fake-coordination"],
    )
    rc = run_work_file(
        work_file,
        config,
        hooks,
        backend="claude-code",
        model="claude-sonnet-4-6",
        lane="slow",
        dispatch_id="unit-1",
        slot_key="gptme/gptme-contrib#1234",
    )
    assert rc == 0

    phases = [r["phase"] for r in _ledger_rows(config)]
    assert phases == ["started", "completed"]
    completed = _ledger_rows(config)[-1]
    assert completed["successes"] == 1 and completed["failures"] == 0
    assert completed["note"] == "transient_completed"

    runner_calls = run_cmd.find("/fake/run.sh")
    assert len(runner_calls) == 1
    argv = runner_calls[0]["argv"]
    assert argv[1:3] == ["--backend", "claude-code"]
    assert "CC_SESSION_ID" in runner_calls[0]["env"]

    claim_calls = [c["argv"] for c in run_cmd.find("fake-coordination")]
    assert ["fake-coordination", "work-claim"] == claim_calls[0][:2]
    assert ["fake-coordination", "work-abandon"] == claim_calls[-1][:2]

    # Lock released (file truncated) and history written
    lock_file, _ = derive_lock_paths(config, "gptme/gptme-contrib#1234")
    assert lock_file.read_text() == ""
    history = config.lock_history.read_text()
    assert "ACQUIRED" in history and "RELEASED" in history


def test_run_work_file_claim_denied_skips_and_exits_zero(tmp_path) -> None:
    item = make_item()
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("work-claim", returncode=1)
    hooks = make_hooks(run_cmd=run_cmd, claim_tool=["fake-coordination"])
    rc = run_work_file(work_file, config, hooks, backend="claude-code")
    assert rc == 0
    assert run_cmd.find("/fake/run.sh") == []
    rows = _ledger_rows(config)
    skipped = [r for r in rows if r["phase"] == "skipped_claimed"]
    assert len(skipped) == 1
    assert skipped[0]["note"] == "coordination_claim_denied:gptme/gptme-contrib#1234"


def test_run_work_file_lock_busy_exits_zero(tmp_path) -> None:
    import fcntl

    item = make_item()
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    lock_file, _ = derive_lock_paths(config, "k")
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        run_cmd = FakeRunCmd()
        rc = run_work_file(
            work_file,
            config,
            make_hooks(run_cmd=run_cmd),
            backend="claude-code",
            slot_key="k",
        )
        assert rc == 0
        assert run_cmd.find("/fake/run.sh") == []
    finally:
        os.close(fd)


def test_run_work_file_propagates_session_exit_code(tmp_path) -> None:
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=124)
    rc = run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")
    assert rc == 124
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    # Timeouts are NOT counted as failures (worker.sh:100-104 elif chain)
    assert completed["failures"] == 0


def test_timeout_is_not_counted_as_a_success(tmp_path) -> None:
    """A hard-killed worker (exit 124) must not appear in `successes`.

    Measured on state/project-monitoring-dispatch.jsonl 2026-08-11: all 17 rows
    with exit_code 124 recorded successes=1, failures=0, outcome="failed" — a
    row that is simultaneously a success and a failure. Every metric summing
    `successes` over-counted by those rows.
    """
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=124)

    assert (
        run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")
        == 124
    )

    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["successes"] == 0
    assert completed["failures"] == 0  # still not a quality failure
    assert (
        completed["timeouts"] == 1
    )  # timed-out item is persisted so the row is self-describing
    assert completed["exit_code"] == 124
    assert completed["outcome"] == "failed"


def test_run_work_file_counts_failures(tmp_path) -> None:
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=3)
    rc = run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")
    assert rc == 3
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["failures"] == 1 and completed["successes"] == 0


# --- Outcome-verification invariant (symptom tests) ---
#
# The failure class: a worker exits non-zero while its ledger row says
# `completed`, so nothing retries and nothing alerts (gptme/gptme#3468).
# The invariant: the recorded outcome is DERIVED from exit status and
# observable effect, never asserted by the phase alone.


def test_completed_row_does_not_record_success_when_worker_exits_nonzero(
    tmp_path,
) -> None:
    """THE symptom test: run a worker that exits 1, assert no recorded success."""
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=1)

    rc = run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")

    assert rc == 1
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["exit_code"] == 1
    assert completed["outcome"] == "failed"
    assert completed["outcome"] != "succeeded"


def test_timeout_exit_is_recorded_as_failed_despite_zero_failures(tmp_path) -> None:
    """The nastiest shape: exit 124 is deliberately NOT counted as a failure.

    Before the derived outcome existed, such a row carried `failures: 0` and
    was indistinguishable from a clean run — the ledger's only failure signal
    said everything was fine while the worker had timed out.
    """
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=124)

    rc = run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")

    assert rc == 124
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    # Item accounting still says "no failures" (worker.sh:100-104 parity) ...
    assert completed["failures"] == 0
    # ... but the exit status is recorded and the outcome derived from it.
    assert completed["exit_code"] == 124
    assert completed["outcome"] == "failed"


def test_clean_run_records_verified_success(tmp_path) -> None:
    (tmp_path / "monitoring-rules.md").write_text("RULES CONTENT")
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()

    rc = run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")

    assert rc == 0
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["exit_code"] == 0
    assert completed["outcome"] == "succeeded"


def _effect_hooks(run_cmd: FakeRunCmd, *, head_after: str) -> RunItemHooks:
    """Hooks that write a real record and report a given post-session PR head."""
    return make_hooks(
        run_cmd=run_cmd,
        make_record=lambda **kw: dict(kw),
        fetch_pr_snapshot=lambda repo, num: {
            "state": "OPEN",
            "headRefOid": head_after,
            "mergeCommit": "",
        },
        merge_lifecycle_io=FakeLifecycleIO(),
    )


def test_clean_exit_that_pushed_nothing_is_not_recorded_as_success(tmp_path) -> None:
    """THE #3468 symptom test: worker exits 0, PR head never moves.

    The worker read the findings, made the fix, committed it — and every
    `git push` was rejected by a pre-push guard. Exit 0, log says success,
    nothing reached GitHub. This must not record as a success.
    """
    (tmp_path / "monitoring-rules.md").write_text("RULES CONTENT")
    work_file = _write_work_file(tmp_path, make_item(types=["pr_update"]))
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("rev-parse", stdout="abc123\n")
    # Pre-session snapshot: head is deadbeef ...
    run_cmd.on(
        "gh", stdout='{"state": "OPEN", "headRefOid": "deadbeef", "mergeCommit": null}'
    )
    # ... and after the session it is STILL deadbeef: the push was rejected.
    hooks = _effect_hooks(run_cmd, head_after="deadbeef")

    rc = run_work_file(work_file, config, hooks, backend="claude-code", lane="slow")

    assert rc == 0  # the worker itself was perfectly happy
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["exit_code"] == 0
    assert completed["failures"] == 0
    # ... and yet nothing shipped:
    assert completed["effect"] == "none"
    assert completed["outcome"] == "no_effect"
    assert completed["outcome"] != "succeeded"


def test_push_that_landed_records_observed_effect(tmp_path) -> None:
    (tmp_path / "monitoring-rules.md").write_text("RULES CONTENT")
    work_file = _write_work_file(tmp_path, make_item(types=["pr_update"]))
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("rev-parse", stdout="abc123\n")
    run_cmd.on(
        "gh", stdout='{"state": "OPEN", "headRefOid": "beforeaa", "mergeCommit": null}'
    )
    hooks = _effect_hooks(run_cmd, head_after="after0bb")

    rc = run_work_file(work_file, config, hooks, backend="claude-code", lane="slow")

    assert rc == 0
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    assert completed["effect"] == "observed"
    assert completed["outcome"] == "succeeded"


def test_non_terminal_phases_never_carry_an_outcome(tmp_path) -> None:
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("/fake/run.sh", returncode=1)

    run_work_file(work_file, config, make_hooks(run_cmd=run_cmd), backend="codex")

    for row in _ledger_rows(config):
        if row["phase"] != "completed":
            assert row["outcome"] is None, row


def test_run_work_file_self_merge_skips_session(tmp_path) -> None:
    item = make_item(types=["merge_ready"])
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    io = FakeLifecycleIO(check=SelfMergeCheckResult(eligible=True), merge_ok=True)
    hooks = make_hooks(run_cmd=run_cmd, merge_lifecycle_io=io)
    rc = run_work_file(work_file, config, hooks, backend="claude-code")
    assert rc == 0
    assert io.merged == [("gptme/gptme-contrib", 1234)]
    assert run_cmd.find("/fake/run.sh") == []


def test_run_work_file_promotes_notification_states(tmp_path) -> None:
    config = make_config(tmp_path)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-9999.state").write_text("seen")
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    run_work_file(work_file, config, make_hooks(), backend="claude-code")
    assert (config.state_dir / "notif-9999.state").read_text() == "seen"


def test_run_work_file_post_run_hook_env(tmp_path) -> None:
    item = make_item(types=["notification"], number=0)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("rev-parse", stdout="abc123\n")
    hooks = make_hooks(run_cmd=run_cmd, post_run=["/fake/post-run.sh"])
    run_work_file(work_file, config, hooks, backend="claude-code", lane="fast")
    post_calls = run_cmd.find("/fake/post-run.sh")
    assert len(post_calls) == 1
    env = post_calls[0]["env"]
    assert env["PM_ITEM_COUNT"] == "1"
    assert env["PM_ITEM_SUCCESSES"] == "1"
    assert env["PM_START_COMMIT"] == "abc123"
    assert env["PM_LANE"] == "fast"


# --- Rate limit handling (worker.sh:107-189) ---


def _cc_log(tmp_path: Path, session_id: str, lines: list[dict]) -> Path:
    log = tmp_path / "stream.jsonl"
    log.write_text("".join(json.dumps(entry) + "\n" for entry in lines))
    ref = Path("/tmp") / f"cc-session-log-ref-{session_id}.txt"
    ref.write_text(str(log))
    return log


def _fake_plan(tmp_path, session_id="test-rl-session"):
    return SimpleNamespace(
        session_id=session_id,
        backend="claude-code",
        index=1,
        timeout=900,
        time_desc="~10 minutes",
    )


def test_cc_rate_limit_confirmed_rejection_blocks(tmp_path) -> None:
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-a"
    log = _cc_log(
        tmp_path,
        sid,
        [
            {"type": "message"},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "seven_day",
                    "resetsAt": 1760000000,
                },
            },
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    assert _handle_cc_rate_limit(plan, config) is True
    block = config.resolved_backend_quota_dir / "claude-code-rate-limited-until.txt"
    assert block.is_file()
    assert not log.exists()  # log + ref removed, like the bash
    assert not (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").exists()


def test_cc_rate_limit_bare_mention_does_not_block(tmp_path) -> None:
    """The 2026-06-14 401-misclassification guard: rateLimitType present but
    no REJECTED event must never write a block file."""
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-b"
    _cc_log(
        tmp_path,
        sid,
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day"},
            }
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    try:
        assert _handle_cc_rate_limit(plan, config) is False
        assert not config.resolved_backend_quota_dir.exists()
    finally:
        (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").unlink(missing_ok=True)


def test_cc_auth_expiry_is_infra_not_rate_limit(tmp_path) -> None:
    """gptme/gptme#3531 (2026-08-17): 'OAuth session expired and could not be
    refreshed' killed three dispatches in 50 min. It must classify as an
    infra failure (free re-arm) WITHOUT writing a rate-limit block file."""
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-c"
    _cc_log(
        tmp_path,
        sid,
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
            },
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    try:
        assert _inspect_cc_failure(plan, config) == (False, "cc_auth")
        assert not config.resolved_backend_quota_dir.exists()
    finally:
        (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").unlink(missing_ok=True)


def test_cc_auth_marker_mid_transcript_is_not_infra(tmp_path) -> None:
    """A tool output mentioning 'Failed to authenticate' (git 401, some cache
    that 'could not be refreshed') followed by an ordinary failure must NOT be
    relabeled cc_auth — that would hand a genuinely broken task a free re-arm."""
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-f"
    _cc_log(
        tmp_path,
        sid,
        [
            {
                "type": "user",
                "message": {"content": "remote: Failed to authenticate (git 401)"},
            },
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
            },
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "result": "Reached max turns",
            },
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    try:
        assert _inspect_cc_failure(plan, config) == (False, None)
    finally:
        (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").unlink(missing_ok=True)


def test_cc_auth_expiry_without_rate_limit_field_is_still_infra(tmp_path) -> None:
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-d"
    _cc_log(
        tmp_path,
        sid,
        [{"type": "result", "is_error": True, "result": "Failed to authenticate"}],
    )
    plan = _fake_plan(tmp_path, sid)
    try:
        assert _inspect_cc_failure(plan, config) == (False, "cc_auth")
    finally:
        (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").unlink(missing_ok=True)


def test_cc_auth_marker_in_truncated_log_without_result_is_not_infra(tmp_path) -> None:
    """Hard-killed log, no result event, last tool output mentions the marker:
    conservatively NOT infra."""
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-g"
    _cc_log(
        tmp_path,
        sid,
        [
            {"type": "assistant", "message": {"content": "running git fetch"}},
            {"type": "user", "message": {"content": "fatal: Failed to authenticate"}},
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    try:
        assert _inspect_cc_failure(plan, config) == (False, None)
    finally:
        (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").unlink(missing_ok=True)


def test_cc_confirmed_rejection_reports_rate_limit_kind(tmp_path) -> None:
    config = make_config(tmp_path)
    sid = f"test-rl-{os.getpid()}-e"
    _cc_log(
        tmp_path,
        sid,
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1760000000,
                },
            }
        ],
    )
    plan = _fake_plan(tmp_path, sid)
    assert _inspect_cc_failure(plan, config) == (True, "cc_rate_limit")


def test_run_work_file_rate_limit_breaks_remaining_items(tmp_path) -> None:
    item1 = make_item(types=["notification"], number=0, title="one")
    item2 = make_item(types=["notification"], number=0, title="two")
    work_file = _write_work_file(tmp_path, item1, item2)
    config = make_config(tmp_path)
    sid_holder: dict[str, str] = {}

    class RateLimitedRunCmd(FakeRunCmd):
        def __call__(self, argv, **kwargs):
            argv_s = [str(a) for a in argv]
            if argv_s[0] == "/fake/run.sh":
                env = kwargs.get("env") or {}
                sid = env.get("CC_SESSION_ID", "")
                sid_holder.setdefault("sid", sid)
                log = tmp_path / f"stream-{sid}.jsonl"
                log.write_text(
                    json.dumps(
                        {
                            "type": "rate_limit_event",
                            "rate_limit_info": {
                                "status": "rejected",
                                "rateLimitType": "seven_day",
                                "resetsAt": 0,
                            },
                        }
                    )
                    + "\n"
                )
                (Path("/tmp") / f"cc-session-log-ref-{sid}.txt").write_text(str(log))
                self.calls.append({"argv": argv_s, **kwargs})
                return subprocess.CompletedProcess(argv_s, 1, "", "")
            return super().__call__(argv, **kwargs)

    run_cmd = RateLimitedRunCmd()
    rc = run_work_file(
        work_file, config, make_hooks(run_cmd=run_cmd), backend="claude-code"
    )
    assert rc == 1
    # Only ONE runner call — the second item was skipped after the rejection
    assert len(run_cmd.find("/fake/run.sh")) == 1
    block = config.resolved_backend_quota_dir / "claude-code-rate-limited-until.txt"
    assert block.is_file()


# --- Trajectory resolution (worker.sh:196-300) ---


def test_trajectory_cc_stream_log_over_floor(tmp_path) -> None:
    sid = f"test-traj-{os.getpid()}"
    log = tmp_path / "stream.jsonl"
    log.write_text("x" * 6000)
    ref = Path("/tmp") / f"cc-session-log-ref-{sid}.txt"
    ref.write_text(str(log))
    try:
        got = resolve_backend_trajectory(
            "claude-code",
            sid,
            predicted="/predicted/stub.jsonl",
            started_epoch=0,
            copilot_state_dir=tmp_path,
            codex_sessions_dir=tmp_path,
            copilot_pre=None,
            codex_pre=None,
        )
        assert got == str(log)
    finally:
        ref.unlink(missing_ok=True)


def test_trajectory_cc_under_floor_keeps_predicted(tmp_path) -> None:
    sid = f"test-traj-small-{os.getpid()}"
    log = tmp_path / "stream.jsonl"
    log.write_text("x" * 100)
    ref = Path("/tmp") / f"cc-session-log-ref-{sid}.txt"
    ref.write_text(str(log))
    try:
        got = resolve_backend_trajectory(
            "claude-code",
            sid,
            predicted="/predicted/stub.jsonl",
            started_epoch=0,
            copilot_state_dir=tmp_path,
            codex_sessions_dir=tmp_path,
            copilot_pre=None,
            codex_pre=None,
        )
        assert got == "/predicted/stub.jsonl"
    finally:
        ref.unlink(missing_ok=True)


def test_trajectory_copilot_snapshot_diff(tmp_path) -> None:
    state = tmp_path / "copilot-state"
    (state / "old-uuid").mkdir(parents=True)
    (state / "old-uuid" / "events.jsonl").write_text("old")
    pre = snapshot_copilot_dirs(state)
    (state / "new-uuid").mkdir()
    new_events = state / "new-uuid" / "events.jsonl"
    new_events.write_text("new session events")
    got = resolve_backend_trajectory(
        "copilot-cli",
        "sid",
        predicted="",
        started_epoch=0,
        copilot_state_dir=state,
        codex_sessions_dir=tmp_path,
        copilot_pre=pre,
        codex_pre=None,
    )
    assert got == str(new_events)


def test_trajectory_copilot_mtime_filter(tmp_path) -> None:
    state = tmp_path / "copilot-state"
    state.mkdir()
    pre = snapshot_copilot_dirs(state)
    (state / "new-uuid").mkdir()
    stale = state / "new-uuid" / "events.jsonl"
    stale.write_text("stale")
    os.utime(stale, (1000, 1000))  # long before started_epoch
    got = resolve_backend_trajectory(
        "copilot-cli",
        "sid",
        predicted="",
        started_epoch=2_000_000_000,
        copilot_state_dir=state,
        codex_sessions_dir=tmp_path,
        copilot_pre=pre,
        codex_pre=None,
    )
    assert got == ""


def test_trajectory_gptme_uses_run_sh_sentinel(tmp_path) -> None:
    trajectory = tmp_path / "gptme-conversation.jsonl"
    trajectory.write_text('{"role": "assistant"}\n')
    (tmp_path / "gptme-traj-session-123.path").write_text(str(trajectory) + "\n")

    got = resolve_backend_trajectory(
        "gptme",
        "session-123",
        predicted="",
        started_epoch=0,
        copilot_state_dir=tmp_path,
        codex_sessions_dir=tmp_path,
        copilot_pre=None,
        codex_pre=None,
        tmp_dir=tmp_path,
    )

    assert got == str(trajectory)


def test_trajectory_gptme_ignores_missing_sentinel_target(tmp_path) -> None:
    (tmp_path / "gptme-traj-session-123.path").write_text(
        str(tmp_path / "missing.jsonl") + "\n"
    )

    got = resolve_backend_trajectory(
        "gptme",
        "session-123",
        predicted="",
        started_epoch=0,
        copilot_state_dir=tmp_path,
        codex_sessions_dir=tmp_path,
        copilot_pre=None,
        codex_pre=None,
        tmp_dir=tmp_path,
    )

    assert got == ""


def test_trajectory_codex_snapshot_diff_newest_wins(tmp_path) -> None:
    sessions = tmp_path / "codex-sessions" / "2026" / "07" / "11"
    sessions.mkdir(parents=True)
    old = sessions / "rollout-old.jsonl"
    old.write_text("old")
    pre = snapshot_codex_rollouts(tmp_path / "codex-sessions")
    a = sessions / "rollout-a.jsonl"
    a.write_text("a")
    os.utime(a, (2000, 2000))
    b = sessions / "rollout-b.jsonl"
    b.write_text("b")
    os.utime(b, (3000, 3000))
    got = resolve_backend_trajectory(
        "codex",
        "sid",
        predicted="",
        started_epoch=0,
        copilot_state_dir=tmp_path,
        codex_sessions_dir=tmp_path / "codex-sessions",
        copilot_pre=None,
        codex_pre=pre,
    )
    assert got == str(b)


# --- Post-session composition (worker.sh order, fake collaborators) ---


def _post_session_fixture(tmp_path, *, exit_code=0, types=("pr_update",)):
    config = make_config(tmp_path)
    config.resolved("records_dir", "records").mkdir(parents=True, exist_ok=True)
    item = make_item(types=list(types))
    run_cmd = FakeRunCmd()

    def fake_post_session(**kwargs):
        return SimpleNamespace(
            record=SimpleNamespace(
                to_dict=lambda: {
                    "harness": kwargs["harness"],
                    "model": kwargs["model"],
                    "run_type": "monitoring",
                    "category": "pm-react",
                    "outcome": "unknown",
                    "session_id": kwargs["session_id"],
                    "duration_seconds": kwargs["duration_seconds"],
                    "deliverables": [],
                }
            ),
            grade=None,
        )

    def fake_build_worker_result(**kwargs):
        return {
            "status": "completed",
            "schema_version": 1,
            "git_refs": {},
            "task": {"intended_category": kwargs.get("intended_category")},
            "artifact_paths": {"draft_path": "x"},
        }

    latency_calls: list[dict] = []

    hooks = RunItemHooks(
        runner=["/fake/run.sh"],
        run_cmd=run_cmd,
        post_session=fake_post_session,
        make_store=lambda d: object(),
        make_record=lambda **kw: dict(kw),
        build_worker_result=fake_build_worker_result,
        write_worker_result=lambda path, manifest: Path(path).write_text(
            json.dumps(manifest)
        ),
        load_worker_result=lambda path: json.loads(Path(path).read_text()),
        append_latency_records=lambda **kw: latency_calls.append(kw),
        fetch_pr_snapshot=lambda repo, num: {
            "state": "MERGED",
            "headRefOid": "bb" * 20,
            "mergeCommit": "cc" * 20,
        },
        delivery_check=["/fake/check-delivery.py"],
        wait_merge_gate=["/fake/gate.py"],
        wait_merge_helper=["/fake/wait-merge.sh"],
        arc_manager=["/fake/arc.py"],
    )
    lifecycle = LifecycleResult()
    plan = plan_item(
        item,
        index=1,
        config=config,
        backend="claude-code",
        model="claude-sonnet-4-6",
        monitoring_rules="RULES",
        lifecycle=lifecycle,
        arc=ArcInfo(arc_id="arc-1", hint="h", sessions=2),
        run_salt=1,
        records_dir=config.records_dir,
        runner=hooks.runner,
        sysprompt_file="",
    )
    from gptme_runloops.run_item import RunItemOutcome

    outcome = RunItemOutcome(
        exit_code=exit_code,
        duration_seconds=42,
        started_epoch=1720000000,
        started_iso="2026-07-11T10:00:00+00:00",
        trajectory_path="",
        pr_before_json='{"state": "OPEN", "headRefOid": "aa", "mergeCommit": null}',
        latency_context_json="[]",
        ack_result_json="",
    )
    return config, item, plan, outcome, hooks, run_cmd, latency_calls


def test_post_session_happy_path_composition(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path
    )
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')
    run_cmd.on("/fake/gate.py", returncode=0, stdout='{"decision": "go"}')
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "gptme-gptme-contrib-pr-1234-update.state").write_text(
        "s"
    )

    run_post_session(plan, item, outcome, config, hooks)

    record = json.loads(Path(plan.record_file).read_text())
    assert record["harness"] == "claude-code"
    assert record["timeout_seconds"] == plan.timeout
    # PR-state diff folded in (fetch fake says MERGED, head advanced)
    assert record["pr_state_after"] == "MERGED"
    assert record["pr_head_oid_before"] == "aa"
    # Worker-result manifest written + reflected
    assert record["worker_status"] == "completed"
    assert Path(record["worker_result_path"]).is_file()
    # Latency append got the pass-through outcome
    assert latency_calls[0]["outcome"] == "handled"
    assert latency_calls[0]["session_id"] == plan.session_id
    # Wait-merge helper ran with the policy env
    helper_calls = run_cmd.find("/fake/wait-merge.sh")
    assert len(helper_calls) == 1
    assert helper_calls[0]["env"]["WORKSPACE_REPO"] == "ErikBjare/bob"
    assert helper_calls[0]["env"]["PR_ADDRESS_TRIGGER"] == "auto-monitoring"
    # Gate log entry appended
    gate_rows = [
        json.loads(line)
        for line in config.resolved_wait_merge_gate_log.read_text().splitlines()
    ]
    assert gate_rows[0]["pr_number"] == 1234
    assert gate_rows[0]["gate_exit_code"] == 0
    # Arc updated and auto-closed (record says MERGED)
    arc_calls = [c["argv"] for c in run_cmd.find("/fake/arc.py")]
    assert arc_calls[0][1] == "update"
    assert arc_calls[-1][1:] == ["close", "arc-1"]
    # State promoted
    assert (config.state_dir / "gptme-gptme-contrib-pr-1234-update.state").is_file()


def test_post_session_fallback_on_post_session_failure(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, exit_code=3, types=("notification",)
    )

    def broken_post_session(**kwargs):
        raise RuntimeError("store exploded")

    hooks.post_session = broken_post_session
    hooks.legacy_record_append = ["/fake/session-records.py"]
    run_post_session(plan, item, outcome, config, hooks)

    # Legacy subprocess appender invoked with the fallback outcome
    legacy = run_cmd.find("/fake/session-records.py")
    assert len(legacy) == 1
    argv = legacy[0]["argv"]
    assert argv[argv.index("--outcome") + 1] == "failed"  # exit 3 → failed
    # Fallback record written via make_record
    record = json.loads(Path(plan.record_file).read_text())
    assert record["outcome"] == "failed"
    assert record["exit_code"] == 3
    assert record["session_id"] == plan.session_id


def test_post_session_timeout_records_unknown(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, exit_code=124, types=("notification",)
    )
    hooks.post_session = None  # force the fallback path
    run_post_session(plan, item, outcome, config, hooks)
    record = json.loads(Path(plan.record_file).read_text())
    # NOTE(parity): timeout (124) records as "unknown", not failed
    assert record["outcome"] == "unknown"


def test_post_session_orphan_delivery_latency_outcome(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path
    )
    run_cmd.on(
        "/fake/check-delivery.py",
        stdout='{"outcome": "orphan_no_delivery", "needs_fallback_reply": true, '
        '"fallback_reply_posted": false}',
    )
    run_cmd.on("/fake/gate.py", returncode=1)
    run_post_session(plan, item, outcome, config, hooks)
    assert latency_calls[0]["outcome"] == "orphan_no_delivery"


def test_post_session_orphan_delivery_rolls_back_instead_of_promoting(
    tmp_path, cooldown_dir
) -> None:
    """A session that delivered no reply must not consume the item's state.

    Regression: the runloops port of worker.sh:661-676 promoted unconditionally,
    so a failed delivery advanced the activity-gate cooldown AND left the
    dispatcher's launch-stamped `.event` fingerprint in place — suppressing the
    item for the 6h event-unchanged TTL with nothing ever having replied.
    """
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on(
        "/fake/check-delivery.py",
        stdout='{"outcome": "orphan_no_delivery", "needs_fallback_reply": true, '
        '"fallback_reply_posted": false}',
    )
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "gptme-gptme-contrib-pr-1234-update.state").write_text(
        "s"
    )
    (cooldown_dir / "gptme-gptme-contrib-1234.event").write_text("fingerprint")

    run_post_session(plan, item, outcome, config, hooks)

    assert not (config.state_dir / "gptme-gptme-contrib-pr-1234-update.state").exists()
    assert not (cooldown_dir / "gptme-gptme-contrib-1234.event").exists()


def test_post_session_timed_out_worker_does_not_consume_notification_state(
    tmp_path, cooldown_dir
) -> None:
    """A worker killed at its time budget (exit 124) handled nothing; promoting
    the item's notif state would make the gate treat the mention as done
    (ActivityWatch/activitywatch#1402, 2026-08-20). PR-side state still
    promotes; the notification thread must stay re-emittable even after the
    end-of-run blanket promotion."""
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, exit_code=124
    )
    outcome.timed_out = True
    # A killed worker has no verified delivery (the check cannot attribute a reply).
    run_cmd.on("/fake/check-delivery.py", returncode=1, stdout="")
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "gptme-gptme-contrib-pr-1234-update.state").write_text(
        "s"
    )
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("2026-08-20T09:31:35Z")
    (config.pending_state_dir / "notif-777.map").write_text("gptme/gptme-contrib#9999")
    (config.pending_state_dir / "notif-777.state").write_text("other-item")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert (config.state_dir / "gptme-gptme-contrib-pr-1234-update.state").exists()
    assert not (config.state_dir / "notif-555.state").exists()
    assert not (config.pending_state_dir / "notif-555.state").exists()
    # a sibling's emitted-but-unhandled thread is left alone, not promoted
    assert not (config.state_dir / "notif-777.state").exists()
    assert (config.pending_state_dir / "notif-777.state").exists()


def test_post_session_clean_exit_with_unverified_delivery_does_not_consume_notification_state(
    tmp_path, cooldown_dir
) -> None:
    """A clean worker exit cannot compensate for a broken delivery check."""
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on("/fake/check-delivery.py", returncode=1, stdout="")
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("t")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert not (config.state_dir / "notif-555.state").exists()
    assert not (config.pending_state_dir / "notif-555.state").exists()


def test_post_session_unverified_delivery_honors_redelivery_cap(
    tmp_path, cooldown_dir, monkeypatch
) -> None:
    """A persistently broken delivery check must not re-emit forever."""
    monkeypatch.delenv("PM_SLOT_KEY", raising=False)
    monkeypatch.setenv("PM_MAX_REDELIVERY_ATTEMPTS", "1")
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on("/fake/check-delivery.py", returncode=1, stdout="")
    run_cmd.on("/fake/gate.py", returncode=1)

    def stage_notification() -> None:
        config.pending_state_dir.mkdir(parents=True, exist_ok=True)
        (config.pending_state_dir / "notif-555.map").write_text(
            "gptme/gptme-contrib#1234"
        )
        (config.pending_state_dir / "notif-555.state").write_text("t")

    stage_notification()
    run_post_session(plan, item, outcome, config, hooks)
    attempts = redelivery_attempts_file(config, item.repo, item.number)
    assert attempts is not None
    assert attempts.read_text() == "1"
    assert not (config.state_dir / "notif-555.state").exists()

    stage_notification()
    run_post_session(plan, item, outcome, config, hooks)
    assert not attempts.exists()
    assert (config.state_dir / "notif-555.state").exists()


def test_post_session_failed_exit_with_verified_delivery_still_promotes(
    tmp_path, cooldown_dir
) -> None:
    """A worker that posted its reply and then died non-zero DID handle the
    thread; purging here would re-emit it and post a duplicate reply."""
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, exit_code=1
    )
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("t")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert (config.state_dir / "notif-555.state").exists()


def test_post_session_clean_exit_promotes_mapped_notification_state(
    tmp_path, cooldown_dir
) -> None:
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("t")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert (config.state_dir / "notif-555.state").exists()


def test_post_session_orphan_delivery_promotes_after_redelivery_cap(
    tmp_path, cooldown_dir, monkeypatch
) -> None:
    """Past the cap the item promotes, so it stops burning a slot every cycle."""
    monkeypatch.setenv("PM_MAX_REDELIVERY_ATTEMPTS", "0")
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on(
        "/fake/check-delivery.py",
        stdout='{"outcome": "orphan_no_delivery", "needs_fallback_reply": true, '
        '"fallback_reply_posted": false}',
    )
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "gptme-gptme-contrib-pr-1234-update.state").write_text(
        "s"
    )

    run_post_session(plan, item, outcome, config, hooks)

    assert (config.state_dir / "gptme-gptme-contrib-pr-1234-update.state").is_file()


def test_post_session_failed_exit_maps_latency_failed(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path, exit_code=1
    )
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')
    run_post_session(plan, item, outcome, config, hooks)
    assert latency_calls[0]["outcome"] == "failed"
    # Wait-merge gate must NOT run for a failed session (exit != 0)
    assert run_cmd.find("/fake/gate.py") == []


def test_post_session_missing_delivery_hook_skips_check(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path, types=("notification",)
    )
    hooks.delivery_check = None
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    run_post_session(plan, item, outcome, config, hooks)
    # Delivery defaults to handled when the script is absent (bash [ -f ] guard)
    assert latency_calls[0]["outcome"] == "handled"


def test_post_session_merge_ready_only_reply_is_not_effect(tmp_path) -> None:
    """A pure merge_ready dispatch that only posts a comment must NOT grade
    effect=observed (gptme/gptme#3531: comment != merge). With the PR snapshot
    unchanged before/after and delivery=handled, the effect is `none`."""
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("merge_ready",)
    )
    item = make_item(types=["merge_ready"])
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # PR did not move: after-snapshot identical to pr_before_json.
    hooks.fetch_pr_snapshot = lambda repo, num: {
        "state": "OPEN",
        "headRefOid": "aa",
        "mergeCommit": None,
    }
    # Worker posted a reply — the delivery check verifies it as handled.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert effect == "none", (
        f"merge_ready-only + comment-only must grade effect='none', not {effect!r}; "
        "'observed' is exactly the grade that made the #3531 reply loop look healthy"
    )


def test_post_session_merge_ready_only_actual_merge_is_observed(tmp_path) -> None:
    """The same pure merge_ready item DOES grade observed when the PR merged."""
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("merge_ready",)
    )
    item = make_item(types=["merge_ready"])
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # Default fixture snapshot: state MERGED, new head, merge commit present.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert effect == "observed"


def test_post_session_mixed_merge_ready_keeps_delivery_signal(tmp_path) -> None:
    """ci_failure+merge_ready: a verified reply still counts as effect — the
    reply can be the legitimate deliverable of the non-merge_ready type."""
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("ci_failure", "merge_ready")
    )
    item = make_item(types=["ci_failure", "merge_ready"])
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    hooks.fetch_pr_snapshot = lambda repo, num: {
        "state": "OPEN",
        "headRefOid": "aa",
        "mergeCommit": None,
    }
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert effect == "observed"


def test_post_session_merge_ready_only_merge_without_reply_promotes(tmp_path) -> None:
    """A pure merge_ready session that merged the PR but posted no reply must
    grade observed AND promote state — not roll back into the dispatch queue
    (an already-settled PR must not re-enter and produce another comment)."""
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("merge_ready",)
    )
    item = make_item(types=["merge_ready"])
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # Default fixture snapshot: MERGED, new head, merge commit — the merge landed.
    # Session posted nothing, so the delivery check reports an orphan.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "orphan_no_delivery"}')
    config.pending_state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = config.pending_state_dir / "gptme-gptme-contrib-pr-1234.state"
    sentinel.write_text("promoted")

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert effect == "observed"
    assert (
        config.state_dir / sentinel.name
    ).exists(), "state must be promoted — rollback would re-queue an already-merged PR"


def test_post_session_failed_exit_without_delivery_hook_preserves_notification(
    tmp_path,
) -> None:
    """Without an observation hook, failure cannot prove no reply was posted."""
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, _, _ = _post_session_fixture(
        tmp_path, exit_code=1, types=("notification",)
    )
    hooks.delivery_check = None
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("t")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert (config.state_dir / "notif-555.state").exists()


def test_post_session_malformed_delivery_output_is_not_verified(
    tmp_path, cooldown_dir
) -> None:
    """Exit zero alone is not verification when the output cannot be parsed."""
    from gptme_runloops.run_item import promote_notification_states

    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, exit_code=1
    )
    run_cmd.on("/fake/check-delivery.py", stdout="not json")
    run_cmd.on("/fake/gate.py", returncode=1)
    config.pending_state_dir.mkdir(parents=True)
    (config.pending_state_dir / "notif-555.map").write_text("gptme/gptme-contrib#1234")
    (config.pending_state_dir / "notif-555.state").write_text("t")

    run_post_session(plan, item, outcome, config, hooks)
    promote_notification_states(config)

    assert not (config.state_dir / "notif-555.state").exists()
    assert not (config.pending_state_dir / "notif-555.state").exists()


def test_post_session_repo_level_item_skips_delivery_check(tmp_path) -> None:
    """master_ci_failure has no thread, so the reply post-condition must not run.

    Its `number` is a workflow run id, not an issue number, so the check would
    query issues/{run_id}/comments, 404, find no comment, and return
    orphan_no_delivery -> rollback -> re-dispatch forever (ErikBjare/bob#1144).

    The fix must also promote state (not roll back): verify the pending state
    file is copied to the real state dir so the item doesn't re-enter the queue.
    """
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path, types=("master_ci_failure",)
    )
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # Seed a pending state file so promote_item_state has something observable to do.
    config.pending_state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = config.pending_state_dir / "gptme-gptme-contrib-master-ci.state"
    sentinel.write_text("promoted")
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "orphan_no_delivery"}')
    effect = run_post_session(plan, item, outcome, config, hooks)
    assert run_cmd.find("/fake/check-delivery.py") == []
    assert latency_calls[0]["outcome"] == "handled"
    # State must be promoted, not rolled back — otherwise the item re-enters the queue.
    assert (config.state_dir / sentinel.name).exists(), (
        "promote_item_state was not called — state was not promoted from pending; "
        "item would re-enter the dispatch queue"
    )
    # For non-thread items the delivery check never runs, so no delivery signal
    # is observed and the record has no PR state diff. EFFECT_UNKNOWN is the
    # correct and expected outcome — not EFFECT_NONE (which would fire the
    # "no observable effect" WARN and implies we *know* nothing happened).
    assert effect == "unknown", (
        f"master_ci_failure must score effect='unknown', not {effect!r}; "
        "EFFECT_NONE would incorrectly trigger the no-effect WARN for a "
        "repo-level item that is expected to do nothing thread-observable."
    )


def test_post_session_agent_msg_reply_skips_delivery_check(tmp_path) -> None:
    """An `agent_msg_reply` item has no GitHub thread at all — never check delivery.

    Second symptom class of the same missing gate (the first is
    master_ci_failure above). Bob's PM ledger, 2026-08-13: every `number: 0`
    dispatch row was an `agent_msg_reply` (peer-agent message; the number is
    synthesized because there is no issue). The unguarded check ran
    `check-pm-delivery.py --number 0` against a nonexistent issue, which always
    reports `orphan_no_delivery` -> `effect=none` -> `outcome=no_effect`. That
    drove `pm_dispatch_recovery.py` to exhaust the retry budget and file a bogus
    "PM cannot drive ErikBjare/bob#0" stuck task — while the reply had in fact
    been delivered.

    Asserts on the returned effect (not just that the check was skipped), so a
    regression that skips the check but still scores the item no-effect fails here.
    """
    config, item, plan, outcome, hooks, run_cmd, _latency = _post_session_fixture(
        tmp_path, types=("agent_msg_reply",)
    )
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # If the check DID run it would report an orphan, as it did in production.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "orphan_no_delivery"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert (
        run_cmd.find("/fake/check-delivery.py") == []
    ), "delivery check must not run for item types with no GitHub thread"
    # Skipping the delivery check means no delivery signal is verified.  The
    # record also carries no PR state diff (number=0 is not a real issue).
    # EFFECT_UNKNOWN is correct: we have no mechanism to observe whether
    # anything happened, so we must not claim EFFECT_NONE ("nothing happened").
    assert effect == "unknown", (
        f"agent_msg_reply must score effect='unknown', not {effect!r}; "
        "EFFECT_NONE would incorrectly fire the no-effect WARN and mark the "
        "item as a delivery failure when the reply may have been delivered."
    )


def test_post_session_notification_zero_number_skips_delivery_check(tmp_path) -> None:
    """A notification item with number=0 must not trigger the delivery check.

    `notification` items in THREAD_DELIVERABLE_TYPES normally have real issue
    numbers. But synthetic notifications (e.g. agent-bus pings) carry number=0
    as a sentinel — there is no GitHub thread to check. Querying
    `issues/0/comments` 404s, returns `orphan_no_delivery`, and triggers
    rollback → re-dispatch forever, the same loop fixed for master_ci_failure
    and agent_msg_reply. The number!=0 gate closes this third symptom class.
    """
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("notification",)
    )
    # Override the default number=1234 with the sentinel value
    item = make_item(types=["notification"], number=0)
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # If the check DID run it would report an orphan, as it does in production.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "orphan_no_delivery"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert (
        run_cmd.find("/fake/check-delivery.py") == []
    ), "delivery check must not run for notification items with number=0"
    assert effect == "unknown", (
        f"notification with number=0 must score effect='unknown', not {effect!r}; "
        "EFFECT_NONE would incorrectly fire the no-effect WARN."
    )


def test_post_session_string_zero_number_skips_delivery_check(tmp_path) -> None:
    """A grouped item carrying ``"number": "0"`` as a *string* must skip too.

    ``RunItem.number`` is typed ``int | str | None`` and
    ``from_grouped_json`` takes the JSON value verbatim — no coercion. A
    sentinel that arrives as the string ``"0"`` is therefore just as real as
    the int ``0``, and an identity-shaped guard (``number != 0``) lets it
    through: the delivery check queries ``issues/0/comments``, 404s, reports
    ``orphan_no_delivery``, and drives the rollback → re-dispatch loop this
    gate exists to eliminate.
    """
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(
        tmp_path, types=("notification",)
    )
    # Built through `from_grouped_json`, so `number` really is the str "0".
    item = make_item(types=["notification"], number="0")
    assert item.number == "0", "fixture must exercise the str sentinel, not the int"
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    # If the check DID run it would report an orphan, as it does in production.
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "orphan_no_delivery"}')

    effect = run_post_session(plan, item, outcome, config, hooks)

    assert (
        run_cmd.find("/fake/check-delivery.py") == []
    ), 'delivery check must not run for items with number="0" (string sentinel)'
    assert effect == "unknown", (
        f"notification with number=\"0\" must score effect='unknown', not {effect!r}; "
        "EFFECT_NONE would incorrectly fire the no-effect WARN."
    )


@pytest.mark.parametrize("item_type", sorted(THREAD_DELIVERABLE_TYPES))
def test_post_session_thread_deliverable_item_still_runs_delivery_check(
    tmp_path,
    item_type: str,
) -> None:
    """Regression guard: the delivery check must run for every thread-deliverable type.

    THREAD_DELIVERABLE_TYPES lists all eight thread-bearing item types. If any of
    them is accidentally removed from the set, the delivery post-condition would
    silently stop running for those items. This parametrized test ensures each type
    in the set actually triggers the check — a future accidental removal will
    fail exactly here.

    Exception by design: a PURE merge_ready item skips the check (its
    deliverable is the merge, not a comment — see the step-4 gate and
    test_post_session_merge_ready_only_reply_is_not_effect). So for
    merge_ready this guard exercises the mixed-item path instead, proving
    membership in the set still matters when combined with another type.
    """
    types = (item_type,) if item_type != "merge_ready" else ("merge_ready", "pr_update")
    config, item, plan, outcome, hooks, run_cmd, latency_calls = _post_session_fixture(
        tmp_path, types=types
    )
    item = make_item(types=list(types))
    hooks.wait_merge_gate = None
    hooks.arc_manager = None
    run_cmd.on(
        "/fake/check-delivery.py",
        stdout='{"outcome": "orphan_no_delivery", "needs_fallback_reply": true, '
        '"fallback_reply_posted": false}',
    )
    run_post_session(plan, item, outcome, config, hooks)
    assert run_cmd.find("/fake/check-delivery.py") != [], (
        f"delivery check was NOT called for thread-deliverable type {item_type!r} "
        f"— was it accidentally removed from THREAD_DELIVERABLE_TYPES?"
    )
    assert latency_calls[0]["outcome"] == "orphan_no_delivery"


def test_post_session_gate_exit_2_warns_no_helper(tmp_path) -> None:
    config, item, plan, outcome, hooks, run_cmd, _ = _post_session_fixture(tmp_path)
    run_cmd.on("/fake/check-delivery.py", stdout='{"outcome": "handled"}')
    run_cmd.on("/fake/gate.py", returncode=2, stdout='{"error": "lookup failed"}')
    run_post_session(plan, item, outcome, config, hooks)
    assert run_cmd.find("/fake/wait-merge.sh") == []
    gate_rows = [
        json.loads(line)
        for line in config.resolved_wait_merge_gate_log.read_text().splitlines()
    ]
    assert gate_rows[0]["gate_exit_code"] == 2


def test_post_session_crashed_delivery_check_does_not_claim_observed_effect(
    tmp_path,
) -> None:
    """PR review finding P1: a delivery check that crashes (non-zero exit /
    OSError) must not be fed to the effect signal as if it had verified a
    reply. The fallback ``{"outcome":"handled"}`` raw was passed straight
    through to ``derive_effect_signal``, which short-circuits to
    ``EFFECT_OBSERVED`` for ``delivery=="handled"``. That re-creates exactly
    the lie the delivery_checked gate exists to prevent.

    The PR head and state are identical before and after (no observable
    effect on GitHub). With the fix, the effect is ``unknown`` — the only
    honest reading when no signal was successfully verified.
    """
    (tmp_path / "monitoring-rules.md").write_text("RULES CONTENT")
    item = make_item(types=["pr_update"], number=1234)
    work_file = _write_work_file(tmp_path, item)
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("rev-parse", stdout="abc123\n")
    # Before-snapshot: head=before11, state=OPEN
    # After-snapshot (fetch): identical — nothing moved on GitHub
    run_cmd.on(
        "gh",
        stdout='{"state": "OPEN", "headRefOid": "before11", "mergeCommit": null}',
    )
    hooks = _effect_hooks(run_cmd, head_after="before11")
    # Delivery check exits 1 (script broken / permission denied / network).
    # Fallback raw is '{"outcome":"handled"}' but the check did not verify.
    run_cmd.on("/fake/check-delivery.py", returncode=1)

    rc = run_work_file(work_file, config, hooks, backend="claude-code", lane="slow")

    assert rc == 0  # the worker was happy
    completed = [r for r in _ledger_rows(config) if r["phase"] == "completed"][0]
    # Without verified delivery, the PR head+state match (no_change) and the
    # effect signal must NOT report observed. Before the fix, this was
    # "observed" via the crashed-check fallback masquerading as a verified reply.
    assert (
        completed["effect"] != "observed"
    ), f"crashed delivery check must not claim observed effect, got {completed['effect']!r}"
    assert completed["outcome"] in {"no_effect", "unknown"}


def test_timeout_tier_instruction_kind_routes_to_adjudication(tmp_path) -> None:
    """PR review finding P2: Phase-A2 routes to adjudication via
    InstructionKind.GREPTILE_CONVERGENCE without adding the type to
    item.types. The timeout tier must honour the instruction kind so
    backoff-spawned adjudication sessions get the 1500s budget rather
    than the 900s default.
    """
    config = make_config(tmp_path)
    # greptile_needs_improvement is the type the backoff path leaves in
    # item.types; without the type fix, the default tier wins.
    timeout, desc = timeout_tier(
        ["greptile_needs_improvement"],
        False,
        config,
        instruction_kind="GREPTILE_CONVERGENCE",
    )
    assert timeout == config.adjudication_timeout == 1500
    assert desc == config.adjudication_time_desc == "~20 minutes"

    # Sanity: a different instruction kind does NOT route to adjudication.
    timeout, _ = timeout_tier(
        ["greptile_needs_improvement"], False, config, instruction_kind="OTHER"
    )
    assert timeout == config.default_timeout == 900


def test_timeout_tier_direct_mention_gets_assigned_issue_budget(tmp_path) -> None:
    """Direct @mention items must get at least the assigned_issue timeout (1500s).

    A 900s fast-lane session was killed at ~14 min before completing AW#1402.
    Regression: notification items with detail=direct_mention must land on ≥1500s.
    """
    config = make_config(tmp_path)
    # A bare notification item normally falls to the 900s default.
    timeout_default, _ = timeout_tier(["notification"], False, config)
    assert timeout_default == config.default_timeout == 900

    # With is_mention=True it is floored at the assigned_issue budget.
    timeout, desc = timeout_tier(["notification"], False, config, is_mention=True)
    assert timeout == config.assigned_issue_timeout == 1500
    assert desc == config.assigned_issue_time_desc == "~20 minutes"

    # assigned_issue still wins when both flags are set (order of precedence).
    timeout_ai, _ = timeout_tier(
        ["assigned_issue", "notification"], False, config, is_mention=True
    )
    assert timeout_ai == config.assigned_issue_timeout == 1500


# --- Claim behavior via execute path ---


def test_execute_plan_pr_before_snapshot_only_for_pr_items(tmp_path) -> None:
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("gh", stdout='{"state": "OPEN"}')
    hooks = make_hooks(run_cmd=run_cmd)
    item = make_item(types=["notification"], number=0)
    plan = plan_item(
        item,
        index=1,
        config=config,
        backend="codex",
        model="",
        monitoring_rules="",
        lifecycle=LifecycleResult(),
        arc=None,
        run_salt=1,
        records_dir=tmp_path,
        runner=hooks.runner,
        sysprompt_file="",
    )
    outcome = execute_plan(plan, item, config, hooks)
    assert outcome.pr_before_json == ""
    assert [c for c in run_cmd.calls if c["argv"][0] == "gh"] == []


@pytest.mark.parametrize(
    "item_type",
    sorted(PR_OBSERVE_TYPES),
)
def test_execute_plan_pr_before_snapshot_for_every_pr_scoped_type(
    tmp_path, item_type
) -> None:
    """Every PR-scoped item type must take a before-snapshot.

    Regression guard for the effect-observation gap: when a PR-addressed type
    is missing from the observation gate, `run_post_session` can only return
    EFFECT_UNKNOWN, which `pm_dispatch_recovery.classify_completion` maps onto
    CLASS_INEFFECTIVE — draining the retry budget and escalating PRs that PM
    was actually driving (gptme/gptme-contrib#1466).
    """
    config = make_config(tmp_path)
    run_cmd = FakeRunCmd()
    run_cmd.on("gh", stdout='{"state": "OPEN", "headRefOid": "aa"}')
    hooks = make_hooks(run_cmd=run_cmd)
    item = make_item(types=[item_type], number=7)
    plan = plan_item(
        item,
        index=1,
        config=config,
        backend="codex",
        model="",
        monitoring_rules="",
        lifecycle=LifecycleResult(),
        arc=None,
        run_salt=1,
        records_dir=tmp_path,
        runner=hooks.runner,
        sysprompt_file="",
    )
    outcome = execute_plan(plan, item, config, hooks)
    assert (
        outcome.pr_before_json != ""
    ), f"{item_type} is in PR_OBSERVE_TYPES but took no before-snapshot"
    assert any(
        c["argv"][:3] == ["gh", "pr", "view"] for c in run_cmd.calls
    ), f"{item_type} did not call `gh pr view`"


def test_pr_observe_types_is_superset_of_pr_state_types() -> None:
    """The observation gate must never be narrower than the bash-parity pair."""
    assert PR_STATE_TYPES <= PR_OBSERVE_TYPES
    # Types with no PR behind `number` must stay out, or `gh pr view` is called
    # with a workflow-run id / synthesized 0 / an issue number.
    assert not PR_OBSERVE_TYPES & {
        "agent_msg_reply",
        "assigned_issue",
        "erik_decision",
        "master_ci_failure",
        "notification",
        "task_closeout",
        "voice_postcall",
    }


def test_promote_item_state_copies_matching_files(tmp_path) -> None:
    config = make_config(tmp_path)
    pending = config.pending_state_dir
    pending.mkdir(parents=True)
    (pending / "gptme-gptme-pr-5-update.state").write_text("a")
    (pending / "gptme-gptme-issue-5.state").write_text("b")
    (pending / "gptme-gptme-pr-6-update.state").write_text("c")
    (pending / "gptme-gptme-master-ci.state").write_text("d")
    promote_item_state(config, "gptme/gptme", 5)
    names = {p.name for p in config.state_dir.iterdir()}
    assert names == {
        "gptme-gptme-pr-5-update.state",
        "gptme-gptme-issue-5.state",
        "gptme-gptme-master-ci.state",
    }


# --- Delivery rollback (bash lib.sh:946-995 parity) ---
#
# Regression cover for the runloops port dropping worker.sh's conditional
# promote: a session that exits without a thread reply must NOT consume the
# item's gate state or leave the dispatcher's launch-stamped event fingerprint
# in place, or the item is suppressed for the 6h event-unchanged TTL and never
# retried (live bite: gptme/gptme#3468, 2026-08-10).


@pytest.fixture
def cooldown_dir(tmp_path, monkeypatch):
    d = tmp_path / "cooldown"
    d.mkdir()
    monkeypatch.setenv("PM_DISPATCH_COOLDOWN_DIR", str(d))
    return d


def test_rollback_clears_event_marker_and_leaves_state_pending(
    tmp_path, cooldown_dir
) -> None:
    config = make_config(tmp_path)
    pending = config.pending_state_dir
    pending.mkdir(parents=True)
    (pending / "gptme-gptme-pr-3468-greptile.state").write_text("5:1:sha:dirty")
    (cooldown_dir / "gptme-gptme-3468.event").write_text("fingerprint")
    (cooldown_dir / "gptme-gptme-3468.event_logged").write_text("fingerprint")

    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")

    # Event fingerprint gone => next dispatch cycle re-evaluates the item.
    assert not (cooldown_dir / "gptme-gptme-3468.event").exists()
    assert not (cooldown_dir / "gptme-gptme-3468.event_logged").exists()
    # Pending state NOT promoted => the activity gate re-emits it.
    assert not config.state_dir.exists() or not list(config.state_dir.iterdir())
    assert (pending / "gptme-gptme-pr-3468-greptile.state").exists()


def test_rollback_reads_slot_key_from_env(tmp_path, cooldown_dir, monkeypatch) -> None:
    config = make_config(tmp_path)
    monkeypatch.setenv("PM_SLOT_KEY", "gptme/gptme#3468")
    (cooldown_dir / "gptme-gptme-3468.event").write_text("fingerprint")

    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")
    assert not (cooldown_dir / "gptme-gptme-3468.event").exists()


def test_rollback_drops_matching_notification_state(tmp_path, cooldown_dir) -> None:
    config = make_config(tmp_path)
    pending = config.pending_state_dir
    pending.mkdir(parents=True)
    (pending / "notif-1.map").write_text("gptme/gptme#3468")
    (pending / "notif-1.state").write_text("ts")
    (pending / "notif-2.map").write_text("gptme/gptme#9999")
    (pending / "notif-2.state").write_text("ts")

    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")

    assert not (pending / "notif-1.map").exists()
    assert not (pending / "notif-1.state").exists()
    assert (pending / "notif-2.map").exists()
    assert (pending / "notif-2.state").exists()


def test_rollback_gives_up_after_max_attempts(tmp_path, cooldown_dir) -> None:
    config = make_config(tmp_path)
    # Default cap is 2: two rollbacks, then the caller is told to promote.
    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")
    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")
    assert not rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")
    # Counter reset so a future genuine failure gets a full budget.
    assert not redelivery_attempts_file(config, "gptme/gptme", 3468).exists()
    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")


def test_rollback_respects_max_attempts_env(
    tmp_path, cooldown_dir, monkeypatch
) -> None:
    config = make_config(tmp_path)
    monkeypatch.setenv("PM_MAX_REDELIVERY_ATTEMPTS", "1")
    assert rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")
    assert not rollback_failed_delivery(config, "gptme/gptme", 3468, "gptme/gptme#3468")


def test_promote_item_state_resets_redelivery_counter(tmp_path, cooldown_dir) -> None:
    config = make_config(tmp_path)
    config.pending_state_dir.mkdir(parents=True)
    attempts = redelivery_attempts_file(config, "gptme/gptme", 3468)
    assert attempts is not None  # ensure the path was created
    attempts.write_text("1")

    promote_item_state(config, "gptme/gptme", 3468)

    assert not attempts.exists()


def test_clear_slot_event_markers_is_noop_without_slot_key(
    tmp_path, cooldown_dir
) -> None:
    config = make_config(tmp_path)
    (cooldown_dir / "gptme-gptme-3468.event").write_text("fingerprint")
    assert clear_slot_event_markers(config, "") is False
    assert (cooldown_dir / "gptme-gptme-3468.event").exists()


def test_promote_item_state_number_zero_promotes_notifs(tmp_path) -> None:
    config = make_config(tmp_path)
    pending = config.pending_state_dir
    pending.mkdir(parents=True)
    (pending / "notif-1.state").write_text("a")
    (pending / "gptme-gptme-pr-5.state").write_text("b")
    promote_item_state(config, "gptme/gptme", 0)
    names = {p.name for p in config.state_dir.iterdir()}
    assert names == {"notif-1.state"}
