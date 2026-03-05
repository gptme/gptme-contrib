"""Tests for Thompson sampling bandit engine."""

import json
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
