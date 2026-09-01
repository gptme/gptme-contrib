"""Tests for post_session context_tier plumbing and signal fallbacks."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_sessions.deliverables import looks_like_sha
from gptme_sessions.post_session import PostSessionResult, post_session
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore

# gptme_sessions/__init__.py re-exports 'post_session' (function), shadowing the
# submodule attribute on the package.  `import gptme_sessions.post_session as mod`
# resolves via getattr(gptme_sessions, 'post_session') → the function, not the module.
# from gptme_sessions.post_session import post_session ensures the module is in
# sys.modules, so we can retrieve it directly for patch.object calls.
_post_session_mod = sys.modules["gptme_sessions.post_session"]


def test_post_session_result_route_metadata_does_not_shift_positional_fields():
    """Additive result fields preserve the established constructor prefix."""
    result = PostSessionResult(SessionRecord(), None, None, 123, 45, 67)

    assert result.token_count == 123
    assert result.input_tokens == 45
    assert result.output_tokens == 67
    assert result.provider is None


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


def test_post_session_persists_pi_route_metadata(tmp_path: Path):
    """Pi provider/model/cost/stop metadata reaches the canonical store."""
    store = SessionStore(sessions_dir=tmp_path)
    trajectory = Path(__file__).parent / "fixtures" / "pi" / "productive-codex.jsonl"

    result = post_session(
        store=store,
        harness="pi",
        model="unknown",
        trajectory_path=trajectory,
    )

    assert result.provider == "openai-codex"
    assert result.record.provider == "openai-codex"
    assert result.record.model == "gpt-5.6-luna"
    assert result.stop_reason == "stop"
    assert result.record.stop_reason == "stop"
    assert result.cost_usd == pytest.approx(0.0004264)
    assert result.record.cost_usd == pytest.approx(0.0004264)

    persisted = SessionStore(sessions_dir=tmp_path).load_all()
    assert len(persisted) == 1
    assert persisted[0].provider == "openai-codex"
    assert persisted[0].model == "gpt-5.6-luna"
    assert persisted[0].stop_reason == "stop"
    assert persisted[0].cost_usd == pytest.approx(0.0004264)


@pytest.mark.parametrize(
    ("reported_cost", "expected_cost"),
    [(0.0, 0.0), (None, None)],
)
def test_post_session_preserves_zero_vs_missing_cost(
    tmp_path: Path, reported_cost: float | None, expected_cost: float | None
):
    """A reported zero remains distinct from unavailable cost data."""
    store = SessionStore(sessions_dir=tmp_path)
    trajectory = tmp_path / "trajectory.jsonl"
    trajectory.touch()
    usage = {"model": "grok-4.6"}
    if reported_cost is not None:
        usage["cost"] = reported_cost

    with patch.object(
        _post_session_mod,
        "extract_from_path",
        return_value={"productive": True, "usage": usage},
    ):
        result = post_session(
            store=store,
            harness="pi",
            model="unknown",
            trajectory_path=trajectory,
        )

    assert result.cost_usd == expected_cost
    assert result.record.cost_usd == expected_cost
    assert SessionStore(sessions_dir=tmp_path).load_all()[0].cost_usd == expected_cost


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


SESSION_73BE_SIBLING_SHAS = [
    "b59e7f7f48353205eb6adbde9f06094b9dbf9f35",
    "bd825297b9f934991db6a1010797748ce5761771",
    "56e141c4ea533d96a1bc47d55a397091323c11aa",
    "d25a6a15376a38b60a97af4b56318d424d76624f",
    "d2c9af6be5ee79940876b4efcf4033250c7fe7ed",
    "5a2abcb1bbe170422e80b51c2d9cb7573ea0c8c9",
]


def test_post_session_file_only_trajectory_drops_untagged_caller_shas(tmp_path: Path):
    """File-only trajectory + untagged shared-range SHAs must not credit siblings.

    Session 73be kept six untagged caller SHAs as fallback_observed with
    reason=trajectory_has_no_sha. Those SHAs are ambiguous, not owned. The
    session's own file edits stay visible.
    """
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")
    own_file = "/tmp/dashboard-smoke-73be.js"

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [own_file],
        "deliverable_details": [
            {
                "value": own_file,
                "kind": "file",
                "provenance_class": "tool_authored",
                "evidence": {"source": "trajectory", "tool_name": "save"},
            }
        ],
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="gptme",
            model="gpt-5.5",
            session_id="73be",
            duration_seconds=3000,
            exit_code=124,
            trajectory_path=fake_traj,
            deliverables=list(SESSION_73BE_SIBLING_SHAS),
        )

    assert result.record.deliverables == [own_file]
    assert all(not looks_like_sha(d) for d in result.record.deliverables)
    for sha in SESSION_73BE_SIBLING_SHAS:
        assert sha not in result.record.deliverables
    assert result.record.outcome == "productive"


def test_post_session_file_only_trajectory_keeps_trailer_owned_sha(tmp_path: Path):
    """A matching Git-Session-Id trailer still owns the commit (A3)."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")
    own_file = "scripts/vitals-portal.py"
    own_sha = "abc1234567890abcdef1234567890abcdef1234"
    sibling_sha = "deadbeef01234567012345670123456701234567"

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [own_file],
        "deliverable_details": [
            {
                "value": own_file,
                "kind": "file",
                "provenance_class": "tool_authored",
                "evidence": {"source": "trajectory", "tool_name": "patch"},
            }
        ],
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="gptme",
            model="sonnet",
            session_id="session-mine",
            duration_seconds=60,
            trajectory_path=fake_traj,
            deliverables=[own_sha, sibling_sha],
            commit_trailers={own_sha: ["session-mine"]},
        )

    assert result.record.deliverables == [own_file, own_sha]
    assert sibling_sha not in result.record.deliverables
    assert result.record.deliverable_details[-1]["provenance_class"] == "session_trailer_owned"


def test_post_session_73be_fixture_rejects_sibling_shas_and_keeps_files(tmp_path: Path):
    """A5: the real 73be SHA list cannot be owned evidence."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")
    files = [
        "/home/bob/bob/packages/metaproductivity/src/metaproductivity/vitals/dashboard_routes.py",
        "/tmp/dashboard-smoke-73be.js",
    ]
    fake_signals = {
        "session_duration_s": 3000,
        "productive": True,
        "deliverables": files,
        "deliverable_details": [
            {
                "value": path,
                "kind": "file",
                "provenance_class": "tool_authored",
                "evidence": {"source": "trajectory", "tool_name": "save"},
            }
            for path in files
        ],
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="gptme",
            model="gpt-5.5",
            session_id="73be",
            duration_seconds=3009,
            exit_code=124,
            trajectory_path=fake_traj,
            deliverables=list(SESSION_73BE_SIBLING_SHAS) + files,
        )

    assert result.record.deliverables == files
    owned_text = " ".join(result.record.deliverables)
    assert "1577" not in owned_text
    assert all(sha not in result.record.deliverables for sha in SESSION_73BE_SIBLING_SHAS)


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


def test_post_session_no_trajectory_flip_records_reason(tmp_path: Path):
    """The noop/unknown -> productive promotion must be auditable, not silent."""
    store = SessionStore(sessions_dir=tmp_path)

    result = post_session(
        store=store,
        harness="gptme",
        model="opus",
        duration_seconds=60,
        deliverables=["abc1234567890abcdef1234567890abcdef1234"],
    )

    assert result.record.outcome == "productive"
    reason = result.record.outcome_flip_reason
    assert reason is not None
    assert reason.startswith("no_trajectory_caller_deliverables:")
    assert "->productive" in reason


def test_post_session_unflipped_outcome_has_no_flip_reason(tmp_path: Path):
    """No flip happened, so nothing is stamped -- absence stays meaningful."""
    store = SessionStore(sessions_dir=tmp_path)

    result = post_session(
        store=store,
        harness="gptme",
        model="opus",
        duration_seconds=60,
    )

    assert result.record.outcome_flip_reason is None


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


def test_post_session_format_blind_trajectory_keeps_caller_deliverables(tmp_path: Path):
    """A trajectory with zero parsed tool calls over a substantial duration is
    treated as extractor format-blindness, not a genuine noop, when the
    caller has real git-range commits to fall back on.

    Reproduces the 2026-07-15 gpt-5.6-sol/gpt-5.6-terra false-noop cluster:
    Codex's newer custom_tool_call "exec" shape (JS-wrapped exec_command)
    wasn't recognized by the extractor, which reported tool_calls={} for a
    trajectory that fully covered the session's wall-clock and said noop,
    silently dropping real commits.
    """
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 600,
        "tool_calls": {},
        "productive": False,
        "deliverables": [],
    }
    real_sha = "abc1234567890abcdef1234567890abcdef1234"
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="codex",
            model="gpt-5.6-sol",
            duration_seconds=620,  # trajectory covers ~97% — duration-reliable
            trajectory_path=fake_traj,
            deliverables=[real_sha],
        )

    assert result.record.outcome == "productive"
    assert result.record.deliverables == [real_sha]
    # The flip reason must name the real cause (format blindness), not the
    # duration-unreliable cause — these are distinct mechanisms and audit
    # consumers need to tell them apart.
    assert result.record.outcome_flip_reason == "format_blind_trajectory_caller_deliverables"


def test_post_session_nonzero_tool_calls_still_drops_caller_deliverables(tmp_path: Path, caplog):
    """Inverse of the format-blind guard: when the trajectory DID parse tool
    calls (non-empty tool_calls) and says noop, it remains an authoritative
    noop — caller git-range commits (likely from a concurrent session) are
    still dropped.

    Also locks the operator-facing ``no-deliverables-with-commits`` marker so
    the diagnostic payload cannot silently regress to the generic concurrent-
    commit drop warning.
    """
    import logging

    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 600,
        "tool_calls": {"exec_command": 3},
        "productive": False,
        "deliverables": [],
    }
    concurrent_sha = "deadbeef" + "01234567" * 4
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        with caplog.at_level(logging.WARNING, logger="gptme_sessions.post_session"):
            result = post_session(
                store=store,
                harness="codex",
                model="gpt-5.6-sol",
                duration_seconds=620,
                trajectory_path=fake_traj,
                deliverables=[concurrent_sha],
            )

    assert result.record.outcome == "noop"
    assert result.record.deliverables == []
    named = [r.message for r in caplog.records if "no-deliverables-with-commits" in r.message]
    assert named, "extraction-gap warning marker must stay in operator logs"
    assert "exec_command" in named[0]
    assert "signal-extraction gap" in named[0]
    assert not any(
        "Dropping" in r.message and "concurrent session" in r.message for r in caplog.records
    )


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
