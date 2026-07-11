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
    ArcInfo,
    RunItem,
    RunItemConfig,
    RunItemHooks,
    _handle_cc_rate_limit,
    build_execution_plan,
    derive_lock_paths,
    derive_session_id,
    execute_plan,
    issue_coordination_key,
    item_slug,
    plan_item,
    predict_cc_trajectory_path,
    promote_item_state,
    resolve_backend_trajectory,
    resolve_cc_sub_suffix,
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


def test_promote_item_state_number_zero_promotes_notifs(tmp_path) -> None:
    config = make_config(tmp_path)
    pending = config.pending_state_dir
    pending.mkdir(parents=True)
    (pending / "notif-1.state").write_text("a")
    (pending / "gptme-gptme-pr-5.state").write_text("b")
    promote_item_state(config, "gptme/gptme", 0)
    names = {p.name for p in config.state_dir.iterdir()}
    assert names == {"notif-1.state"}
