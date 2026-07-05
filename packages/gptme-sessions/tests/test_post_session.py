"""Tests for post_session context_tier plumbing and signal fallbacks."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore

# gptme_sessions/__init__.py re-exports 'post_session' (function), shadowing the
# submodule attribute on the package.  `import gptme_sessions.post_session as mod`
# resolves via getattr(gptme_sessions, 'post_session') → the function, not the module.
# from gptme_sessions.post_session import post_session ensures the module is in
# sys.modules, so we can retrieve it directly for patch.object calls.
_post_session_mod = sys.modules["gptme_sessions.post_session"]


def test_post_session_context_tier(tmp_path: Path):
    """context_tier is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="massive",
        duration_seconds=120,
    )
    assert result.record.context_tier == "massive"

    # Verify it persists through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].context_tier == "massive"


def test_post_session_context_tier_none(tmp_path: Path):
    """context_tier defaults to None when not provided."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=60,
    )
    assert result.record.context_tier is None


def test_post_session_context_tier_standard(tmp_path: Path):
    """context_tier='standard' is stored correctly."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="standard",
        duration_seconds=90,
    )
    assert result.record.context_tier == "standard"


def test_post_session_ab_group_tier_version(tmp_path: Path):
    """ab_group and tier_version are stored in SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="massive",
        ab_group="treatment",
        tier_version="v2",
        duration_seconds=120,
    )
    assert result.record.ab_group == "treatment"
    assert result.record.tier_version == "v2"

    # Verify they persist through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].ab_group == "treatment"
    assert records[0].tier_version == "v2"


def test_post_session_ab_group_tier_version_none(tmp_path: Path):
    """ab_group and tier_version default to None when not provided."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=60,
    )
    assert result.record.ab_group is None
    assert result.record.tier_version is None


def test_post_session_selector_mode(tmp_path: Path):
    """selector_mode is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        selector_mode="llm-context",
        recommended_category="cleanup",
        duration_seconds=60,
    )
    assert result.record.selector_mode == "llm-context"
    assert result.record.recommended_category == "cleanup"

    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].selector_mode == "llm-context"
    assert records[0].recommended_category == "cleanup"


def test_post_session_selector_mode_none(tmp_path: Path):
    """selector_mode defaults to None when not provided."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=30,
    )
    assert result.record.selector_mode is None


def test_post_session_backfills_session_name_and_project_from_trajectory(tmp_path: Path):
    """Trajectory metadata should populate session_name/project when available."""
    store = SessionStore(sessions_dir=tmp_path)
    traj_dir = tmp_path / ".codex" / "sessions" / "2026" / "05" / "18"
    traj_dir.mkdir(parents=True)
    fake_traj = traj_dir / "12345678-abcdef.jsonl"
    fake_traj.write_text('{"timestamp":"2026-05-18T04:00:00Z","payload":{"cwd":"/home/bob/bob"}}\n')

    result = post_session(
        store=store,
        harness="codex",
        model="gpt-5.4",
        duration_seconds=30,
        trajectory_path=fake_traj,
    )

    assert result.record.session_name == "12345678"
    assert result.record.project == "/home/bob/bob"


def test_post_session_ab_group_invalid(tmp_path: Path):
    """post_session raises ValueError for invalid ab_group values."""
    store = SessionStore(sessions_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid ab_group"):
        post_session(
            store=store,
            harness="claude-code",
            model="opus",
            ab_group="invalid-group",
            duration_seconds=60,
        )


def test_post_session_duration_fallback_from_signals(tmp_path: Path):
    """duration_seconds falls back to session_duration_s from trajectory signals when 0."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")  # must exist for signal extraction to run

    fake_signals = {"session_duration_s": 300, "productive": True, "deliverables": []}
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )
    assert result.record.duration_seconds == 300


def test_post_session_exit_code_persisted(tmp_path: Path):
    """exit_code is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        exit_code=124,
        duration_seconds=3000,
    )
    assert result.record.exit_code == 124

    # Verify it persists through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].exit_code == 124


def test_post_session_exit_code_defaults_zero(tmp_path: Path):
    """exit_code defaults to 0 when not specified."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="opus",
        duration_seconds=60,
    )
    assert result.record.exit_code == 0


def test_post_session_model_fallback_from_signals(tmp_path: Path):
    """model falls back to usage.model from trajectory signals when 'unknown'."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {"model": "claude-sonnet-4-6", "total_tokens": 1000},
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )
    assert result.record.model == "claude-sonnet-4-6"


def test_post_session_populates_productivity_grade(tmp_path: Path):
    """post_session mirrors the scalar trajectory grade into grades.productivity."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": ["feat: ship thing (abc1234)"],
        "grade": 0.68,
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.record.trajectory_grade == 0.68
    assert result.record.grades == {"productivity": 0.68}

    records = store.load_all()
    assert records[0].grades == {"productivity": 0.68}


def test_post_session_persists_usage_fields(tmp_path: Path):
    """Trajectory usage totals should be written into the canonical session record."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_creation_tokens": 30,
            "cache_read_tokens": 600,
            "total_tokens": 795,
            "sys_prompt_tokens": 150,
            "context_peak_tokens": 750,
            "context_window": 200000,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.token_count == 795
    assert result.record.input_tokens == 120
    assert result.record.output_tokens == 45
    assert result.record.cache_creation_tokens == 30
    assert result.record.cache_read_tokens == 600
    assert result.record.sys_prompt_tokens == 150
    assert result.record.context_peak_tokens == 750
    assert result.record.context_window == 200000

    records = store.load_all()
    assert records[0].input_tokens == 120
    assert records[0].output_tokens == 45
    assert records[0].cache_creation_tokens == 30
    assert records[0].cache_read_tokens == 600
    assert records[0].sys_prompt_tokens == 150
    assert records[0].context_peak_tokens == 750
    assert records[0].context_window == 200000


def test_post_session_partial_usage_fields_stored_as_none(tmp_path: Path):
    """Absent usage sub-fields must be None, not 0, to avoid corrupting analytics."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    # Older trajectory format: only total_tokens present, no breakdown keys
    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "total_tokens": 500,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.token_count == 500
    # Absent breakdown keys must be None, not 0
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cache_creation_tokens is None
    assert result.cache_read_tokens is None

    records = store.load_all()
    assert records[0].input_tokens is None
    assert records[0].output_tokens is None
    assert records[0].cache_creation_tokens is None
    assert records[0].cache_read_tokens is None


def test_post_session_preserves_zero_token_usage(tmp_path: Path):
    """Zero-token trajectories are stored as token_count=0, not dropped as missing."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": False,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            exit_code=1,
        )

    assert result.token_count == 0
    assert result.record.token_count == 0

    records = store.load_all()
    assert records[0].token_count == 0


def test_post_session_category_none_when_no_signals(tmp_path: Path):
    """Category defaults to None when no explicit category and no trajectory."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        duration_seconds=120,
        run_type="operator",
    )
    assert result.record.category is None

    records = store.load_all()
    assert records[0].category is None


def test_post_session_explicit_category(tmp_path: Path):
    """Explicit category is stored when passed (operator fix regression test)."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        duration_seconds=120,
        run_type="operator",
        category="monitoring",
    )
    assert result.record.category == "monitoring"

    records = store.load_all()
    assert records[0].category == "monitoring"


def test_post_session_inferred_category_from_signals(tmp_path: Path):
    """Category falls back to inferred_category from trajectory signals when no explicit category."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 120,
        "productive": True,
        "deliverables": ["fix: thing (abc1234)"],
        "inferred_category": "infrastructure",
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )
    assert result.record.category == "infrastructure"

    records = store.load_all()
    assert records[0].category == "infrastructure"


def test_post_session_explicit_category_overrides_inferred(tmp_path: Path):
    """Explicit category takes priority over inferred_category from signals."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 120,
        "productive": True,
        "deliverables": ["fix: thing (abc1234)"],
        "inferred_category": "infrastructure",
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            category="code",
        )
    assert result.record.category == "code"

    records = store.load_all()
    assert records[0].category == "code"


def test_post_session_cascade_intent(tmp_path: Path):
    """cascade_intent is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    cascade_intent = {
        "reasons": ["recent CI failure", "priority score"],
        "constraints": ["avoid social work"],
    }

    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        cascade_intent=cascade_intent,
        duration_seconds=120,
    )
    assert result.record.cascade_intent == cascade_intent

    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].cascade_intent == cascade_intent


# ---------------------------------------------------------------------------
# Trajectory-authoritative deliverable attribution (cross-session contamination)
# ---------------------------------------------------------------------------


def test_post_session_trajectory_deliverables_take_precedence(tmp_path: Path):
    """When trajectory has deliverables, git-range commits absent from the
    trajectory are dropped (cross-session contamination filter)."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    traj_deliverable = "fix: something this session did (abc1234)"
    # git-range supplies this session's commit plus two concurrent-session commits
    concurrent_sha1 = "deadbeef" + "01234567" * 4
    concurrent_sha2 = "cafebabe" + "01234567" * 4

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [traj_deliverable],
        "deliverable_details": [
            {
                "value": traj_deliverable,
                "kind": "commit",
                "provenance_class": "session_committed",
                "evidence": {"source": "trajectory", "tool_name": "Bash"},
            }
        ],
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            deliverables=[
                "abc1234567890abcdef1234567890abcdef1234",  # this session
                concurrent_sha1,
                concurrent_sha2,
            ],
        )

    # Validated caller SHA is now merged with trajectory deliverables
    assert len(result.record.deliverables) == 2
    assert result.record.deliverables[0] == traj_deliverable
    assert result.record.deliverables[1] == "abc1234567890abcdef1234567890abcdef1234"
    assert result.record.deliverable_details == [
        {
            "value": traj_deliverable,
            "kind": "commit",
            "provenance_class": "session_committed",
            "evidence": {"source": "trajectory", "tool_name": "Bash"},
        },
        {
            "value": "abc1234567890abcdef1234567890abcdef1234",
            "kind": "commit",
            "provenance_class": "session_committed",
            "evidence": {"source": "caller", "validation": "trajectory_sha_prefix"},
        },
    ]


def test_post_session_trajectory_commit_validation_keeps_non_sha_caller_deliverables(
    tmp_path: Path,
):
    """Trajectory SHA validation should not drop caller PR/file deliverables."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    traj_deliverable = "fix: something this session did (abc1234)"
    caller_pr = "https://github.com/gptme/gptme-contrib/pull/944"
    caller_file = "packages/gptme-sessions/src/gptme_sessions/post_session.py"
    concurrent_sha = "deadbeef" + "01234567" * 4

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [traj_deliverable],
        "deliverable_details": [
            {
                "value": traj_deliverable,
                "kind": "commit",
                "provenance_class": "session_committed",
                "evidence": {"source": "trajectory", "tool_name": "Bash"},
            }
        ],
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            deliverables=[
                "abc1234567890abcdef1234567890abcdef1234",
                caller_pr,
                caller_file,
                concurrent_sha,
            ],
        )

    assert result.record.deliverables == [
        traj_deliverable,
        "abc1234567890abcdef1234567890abcdef1234",
        caller_pr,
        caller_file,
    ]
    assert result.record.deliverable_details == [
        {
            "value": traj_deliverable,
            "kind": "commit",
            "provenance_class": "session_committed",
            "evidence": {"source": "trajectory", "tool_name": "Bash"},
        },
        {
            "value": "abc1234567890abcdef1234567890abcdef1234",
            "kind": "commit",
            "provenance_class": "session_committed",
            "evidence": {"source": "caller", "validation": "trajectory_sha_prefix"},
        },
        {
            "value": caller_pr,
            "kind": "pull_request",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "caller", "reason": "non_sha_passthrough"},
        },
        {
            "value": caller_file,
            "kind": "file",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "caller", "reason": "non_sha_passthrough"},
        },
    ]


def test_post_session_caller_only_deliverables_when_no_trajectory(tmp_path: Path):
    """Without a trajectory, caller-supplied (git-range) deliverables are used
    as-is and upgrade outcome from noop/unknown to productive."""
    store = SessionStore(sessions_dir=tmp_path)

    result = post_session(
        store=store,
        harness="gptme",
        model="opus",
        duration_seconds=60,
        deliverables=["abc1234567890abcdef1234567890abcdef1234"],
    )

    assert len(result.record.deliverables) == 1
    assert result.record.outcome == "productive"
    assert result.record.deliverable_details == [
        {
            "value": "abc1234567890abcdef1234567890abcdef1234",
            "kind": "commit",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "caller", "reason": "no_trajectory"},
        }
    ]


def test_post_session_caller_deliverables_no_outcome_override_when_traj_noop(tmp_path: Path):
    """Git-range commits must NOT upgrade outcome when trajectory determined noop.
    Prevents concurrent-session commits from inflating session classification."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": False,
        "deliverables": [],
    }
    concurrent_sha = "deadbeef" + "01234567" * 4
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            deliverables=[concurrent_sha],
        )

    assert result.record.outcome == "noop"
    assert result.record.deliverables == []
    assert result.record.deliverable_details == []


def test_post_session_unreliable_trajectory_keeps_caller_deliverables(tmp_path: Path):
    """A trajectory covering far less wall-clock than the session duration is
    treated as unreliable (truncated/misattributed). Its noop verdict must NOT
    drop the caller's real git-range commits or record a false noop.

    Reproduces ErikBjare/bob session 36d9: two concurrent gptme sessions
    resolved to the same log dir, so 36d9's 1158s run was assigned 026d's 214s
    noop trajectory, dropping 36d9's real commits and recording a false noop.
    """
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    # Trajectory says noop and covers only 214s of wall-clock...
    fake_signals = {
        "session_duration_s": 214,
        "productive": False,
        "deliverables": [],
    }
    real_sha = "abc1234567890abcdef1234567890abcdef1234"
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="gptme",
            model="deepseek-v4-flash",
            # ...but the session actually ran 1158s and made a real commit.
            duration_seconds=1158,
            trajectory_path=fake_traj,
            deliverables=[real_sha],
        )

    assert result.record.outcome == "productive"
    assert result.record.deliverables == [real_sha]
    assert result.record.deliverable_details == [
        {
            "value": real_sha,
            "kind": "commit",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "caller", "reason": "trajectory_unreliable"},
        }
    ]


def test_post_session_unreliable_trajectory_no_caller_deliverables_records_unknown(
    tmp_path: Path, caplog
):
    """When trajectory is unreliable (truncated/misattributed) and says noop,
    but no caller deliverables exist either, record unknown — don't penalize
    the backend with a false noop."""
    import logging

    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 214,  # 3.5 minutes
        "productive": False,
        "deliverables": [],
    }
    with (
        patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals),
        caplog.at_level(logging.INFO),
    ):
        result = post_session(
            store=store,
            harness="gptme",
            model="opus",
            duration_seconds=1158,  # 19 min real session
            trajectory_path=fake_traj,
            deliverables=[],  # no caller commits either
        )

    assert result.record.outcome == "unknown"
    assert result.record.deliverables == []


def test_post_session_reliable_trajectory_still_drops_concurrent_commits(tmp_path: Path):
    """Guard regression: when the trajectory span matches the session duration,
    a trajectory-determined noop still drops caller git-range commits, so the
    concurrent-session contamination filter stays intact."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 600,
        "productive": False,
        "deliverables": [],
    }
    concurrent_sha = "deadbeef" + "01234567" * 4
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="gptme",
            model="opus",
            duration_seconds=620,  # trajectory covers ~97% — reliable
            trajectory_path=fake_traj,
            deliverables=[concurrent_sha],
        )

    assert result.record.outcome == "noop"
    assert result.record.deliverables == []


def test_post_session_trajectory_empty_deliverables_keeps_caller_when_productive(
    tmp_path: Path, caplog
):
    """When trajectory ran but found no deliverables yet says productive,
    caller (git-range) commits are KEPT — trajectory couldn't validate
    or contradict the caller's evidence."""
    import logging

    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
    }
    caller_sha = "abc1234567890abcdef1234567890abcdef1234"
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        with caplog.at_level(logging.WARNING, logger="gptme_sessions.post_session"):
            result = post_session(
                store=store,
                harness="gptme",
                model="opus",
                duration_seconds=0,
                trajectory_path=fake_traj,
                deliverables=[caller_sha],
            )

    assert result.record.deliverables == [caller_sha]
    assert result.record.deliverable_details == [
        {
            "value": caller_sha,
            "kind": "commit",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "caller", "reason": "trajectory_empty"},
        }
    ]
    assert any(
        "Trajectory ran but found no deliverables; keeping" in r.message for r in caplog.records
    )


def test_extract_traj_sha_prefixes():
    """_extract_traj_sha_prefixes correctly parses trajectory commit strings."""
    from gptme_sessions.post_session import _extract_traj_sha_prefixes

    entries = [
        "fix: something good (abc1234)",
        "feat: another thing (dead123)",
        "/some/file/path.py",
        "plain text without parens",
        "bad (notasha!)",
        "PR merge (12345678abcd)",
    ]
    result = _extract_traj_sha_prefixes(entries)
    assert "abc1234" in result
    assert "dead123" in result
    assert "12345678abcd" in result
    assert len(result) == 3


def test_caller_sha_in_traj():
    """_caller_sha_in_traj matches full SHA against 7-char trajectory prefixes."""
    from gptme_sessions.post_session import _caller_sha_in_traj

    prefixes = {"abc1234", "dead123"}
    assert _caller_sha_in_traj("abc1234567890abcdef1234567890abcdef1234", prefixes) is True
    assert _caller_sha_in_traj("dead123456789abcdef0000000000000000000", prefixes) is True
    assert _caller_sha_in_traj("cafe000000000000000000000000000000000", prefixes) is False
    assert _caller_sha_in_traj("ABC1234567890ABCDEF", prefixes) is True  # case-insensitive


def test_post_session_populates_smell_score(tmp_path: Path):
    """post_session computes a smell_score from the journal prose and persists it."""
    store = SessionStore(sessions_dir=tmp_path)
    journal = tmp_path / "session.md"
    journal.write_text(
        "It's worth noting that this is a testament to our ever-evolving "
        "tapestry of solutions. Let's delve into the realm of possibilities. "
        "It's not just a feature, it's a game-changer. In conclusion, I'd be "
        "happy to help. Great question! Moreover, this showcases a comprehensive "
        "approach.",
        encoding="utf-8",
    )

    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        duration_seconds=60,
        journal_path=str(journal),
    )

    assert result.record.smell_score is not None
    assert 0.0 < result.record.smell_score <= 1.0

    # Persists through store reload.
    records = SessionStore(sessions_dir=tmp_path).load_all()
    assert records[0].smell_score == result.record.smell_score


def test_post_session_smell_score_none_without_journal(tmp_path: Path):
    """No journal_path means smell_score stays None (no crash)."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        duration_seconds=60,
    )
    assert result.record.smell_score is None


def test_post_session_smell_score_zero_for_clean_journal(tmp_path: Path):
    """Clean technical prose with no LLM-smell hits produces smell_score=0.0, not None."""
    store = SessionStore(sessions_dir=tmp_path)
    journal = tmp_path / "session.md"
    journal.write_text(
        "Fixed the IndexError in parse_tokens by checking slice bounds before access. "
        "Added a unit test covering the empty-list path. "
        "CI passes on Python 3.10, 3.11, and 3.12. Pushed the fix.",
        encoding="utf-8",
    )

    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        duration_seconds=60,
        journal_path=str(journal),
    )

    assert result.record.smell_score == 0.0

    # Persists through store reload — 0.0 is stored, not collapsed to None.
    records = SessionStore(sessions_dir=tmp_path).load_all()
    assert records[0].smell_score == 0.0
