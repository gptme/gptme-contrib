"""Tests for Thompson sampling bandit engine."""

import builtins
import importlib
import json
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gptme_sessions import Bandit, BanditArm, BanditState, SessionRecord
from gptme_sessions.thompson_sampling import load_bandit_means


# ── BanditArm unit tests ─────────────────────────────────────────────────────


def test_bandit_arm_defaults():
    """BanditArm starts with uniform prior Beta(1, 1)."""
    arm = BanditArm(arm_id="code")
    assert arm.alpha == 1.0
    assert arm.beta == 1.0
    assert arm.total_selections == 0
    assert arm.mean == 0.5


def test_bandit_arm_mean():
    """Mean equals alpha / (alpha + beta)."""
    arm = BanditArm(arm_id="code", alpha=3.0, beta=1.0)
    assert arm.mean == pytest.approx(0.75)


def test_bandit_arm_update_bool():
    """Bool reward update increments correct parameter."""
    arm = BanditArm(arm_id="code")
    arm.update(True)
    assert arm.alpha == pytest.approx(2.0)
    assert arm.beta == pytest.approx(1.0)
    assert arm.total_selections == 1
    assert arm.total_rewards == 1

    arm.update(False)
    assert arm.alpha == pytest.approx(2.0)
    assert arm.beta == pytest.approx(2.0)
    assert arm.total_selections == 2
    assert arm.total_rewards == 1


def test_bandit_arm_update_graded():
    """Graded float reward splits between alpha and beta."""
    arm = BanditArm(arm_id="infra")
    arm.update(0.6)
    assert arm.alpha == pytest.approx(1.6)
    assert arm.beta == pytest.approx(1.4)
    assert arm.total_rewards == 1  # 0.6 > 0.5


def test_bandit_arm_update_partial_failure():
    """Graded reward below 0.5 does not increment total_rewards."""
    arm = BanditArm(arm_id="triage")
    arm.update(0.3)
    assert arm.alpha == pytest.approx(1.3)
    assert arm.beta == pytest.approx(1.7)
    assert arm.total_rewards == 0


def test_bandit_arm_decay():
    """Decay moves alpha/beta toward prior (1, 1)."""
    arm = BanditArm(arm_id="code", alpha=5.0, beta=3.0)
    arm.apply_decay(gamma=0.9)
    assert arm.alpha == pytest.approx(1.0 + 0.9 * 4.0)  # 1 + 0.9*(5-1) = 4.6
    assert arm.beta == pytest.approx(1.0 + 0.9 * 2.0)  # 1 + 0.9*(3-1) = 2.8


def test_bandit_arm_decay_prior_invariant():
    """Arm at prior Beta(1,1) is unchanged by decay."""
    arm = BanditArm(arm_id="code")
    arm.apply_decay(gamma=0.95)
    assert arm.alpha == pytest.approx(1.0)
    assert arm.beta == pytest.approx(1.0)


def test_bandit_arm_sample_range():
    """sample() always returns a value in [0, 1]."""
    arm = BanditArm(arm_id="code", alpha=5.0, beta=2.0)
    import random

    rng = random.Random(42)
    for _ in range(100):
        s = arm.sample(rng)
        assert 0.0 <= s <= 1.0


def test_bandit_arm_ucb_above_mean():
    """UCB is always >= mean."""
    arm = BanditArm(arm_id="code", alpha=3.0, beta=2.0)
    assert arm.ucb >= arm.mean


def test_bandit_arm_update_out_of_range():
    """Out-of-range reward raises ValueError instead of corrupting Beta params."""
    arm = BanditArm(arm_id="code")
    with pytest.raises(ValueError, match=r"reward must be in \[0, 1\]"):
        arm.update(2.0)
    with pytest.raises(ValueError, match=r"reward must be in \[0, 1\]"):
        arm.update(-0.1)
    # Params must be unchanged after failed update
    assert arm.alpha == 1.0
    assert arm.beta == 1.0
    assert arm.total_selections == 0


# ── BanditState unit tests ────────────────────────────────────────────────────


def test_bandit_state_get_or_create():
    """get_or_create_arm creates new arm with defaults."""
    state = BanditState()
    arm = state.get_or_create_arm("code")
    assert arm.alpha == 1.0
    # Same call returns same arm
    assert state.get_or_create_arm("code") is arm


def test_bandit_state_update_session_string_outcome():
    """String outcomes map to 0.0/1.0 correctly."""
    state = BanditState()
    state.update_session(["code", "triage"], outcome="productive")
    assert state.arms["code"].alpha == pytest.approx(2.0)
    assert state.arms["triage"].alpha == pytest.approx(2.0)
    assert state.total_sessions == 1

    state.update_session(["code"], outcome="noop")
    assert state.arms["code"].beta == pytest.approx(2.0)
    assert state.total_sessions == 2


def test_bandit_state_update_session_violated_policy_outcome():
    """violated_policy outcome maps to 0.0 reward (same as noop/failed)."""
    state = BanditState()
    state.update_session(["code"], outcome="productive")
    state.update_session(["code"], outcome="violated_policy")
    assert state.arms["code"].beta == pytest.approx(2.0)
    assert state.arms["code"].alpha == pytest.approx(2.0)
    assert state.total_sessions == 2


def test_bandit_state_update_session_float_outcome():
    """Float outcomes produce fractional alpha/beta updates."""
    state = BanditState()
    state.update_session(["infra"], outcome=0.7)
    assert state.arms["infra"].alpha == pytest.approx(1.7)
    assert state.arms["infra"].beta == pytest.approx(1.3)


def test_bandit_state_update_returns_reward_bools():
    """update_session returns True for arms with reward > 0.5."""
    state = BanditState()
    result = state.update_session(["code", "triage"], outcome=0.8)
    assert result["code"] is True
    assert result["triage"] is True

    result = state.update_session(["code"], outcome=0.3)
    assert result["code"] is False


def test_bandit_state_per_arm_rewards():
    """per_arm_rewards overrides base reward for specified arms."""
    state = BanditState()
    state.update_session(
        ["code", "triage", "content"],
        outcome="productive",
        per_arm_rewards={"code": 0.9, "triage": 0.2},
    )
    # code gets per-arm reward 0.9 → alpha=1.9
    assert state.arms["code"].alpha == pytest.approx(1.9)
    # triage gets per-arm reward 0.2 → alpha=1.2
    assert state.arms["triage"].alpha == pytest.approx(1.2)
    # content not in per_arm_rewards → uses base reward 1.0 → alpha=2.0
    assert state.arms["content"].alpha == pytest.approx(2.0)


def test_bandit_state_contextual_arms():
    """Contextual arms track per-context data separately from global arm."""
    state = BanditState()
    state.update_session(["infra"], outcome=0.8, context=("infra", "opus"))

    # Global arm updated
    assert "infra" in state.arms
    # Contextual arm created
    assert "infra" in state.contextual_arms
    ctx_key = '["infra", "opus"]'
    assert ctx_key in state.contextual_arms["infra"]
    ctx_arm = state.contextual_arms["infra"][ctx_key]
    assert ctx_arm.alpha == pytest.approx(1.8)


def test_bandit_state_rank_by_expected():
    """rank_by_expected returns arms sorted descending by mean."""
    state = BanditState()
    state.update_session(["code"], outcome=1.0)  # code: mean > 0.5
    state.update_session(["triage"], outcome=0.0)  # triage: mean < 0.5
    ranked = state.rank_by_expected()
    assert ranked[0][0] == "code"
    assert ranked[-1][0] == "triage"


def test_bandit_state_apply_decay():
    """apply_decay decays all arms including contextual."""
    state = BanditState()
    state.update_session(["code"], outcome=1.0)
    state.update_session(["infra"], outcome=0.8, context=("infra", "opus"))

    initial_code_alpha = state.arms["code"].alpha
    count = state.apply_decay(gamma=0.9)
    # Both global and contextual arms decayed
    assert count >= 2
    assert state.arms["code"].alpha < initial_code_alpha


def test_bandit_state_sample_scores():
    """sample_scores returns dict with all requested arms."""
    state = BanditState()
    state.update_session(["code", "triage"], outcome="productive")
    scores = state.sample_scores(["code", "triage", "new-arm"], seed=42)
    assert set(scores.keys()) == {"code", "triage", "new-arm"}
    for s in scores.values():
        assert 0.0 <= s <= 1.0


def test_bandit_state_sample_deterministic_with_seed():
    """Same seed produces same scores."""
    state = BanditState()
    state.update_session(["code", "triage"], outcome="productive")
    s1 = state.sample_scores(["code", "triage"], seed=99)
    s2 = state.sample_scores(["code", "triage"], seed=99)
    assert s1 == s2


def test_bandit_state_prune_stale():
    """prune_stale removes arms with zero selections."""
    state = BanditState()
    state.get_or_create_arm("never-selected")  # never updated
    state.update_session(["active"], outcome=1.0)

    pruned = state.prune_stale(min_selections=0)
    assert pruned == 1
    assert "never-selected" not in state.arms
    assert "active" in state.arms


def test_bandit_state_prune_stale_contextual():
    """prune_stale also removes stale contextual arms, not just global ones."""
    state = BanditState()
    # Create a stale contextual arm (never selected → zero selections)
    state.get_or_create_contextual_arm("infra", ("infra", "opus"))  # never updated
    # Also add an active global arm so prune doesn't wipe state entirely
    state.update_session(["code"], outcome=1.0)

    pruned = state.prune_stale(min_selections=0)
    # The stale contextual arm for "infra" should be gone
    assert pruned >= 1
    assert "infra" not in state.contextual_arms
    # Active global arm survives
    assert "code" in state.arms


def test_bandit_state_prune_stale_age_for_selected_arms():
    """prune_stale age check applies to selected arms — max_age_days is not dead code."""
    from datetime import timedelta

    state = BanditState()
    # Arm with selections but last_updated far in the past
    arm = state.get_or_create_arm("old-but-selected")
    arm.update(1.0)  # 1 selection
    old_time = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    arm.last_updated = old_time

    # Also add a recently-updated arm that should survive
    state.update_session(["recent"], outcome=1.0)

    pruned = state.prune_stale(min_selections=0, max_age_days=90)
    assert pruned >= 1
    assert "old-but-selected" not in state.arms
    assert "recent" in state.arms


def test_bandit_state_update_session_unknown_string_raises():
    """Unknown string outcomes raise ValueError instead of silently mapping to 0.0."""
    state = BanditState()
    with pytest.raises(ValueError, match="Unknown string outcome"):
        state.update_session(["code"], outcome="failure")
    with pytest.raises(ValueError, match="Unknown string outcome"):
        state.update_session(["code"], outcome="Productive")  # wrong case
    # State must be unchanged
    assert "code" not in state.arms


def test_bandit_sample_scores_no_mutation():
    """sample_scores does not mutate state for unseen arm IDs."""
    state = BanditState()
    state.update_session(["known"], outcome=1.0)
    arms_before = set(state.arms.keys())

    scores = state.sample_scores(["known", "unseen-a", "unseen-b"], seed=42)
    # Unseen arms get a valid score
    assert 0.0 <= scores["unseen-a"] <= 1.0
    assert 0.0 <= scores["unseen-b"] <= 1.0
    # But state is not mutated
    assert set(state.arms.keys()) == arms_before


# ── Bandit (manager) tests ────────────────────────────────────────────────────


def test_bandit_persist_and_reload(tmp_path: Path):
    """Bandit persists state to disk and reloads correctly."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code", "triage"], outcome="productive")
    bandit.update(["infra"], outcome=0.4)

    bandit2 = Bandit(state_dir=tmp_path)
    assert bandit2.state.total_sessions == 2
    assert "code" in bandit2.state.arms
    assert "infra" in bandit2.state.arms
    assert bandit2.state.arms["code"].alpha == pytest.approx(2.0)
    assert bandit2.state.arms["infra"].beta > 1.0


def test_bandit_sample(tmp_path: Path):
    """Bandit.sample returns scores for all requested arms."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code", "content"], outcome=0.8)
    scores = bandit.sample(["code", "content", "unseen"])
    assert len(scores) == 3
    for s in scores.values():
        assert 0.0 <= s <= 1.0


def test_bandit_decay(tmp_path: Path):
    """Bandit.decay reduces accumulated evidence toward prior."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0)
    alpha_before = bandit.state.arms["code"].alpha

    bandit.decay(gamma=0.5)

    bandit2 = Bandit(state_dir=tmp_path)
    assert bandit2.state.arms["code"].alpha < alpha_before


def test_bandit_update_with_decay(tmp_path: Path):
    """Passing decay_rate to update applies decay before the update."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0)
    alpha_after_one = bandit.state.arms["code"].alpha

    bandit.update(["code"], outcome=1.0, decay_rate=0.5)
    # After decay: alpha = 1 + 0.5*(alpha_after_one - 1), then +1.0 for the update
    expected = 1.0 + 0.5 * (alpha_after_one - 1.0) + 1.0
    assert bandit.state.arms["code"].alpha == pytest.approx(expected)


def test_bandit_context_isolation(tmp_path: Path):
    """Context-specific posteriors don't pollute each other."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0, context=("code", "opus"))
    bandit.update(["code"], outcome=0.0, context=("code", "sonnet"))

    opus_arm = bandit.state.contextual_arms["code"]['["code", "opus"]']
    sonnet_arm = bandit.state.contextual_arms["code"]['["code", "sonnet"]']
    assert opus_arm.alpha > sonnet_arm.alpha


def test_bandit_corrupted_state_file(tmp_path: Path):
    """Corrupted state file starts fresh without crash."""
    state_file = tmp_path / "bandit-state.json"
    state_file.write_text("not valid json")

    bandit = Bandit(state_dir=tmp_path)
    assert bandit.state.total_sessions == 0
    assert bandit.state.arms == {}


def test_bandit_status_report(tmp_path: Path):
    """status_report produces non-empty human-readable string."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code", "triage", "content"], outcome=0.8)
    report = bandit.status_report()
    assert "code" in report
    assert "E[p]" in report


def test_bandit_status_report_empty(tmp_path: Path):
    """status_report handles empty state gracefully."""
    bandit = Bandit(state_dir=tmp_path)
    report = bandit.status_report()
    assert "No arms" in report


def test_bandit_old_format_compat(tmp_path: Path):
    """Bandit loads old state files that use 'lesson_path' key."""
    old_state = {
        "arms": {
            "lessons/foo.md": {
                "lesson_path": "lessons/foo.md",
                "alpha": 3.0,
                "beta": 1.0,
                "total_selections": 2,
                "total_rewards": 2,
                "last_updated": "",
            }
        },
        "total_sessions": 2,
        "created": "2025-01-01T00:00:00+00:00",
        "last_updated": "2025-01-02T00:00:00+00:00",
    }
    (tmp_path / "bandit-state.json").write_text(json.dumps(old_state))
    bandit = Bandit(state_dir=tmp_path)
    assert "lessons/foo.md" in bandit.state.arms
    assert bandit.state.arms["lessons/foo.md"].alpha == 3.0


# ── trigger field tests ───────────────────────────────────────────────────────


def test_session_record_trigger_default():
    """trigger field defaults to None."""
    r = SessionRecord()
    assert r.trigger is None


def test_session_record_trigger_roundtrip():
    """trigger field survives serialize → deserialize."""
    r = SessionRecord(trigger="timer", run_type="autonomous")
    d = r.to_dict()
    assert d["trigger"] == "timer"
    r2 = SessionRecord.from_dict(d)
    assert r2.trigger == "timer"


def test_session_record_trigger_values():
    """All documented trigger values are accepted."""
    for trigger in ("timer", "dispatch", "manual", "spawn"):
        r = SessionRecord(trigger=trigger)
        assert r.trigger == trigger


def test_session_record_trigger_independent_of_run_type():
    """trigger and run_type coexist for backward compatibility."""
    r = SessionRecord(run_type="autonomous", trigger="timer")
    assert r.run_type == "autonomous"
    assert r.trigger == "timer"


def test_session_record_from_dict_without_trigger():
    """Old records without trigger field deserialize fine (defaults to None)."""
    d = {"model": "opus", "outcome": "productive", "run_type": "autonomous"}
    r = SessionRecord.from_dict(d)
    assert r.trigger is None


# ── load_bandit_means tests ───────────────────────────────────────────────────


def test_load_bandit_means_no_mutation(tmp_path: Path):
    """load_bandit_means with context does not mutate bandit state on disk."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0, context=("code", "opus"))

    arms_before = set(bandit.state.arms.keys())
    _ = load_bandit_means(tmp_path, arm_ids=["code", "unseen"], context=("code", "opus"))

    # Reload from disk and verify state is unchanged
    bandit2 = Bandit(state_dir=tmp_path)
    assert set(bandit2.state.arms.keys()) == arms_before


def test_load_bandit_means_unknown_arm_returns_half(tmp_path: Path):
    """Unknown arms return 0.5 (uninformative prior) whether or not context given."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0)

    means_no_ctx = load_bandit_means(tmp_path, arm_ids=["unseen"])
    assert means_no_ctx["unseen"] == pytest.approx(0.5)

    means_ctx = load_bandit_means(tmp_path, arm_ids=["unseen"], context=("code", "opus"))
    assert means_ctx["unseen"] == pytest.approx(0.5)


def test_bandit_state_prune_stale_min_selections_threshold():
    """prune_stale with min_selections > 0 prunes arms with 1..min_selections selections."""
    state = BanditState()
    # Arm with 1 selection — below threshold of 3
    arm_low = state.get_or_create_arm("low-selections")
    arm_low.update(1.0)  # 1 selection
    # Arm with 5 selections — above threshold of 3, should survive
    arm_high = state.get_or_create_arm("high-selections")
    for _ in range(5):
        arm_high.update(0.8)

    pruned = state.prune_stale(min_selections=3)
    assert pruned == 1, f"Expected 1 pruned, got {pruned}"
    assert "low-selections" not in state.arms, "Arm with 1 selection should be pruned"
    assert "high-selections" in state.arms, "Arm with 5 selections should survive"


def test_bandit_state_prune_stale_min_selections_boundary():
    """Arms with exactly min_selections selections ARE pruned (semantics: total_selections <= min_selections)."""
    state = BanditState()
    # Arm with exactly min_selections=3 selections — at the boundary, gets pruned
    arm_exact = state.get_or_create_arm("exact-threshold")
    for _ in range(3):
        arm_exact.update(0.5)
    # Arm with 4 selections — above threshold, should survive
    arm_above = state.get_or_create_arm("above-threshold")
    for _ in range(4):
        arm_above.update(0.5)

    pruned = state.prune_stale(min_selections=3)
    assert pruned == 1, f"Expected 1 pruned, got {pruned}"
    assert "exact-threshold" not in state.arms, "Arm with exactly min_selections should be pruned"
    assert "above-threshold" in state.arms, "Arm above threshold should survive"


def test_missing_posix_lock_support_preserves_reads_but_refuses_mutation(tmp_path, monkeypatch):
    """Unsupported platforms must not silently persist an unserialized update."""
    real_import = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("fcntl unavailable")
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "__import__", without_fcntl)
        module = importlib.reload(importlib.import_module("gptme_sessions.thompson_sampling"))
        bandit = module.Bandit(state_dir=tmp_path)
        assert module.load_bandit_means(tmp_path, arm_ids=["code"]) == {"code": 0.5}
        with pytest.raises(ImportError, match="fcntl unavailable"):
            bandit.update(["code"], 1.0)
        assert not bandit.state_file.exists()
        assert bandit.state.total_sessions == 0


# ── cross-process mutation locking ───────────────────────────────────────────

# The package uses POSIX flock; fork also avoids re-importing pytest's
# importlib-loaded module under an unresolvable synthetic name.
_MP_CTX = (
    multiprocessing.get_context("fork")
    if "fork" in multiprocessing.get_all_start_methods()
    else None
)


def _update_worker(
    state_dir: str,
    arm_id: str,
    updates: int,
    start_event: "multiprocessing.synchronize.Event",
) -> None:
    """Load a Bandit instance, wait for the starting gun, then update."""
    bandit = Bandit(state_dir=Path(state_dir))
    assert start_event.wait(timeout=15), "parent never released workers"
    for _ in range(updates):
        bandit.update([arm_id], outcome=1.0)


def _locked_update_worker(
    state_dir: str,
    arm_id: str,
    ready_event: "multiprocessing.synchronize.Event",
    done_event: "multiprocessing.synchronize.Event",
) -> None:
    bandit = Bandit(state_dir=Path(state_dir))
    ready_event.set()
    bandit.update([arm_id], outcome=1.0)
    done_event.set()


def test_update_reloads_stale_instance_state(tmp_path: Path):
    """A Bandit loaded before another instance's update must not clobber it."""
    writer = Bandit(state_dir=tmp_path)
    writer.update(["first"], outcome=1.0)

    # Snapshot that predates the next writer's update.
    stale = Bandit(state_dir=tmp_path)
    writer.update(["second"], outcome=1.0)

    stale.update(["third"], outcome=1.0)

    reloaded = Bandit(state_dir=tmp_path)
    assert {"first", "second", "third"} <= set(reloaded.state.arms)
    assert reloaded.state.arms["second"].alpha == pytest.approx(2.0)


def test_decay_reloads_stale_instance_state(tmp_path: Path):
    """Decay from a stale instance must preserve unseen updates and decay them."""
    writer = Bandit(state_dir=tmp_path)
    stale = Bandit(state_dir=tmp_path)  # snapshot before the update
    writer.update(["fresh"], outcome=1.0)

    stale.decay(gamma=0.5)

    reloaded = Bandit(state_dir=tmp_path)
    assert "fresh" in reloaded.state.arms
    # alpha = 1 + 0.5 * (2 - 1) = 1.5; the update was seen and decayed.
    assert reloaded.state.arms["fresh"].alpha == pytest.approx(1.5)


def test_prune_reloads_stale_instance_state(tmp_path: Path):
    """Prune from a stale instance must not delete arms it never loaded."""
    writer = Bandit(state_dir=tmp_path)
    stale = Bandit(state_dir=tmp_path)  # snapshot before the update
    writer.update(["fresh"], outcome=1.0)

    stale.prune(min_selections=0)

    reloaded = Bandit(state_dir=tmp_path)
    assert "fresh" in reloaded.state.arms
    assert reloaded.state.arms["fresh"].alpha == pytest.approx(2.0)


def test_bandit_state_lock_file_is_sidecar(tmp_path: Path):
    """Mutation creates a permanent sidecar lock next to the state file."""
    bandit = Bandit(state_dir=tmp_path)
    bandit.update(["code"], outcome=1.0)

    # The sentinel must remain so every caller flocks the same inode.
    assert (tmp_path / "bandit-state.json.lock").exists()


@pytest.mark.skipif(_MP_CTX is None, reason="requires POSIX fork/flock")
def test_state_lock_blocks_other_process_until_release(tmp_path: Path):
    """A ready child cannot finish its mutation until the parent releases flock."""
    bandit = Bandit(state_dir=tmp_path)
    ready = _MP_CTX.Event()
    done = _MP_CTX.Event()
    child = _MP_CTX.Process(
        target=_locked_update_worker,
        args=(str(tmp_path), "child", ready, done),
        daemon=True,
    )
    try:
        with bandit._state_lock():
            child.start()
            assert ready.wait(timeout=15), "child did not reach update"
            assert not done.wait(timeout=0.2), "child updated while parent held flock"
        assert done.wait(timeout=15), "child did not finish after lock release"
        child.join(timeout=15)
        assert not child.is_alive()
        assert child.exitcode == 0
        assert "child" in Bandit(state_dir=tmp_path).state.arms
    finally:
        if child.pid is not None:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)


@pytest.mark.skipif(_MP_CTX is None, reason="requires POSIX fork/flock")
def test_concurrent_processes_do_not_lose_updates(tmp_path: Path):
    """Two processes updating distinct arms must both persist all updates."""
    updates_per_process = 15
    start_event = _MP_CTX.Event()
    processes = [
        _MP_CTX.Process(
            target=_update_worker,
            args=(str(tmp_path), arm, updates_per_process, start_event),
            daemon=True,
        )
        for arm in ("proc-a", "proc-b")
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=60)
            assert not process.is_alive(), "worker did not finish"
            assert process.exitcode == 0

        reloaded = Bandit(state_dir=tmp_path)
        assert reloaded.state.total_sessions == 2 * updates_per_process
        for arm in ("proc-a", "proc-b"):
            assert arm in reloaded.state.arms
            assert reloaded.state.arms[arm].total_selections == updates_per_process
            assert reloaded.state.arms[arm].alpha == pytest.approx(1.0 + updates_per_process)
    finally:
        # Never let a failed assertion leak workers into the rest of the suite.
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def _saved_bandit(tmp_path: Path, arm: str = "a") -> Bandit:
    bandit = Bandit(state_dir=tmp_path)
    bandit.update([arm], outcome=1.0)
    return bandit


def test_save_keeps_backup_of_previous_state(tmp_path: Path):
    bandit = _saved_bandit(tmp_path)
    first = bandit.state_file.read_text()
    bandit.update(["b"], outcome=0.0)

    assert bandit.backup_file.read_text() == first
    assert "b" in json.loads(bandit.state_file.read_text())["arms"]


def test_corrupt_state_recovers_from_backup(tmp_path: Path):
    bandit = _saved_bandit(tmp_path)
    bandit.update(["b"], outcome=1.0)
    bandit.state_file.write_text('{"arms": {"a": ')  # truncated mid-write

    recovered = Bandit(state_dir=tmp_path)

    assert "a" in recovered.state.arms
    assert recovered.state.total_sessions == 1


def test_save_never_backs_up_a_corrupt_state_file(tmp_path: Path):
    bandit = _saved_bandit(tmp_path)
    bandit.update(["b"], outcome=1.0)
    good_backup = bandit.backup_file.read_text()
    bandit.state_file.write_text("not json")

    recovered = Bandit(state_dir=tmp_path)
    recovered.save()

    assert recovered.backup_file.read_text() == good_backup
    assert json.loads(recovered.state_file.read_text())["arms"]

    # Once repaired, the next save rotates the repaired primary into the backup.
    recovered.update(["c"], outcome=1.0)
    repaired = recovered.state_file.read_text()
    recovered.update(["d"], outcome=1.0)
    assert recovered.backup_file.read_text() == repaired
    assert "c" in Bandit(state_dir=tmp_path).state.arms


@pytest.mark.parametrize(
    "corrupt",
    [
        "[]",
        '"corrupt"',
        "null",
        '{"arms": [1]}',
        '{"arms": null}',
        '{"arms": {"a": 1}}',
        '{"arms": {"a": {}}}',
        '{"contextual_arms": [1]}',
        '{"contextual_arms": {"a": [1]}}',
        '{"contextual_arms": {"a": {"null": 1}}}',
        '{"contextual_arms": {"a": {"null": {}}}}',
    ],
)
@pytest.mark.parametrize("has_backup", [True, False])
def test_malformed_json_state_recovers_without_poisoning_backup(tmp_path, corrupt, has_backup):
    """Load and backup rotation must agree on which state documents are usable."""
    bandit = _saved_bandit(tmp_path)
    bandit.update(["b"], outcome=1.0)
    good_backup = bandit.backup_file.read_text()
    if not has_backup:
        bandit.backup_file.unlink()
    bandit.state_file.write_text(corrupt)

    recovered = Bandit(state_dir=tmp_path)
    assert recovered.state.total_sessions == (1 if has_backup else 0)
    assert set(recovered.state.arms) == ({"a"} if has_backup else set())
    recovered.update(["c"], outcome=1.0)

    if has_backup:
        assert recovered.backup_file.read_text() == good_backup
    else:
        assert not recovered.backup_file.exists()
    reloaded = Bandit(state_dir=tmp_path)
    assert reloaded.state.total_sessions == (2 if has_backup else 1)
    assert set(reloaded.state.arms) == ({"a", "c"} if has_backup else {"c"})


def test_failed_backup_does_not_advance_state(tmp_path: Path, monkeypatch):
    bandit = _saved_bandit(tmp_path)
    bandit.update(["b"], outcome=1.0)
    before = bandit.state_file.read_text()
    backup_before = bandit.backup_file.read_text()
    module = importlib.import_module("gptme_sessions.thompson_sampling")
    real_replace = module.os.replace

    def fail_backup_replace(src, dst):
        if dst == bandit.backup_file:
            raise OSError("backup disk full")
        real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", fail_backup_replace)
    bandit.state.total_sessions = 99
    with pytest.raises(OSError, match="backup disk full"):
        bandit.save()

    assert bandit.state_file.read_text() == before
    assert bandit.backup_file.read_text() == backup_before
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_write_leaves_previous_state_intact(tmp_path: Path, monkeypatch):
    bandit = _saved_bandit(tmp_path)
    before = bandit.state_file.read_text()

    module = importlib.import_module("gptme_sessions.thompson_sampling")
    real_replace = module.os.replace

    def fail_replace(src, dst):
        if dst == bandit.state_file:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", fail_replace)
    bandit.state.total_sessions = 99
    with pytest.raises(OSError):
        bandit.save()

    assert bandit.state_file.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []


def test_overlapping_atomic_writes_use_distinct_temporary_files(tmp_path, monkeypatch):
    """Another write in the same process cannot consume the first writer's temp."""
    module = importlib.import_module("gptme_sessions.thompson_sampling")
    real_replace = module.os.replace
    target = tmp_path / "state.json"
    sources = []

    def overlapping_replace(src, dst):
        sources.append(src)
        if len(sources) == 1:
            module._atomic_write_text(target, '{"writer": "inner"}\n')
        real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", overlapping_replace)
    module._atomic_write_text(target, '{"writer": "outer"}\n')

    assert len(set(sources)) == 2
    assert json.loads(target.read_text()) == {"writer": "outer"}
    assert not list(tmp_path.glob("*.tmp"))
