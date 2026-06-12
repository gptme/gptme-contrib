"""Tests for pm_bandit module."""

from __future__ import annotations

import json
import random

from gptme_runloops.pm_bandit import (
    PM_WORK_TYPES,
    BanditArm,
    PmModelBandit,
    _arm_id,
    _parse_arm_id,
)


def test_bandit_arm_default_prior():
    """A fresh arm should have uniform Beta(1,1) prior -> mean 0.5."""
    arm = BanditArm()
    assert arm.alpha == 1.0
    assert arm.beta == 1.0
    assert arm.mean == 0.5
    assert arm.total_selections == 0


def test_bandit_arm_update():
    """After a productive outcome, mean should rise."""
    arm = BanditArm()
    arm.update(1.0)  # productive
    assert arm.alpha == 2.0
    assert arm.beta == 1.0
    assert arm.mean == 2.0 / 3.0
    assert arm.total_selections == 1


def test_bandit_arm_update_failure():
    """After a failed outcome, mean should fall."""
    arm = BanditArm()
    arm.update(0.0)  # failed
    assert arm.alpha == 1.0
    assert arm.beta == 2.0
    assert arm.mean == 1.0 / 3.0
    assert arm.total_selections == 1


def test_bandit_arm_update_graded():
    """Graded rewards should work for partial success."""
    arm = BanditArm()
    arm.update(0.7)
    assert arm.alpha == 1.7
    assert arm.beta == 1.3


def test_bandit_arm_sample_deterministic():
    """Same seed should produce same sample."""
    arm = BanditArm()
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    assert arm.sample(rng1) == arm.sample(rng2)


def test_arm_id_roundtrip():
    """_arm_id and _parse_arm_id should be inverses."""
    aid = _arm_id("ci-fix", "haiku")
    assert aid == "pm-model:ci-fix:haiku"
    parsed = _parse_arm_id(aid)
    assert parsed == ("ci-fix", "haiku")


def test_parse_arm_id_malformed():
    """Malformed arm IDs should return None."""
    assert _parse_arm_id("invalid") is None
    assert _parse_arm_id("pm-model:onlyone") is None
    assert _parse_arm_id("other:ci-fix:model") is None


def test_pm_work_types_defined():
    """PM_WORK_TYPES should have the expected set of work types."""
    assert "ci-fix" in PM_WORK_TYPES
    assert "strategy-reply" in PM_WORK_TYPES
    assert "greptile-fix" in PM_WORK_TYPES
    assert "pr-review" in PM_WORK_TYPES
    assert 6 <= len(PM_WORK_TYPES) <= 12  # sensible range


def test_resolve_model_default(tmp_path):
    """With no state, resolve_model falls back to default."""
    bandit = PmModelBandit(state_dir=str(tmp_path / "pm-dispatch"))
    model = bandit.resolve_model("ci-fix")
    assert model == "sonnet"


def test_resolve_model_single_option(tmp_path):
    """With only one model available, return it regardless."""
    bandit = PmModelBandit(state_dir=str(tmp_path / "pm-dispatch"))
    assert bandit.resolve_model("ci-fix", ["haiku"]) == "haiku"


def test_resolve_model_after_outcome(tmp_path):
    """After recording productive outcomes for one model, that model
    should be preferred (but not guaranteed — Thompson sampling is random)."""
    bandit = PmModelBandit(state_dir=str(tmp_path / "pm-dispatch"))
    # Give haiku 10 productive outcomes for ci-fix
    for _ in range(10):
        bandit.record_outcome("ci-fix", "haiku", "productive")
    # Give sonnet 0 productive outcomes for ci-fix (10 failures)
    for _ in range(10):
        bandit.record_outcome("ci-fix", "sonnet", "failed")

    # With a fixed seed, haiku should win consistently
    rng = random.Random(42)
    haiku_wins = 0
    for _ in range(100):
        model = bandit.resolve_model("ci-fix", ["haiku", "sonnet"], rng=rng)
        if model == "haiku":
            haiku_wins += 1
    assert haiku_wins > 80  # haiku should win the vast majority


def test_resolve_model_exploration(tmp_path):
    """With few observations, exploration should still give the
    underdog a chance (Thompson sampling naturally explores)."""
    bandit = PmModelBandit(state_dir=str(tmp_path / "pm-dispatch"))
    # Just 2 productive for haiku, 0 for sonnet — still some uncertainty
    bandit.record_outcome("pr-review", "haiku", "productive")
    bandit.record_outcome("pr-review", "haiku", "productive")

    rng = random.Random(123)
    sonnet_wins = 0
    for _ in range(100):
        model = bandit.resolve_model("pr-review", ["haiku", "sonnet"], rng=rng)
        if model == "sonnet":
            sonnet_wins += 1
    # Sonnet should win at least a few times due to exploration
    assert sonnet_wins > 0


def test_record_outcome_persists(tmp_path):
    """record_outcome should persist to disk for the next bandit instance."""
    state_dir = tmp_path / "pm-dispatch"
    bandit = PmModelBandit(state_dir=str(state_dir))
    bandit.record_outcome("ci-fix", "haiku", "productive")

    # Verify the file was written
    state_file = state_dir / "bandit-state.json"
    assert state_file.exists()

    data = json.loads(state_file.read_text())
    assert "pm-model:ci-fix:haiku" in data["arms"]


def test_record_outcome_loads(tmp_path):
    """A new PmModelBandit should load previously persisted state."""
    state_dir = tmp_path / "pm-dispatch"
    bandit1 = PmModelBandit(state_dir=str(state_dir))
    bandit1.record_outcome("ci-fix", "haiku", "productive")

    bandit2 = PmModelBandit(state_dir=str(state_dir))
    assert bandit2.arms["pm-model:ci-fix:haiku"].mean > 0.5


def test_known_work_types(tmp_path):
    """known_work_types should return only types with observations."""
    state_dir = tmp_path / "pm-dispatch"
    bandit = PmModelBandit(state_dir=str(state_dir))
    assert len(bandit.known_work_types()) == 0

    bandit.record_outcome("ci-fix", "haiku", "productive")
    known = bandit.known_work_types()
    assert "ci-fix" in known
    assert "strategy-reply" not in known


def test_summary_empty(tmp_path):
    """Summary should be empty for a fresh bandit."""
    bandit = PmModelBandit(state_dir=str(tmp_path / "pm-dispatch"))
    assert bandit.summary() == {}


def test_summary_populated(tmp_path):
    """Summary should reflect recorded outcomes."""
    state_dir = tmp_path / "pm-dispatch"
    bandit = PmModelBandit(state_dir=str(state_dir))
    bandit.record_outcome("ci-fix", "haiku", "productive")
    bandit.record_outcome("ci-fix", "sonnet", "failed")

    summary = bandit.summary()
    assert "ci-fix" in summary
    assert "haiku" in summary["ci-fix"]
    assert "sonnet" in summary["ci-fix"]
    assert summary["ci-fix"]["haiku"]["mean"] > summary["ci-fix"]["sonnet"]["mean"]


def test_work_type_classification():
    """Verify that the set of work types covers PM dispatch needs."""
    # These should all be classified
    for wt in [
        "ci-fix",
        "greptile-fix",
        "pr-review",
        "merge-conflict",
        "strategy-reply",
        "issue-triage",
        "assigned-issue",
        "notification-triage",
    ]:
        assert wt in PM_WORK_TYPES, f"{wt} should be in PM_WORK_TYPES"
