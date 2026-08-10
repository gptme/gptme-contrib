"""Symptom tests for the failed-delivery rollback.

History that these tests exist to prevent repeating: the first port of
``rollback_failed_delivery`` counted redelivery attempts and skipped
``promote_item_state``, and stopped there. It passed its tests and changed
nothing in production, because the two effects it omitted each independently
defeat the rollback:

- the slot's ``.event`` fingerprint stayed stamped, suppressing the item for
  the full 6h TTL;
- pending ``notif-*`` state stayed in place, and
  ``promote_notification_states()`` at end of run promoted *every* pending
  ``notif-*.state`` — consuming exactly what the rollback claimed to preserve.

So these tests assert the observable end state on disk **after the end-of-run
promotion has also run**, not the fact that a function was called. A rollback
that is not visible after `promote_notification_states()` is not a rollback.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from gptme_runloops.run_item import (
    RunItemConfig,
    clear_slot_event_markers,
    max_redelivery_attempts,
    promote_item_state,
    promote_notification_states,
    purge_pending_notif_state,
    redelivery_attempts_file,
    rollback_failed_delivery,
    slot_safe_candidates,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunItemConfig:
    cooldown = tmp_path / "cooldown"
    cooldown.mkdir()
    monkeypatch.setenv("PM_DISPATCH_COOLDOWN_DIR", str(cooldown))
    monkeypatch.delenv("PM_SLOT_KEY", raising=False)
    monkeypatch.delenv("PM_MAX_REDELIVERY_ATTEMPTS", raising=False)
    return RunItemConfig(
        workspace=tmp_path,
        author="a",
        agent_name="Bob",
        operator_name="Erik",
        primary_repo="ErikBjare/bob",
        greptile_repos_pattern="^x$",
        self_merge_repos="",
        wait_merge_auto_enabled_repos="",
        state_dir=tmp_path / "state",
        pending_state_dir=tmp_path / "pending",
        lock_dir=tmp_path,
        lock_stem="s",
        lock_history=tmp_path / "lh.log",
        records_dir=tmp_path / "rec",
        dispatch_ledger=tmp_path / "d.jsonl",
        wait_merge_gate_log=tmp_path / "g.jsonl",
        backend_quota_dir=tmp_path / "bq",
        cc_projects_dir=tmp_path / "cc",
        cc_credentials_path=tmp_path / "c.json",
        copilot_state_dir=tmp_path / "cop",
        codex_sessions_dir=tmp_path / "cod",
        monitoring_rules_file=tmp_path / "rules.md",
    )


def cooldown_dir(config: RunItemConfig) -> Path:
    return Path(os.environ["PM_DISPATCH_COOLDOWN_DIR"])


def stage_failed_delivery(
    config: RunItemConfig,
    *,
    slot_stem: str = "gptme-gptme-3468",
    repo: str = "gptme/gptme",
    number: int = 3468,
    notif_id: str = "99887766",
) -> None:
    """The on-disk world at the moment a delivery fails."""
    cd = cooldown_dir(config)
    # The dispatcher stamped the fingerprint at LAUNCH.
    (cd / f"{slot_stem}.event").write_text("fingerprintabc\n")
    (cd / f"{slot_stem}.event_logged").write_text("1\n")
    # The activity gate wrote pending notification state for this item.
    config.pending_state_dir.mkdir(parents=True, exist_ok=True)
    (config.pending_state_dir / f"notif-{notif_id}.map").write_text(f"{repo}#{number}")
    (config.pending_state_dir / f"notif-{notif_id}.state").write_text("seen")


class TestRollbackSurvivesEndOfRunPromotion:
    """THE symptom test — the exact shape the half-port failed."""

    def test_rollback_is_still_intact_after_promote_notification_states(
        self, env: RunItemConfig
    ) -> None:
        stage_failed_delivery(env)
        cd = cooldown_dir(env)

        assert rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        # The line that silently undid the previous rollback:
        promote_notification_states(env)

        # 6h suppression lifted
        assert not (cd / "gptme-gptme-3468.event").exists()
        assert not (cd / "gptme-gptme-3468.event_logged").exists()
        # Pending notif state purged, so the blanket promotion cannot consume it
        assert not (env.pending_state_dir / "notif-99887766.state").exists()
        assert not (env.pending_state_dir / "notif-99887766.map").exists()
        # ... and crucially it did NOT reach the real state dir
        assert not (env.state_dir / "notif-99887766.state").exists()

    def test_rollback_is_surgical_and_spares_unrelated_items(
        self, env: RunItemConfig
    ) -> None:
        """Negative control: a blanket suppression would also 'pass' the test above."""
        stage_failed_delivery(env)
        (env.pending_state_dir / "notif-11112222.map").write_text("gptme/gptme#9999")
        (env.pending_state_dir / "notif-11112222.state").write_text("seen")

        rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        promote_notification_states(env)

        assert not (env.state_dir / "notif-99887766.state").exists()
        # The unrelated item must still be promoted normally.
        assert (env.state_dir / "notif-11112222.state").exists()

    def test_purge_matches_the_key_exactly_not_by_prefix(
        self, env: RunItemConfig
    ) -> None:
        # bash anchors the regex (^repo#number$); #346 must not match #3468.
        env.pending_state_dir.mkdir(parents=True, exist_ok=True)
        (env.pending_state_dir / "notif-a.map").write_text("gptme/gptme#346")
        (env.pending_state_dir / "notif-a.state").write_text("seen")
        (env.pending_state_dir / "notif-b.map").write_text("gptme/gptme#3468")
        (env.pending_state_dir / "notif-b.state").write_text("seen")

        assert purge_pending_notif_state(env, "gptme/gptme", 3468) == 1
        assert (env.pending_state_dir / "notif-a.state").exists()
        assert not (env.pending_state_dir / "notif-b.state").exists()


class TestEventMarkerSpellings:
    """The sanitizers disagree, and the disagreement silently no-ops bash."""

    def test_colon_slot_keys_get_every_spelling_cleared(
        self, env: RunItemConfig
    ) -> None:
        # A CI-check slot. The dispatcher writes the ':'-translated name
        # (tr '/#:' '---'); bash's rollback computes the ':'-preserving one
        # and its rm -f hits nothing.
        key = "ActivityWatch/aw-server-rust#master-ci:dependabot-auto-merge"
        cd = cooldown_dir(env)
        dispatcher_name = "ActivityWatch-aw-server-rust-master-ci-dependabot-auto-merge"
        (cd / f"{dispatcher_name}.event").write_text("fp\n")

        assert clear_slot_event_markers(env, key)
        assert not (cd / f"{dispatcher_name}.event").exists()

    def test_candidates_cover_both_writers(self) -> None:
        key = "a/b#master-ci:check"
        cands = slot_safe_candidates(key)
        assert "a-b-master-ci-check" in cands  # dispatcher, tr '/#:' '---'
        assert "a-b-master-ci:check" in cands  # bash rollback, only / and #

    def test_no_cooldown_dir_is_not_an_error(
        self, env: RunItemConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PM_DISPATCH_COOLDOWN_DIR", raising=False)
        assert clear_slot_event_markers(env, "a/b#1") is False

    def test_empty_slot_key_is_not_an_error(self, env: RunItemConfig) -> None:
        assert clear_slot_event_markers(env, "") is False


class TestLockBusyClearsEventMarker:
    """P5 — the highest-harm case: a dropped human message.

    A mention arriving while a long slot session holds the lock gets its
    fingerprint stamped at LAUNCH and then never delivered. Leaving the stamp
    suppresses it for 6h, and unlike a bot signal nothing retries it — the
    person assumes they were seen. Live bite 2026-08-05 on ErikBjare/bob#1127.
    """

    def test_lock_busy_run_clears_the_slot_event_marker(
        self, env: RunItemConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from gptme_runloops.run_item import RunItem, run_work_file

        cd = cooldown_dir(env)
        (cd / "gptme-gptme-3468.event").write_text("fp\n")
        (cd / "gptme-gptme-3468.event_logged").write_text("1\n")

        work_file = tmp_path / "slot.jsonl"
        item = RunItem.from_grouped_json(
            '{"repo": "gptme/gptme", "number": 3468, "title": "t", '
            '"detail": "d", "type": "pr_update", "types": ["pr_update"], '
            '"all_numbers": ["3468"]}'
        )
        work_file.write_text(item.raw_line + "\n")

        # Hold the lock so acquire() fails, exactly like a live sibling slot.
        from gptme_runloops.run_item import SlotLock, derive_lock_paths

        lock_file, _ = derive_lock_paths(env, "gptme/gptme#3468")
        holder = SlotLock(lock_file, "slot:gptme/gptme#3468", env.lock_history, "cc")
        assert holder.acquire()
        try:
            rc = run_work_file(
                work_file,
                env,
                __import__(
                    "gptme_runloops.run_item", fromlist=["RunItemHooks"]
                ).RunItemHooks(runner=["/fake/run.sh"]),
                backend="claude-code",
                slot_key="gptme/gptme#3468",
            )
        finally:
            holder.release()

        assert rc == 0
        # The symptom: the fingerprint must NOT survive a delivery-less launch.
        assert not (cd / "gptme-gptme-3468.event").exists()
        assert not (cd / "gptme-gptme-3468.event_logged").exists()

    def test_global_scope_lock_busy_touches_no_markers(
        self, env: RunItemConfig
    ) -> None:
        # Only slot-scoped runs own a fingerprint; a global lock-busy must not
        # clear anything.
        cd = cooldown_dir(env)
        (cd / "unrelated.event").write_text("fp\n")
        assert clear_slot_event_markers(env, "") is False
        assert (cd / "unrelated.event").exists()


class TestRedeliveryCounter:
    def test_counter_lives_in_the_cooldown_dir_under_the_bash_name(
        self, env: RunItemConfig
    ) -> None:
        # Must match bash _redelivery_attempts_file, or the two executors keep
        # separate counters and an item gets double its redelivery budget.
        path = redelivery_attempts_file(env, "gptme/gptme", 3468)
        assert path is not None
        assert path.parent == cooldown_dir(env)
        assert path.name == "redeliver-gptme-gptme-3468.attempts"

    def test_counter_is_none_without_a_cooldown_dir(
        self, env: RunItemConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PM_DISPATCH_COOLDOWN_DIR", raising=False)
        assert redelivery_attempts_file(env, "a/b", 1) is None

    def test_rollback_without_cooldown_dir_fails_toward_redelivery(
        self, env: RunItemConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bash: "no durable place to count" -> redeliver (ErikBjare/bob#1127).
        monkeypatch.delenv("PM_DISPATCH_COOLDOWN_DIR", raising=False)
        assert rollback_failed_delivery(env, "a/b", 1, "a/b#1") is True

    def test_cap_is_honoured_then_resets(self, env: RunItemConfig) -> None:
        path = redelivery_attempts_file(env, "gptme/gptme", 3468)
        assert path is not None
        assert rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        assert path.read_text() == "1"
        assert rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        assert path.read_text() == "2"
        # Third attempt exceeds the default cap of 2 -> promote instead.
        assert not rollback_failed_delivery(
            env, "gptme/gptme", 3468, "gptme/gptme#3468"
        )
        # ... and the counter resets so a later genuine failure gets a full budget.
        assert not path.exists()

    def test_cap_honours_the_env_var(
        self, env: RunItemConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PM_MAX_REDELIVERY_ATTEMPTS", "1")
        assert max_redelivery_attempts() == 1
        assert rollback_failed_delivery(env, "a/b", 1, "a/b#1")
        assert not rollback_failed_delivery(env, "a/b", 1, "a/b#1")

    def test_garbage_env_falls_back_to_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PM_MAX_REDELIVERY_ATTEMPTS", "not-a-number")
        assert max_redelivery_attempts() == 2

    def test_promote_item_state_resets_the_counter(self, env: RunItemConfig) -> None:
        # bash lib.sh:928-929. Without this the retry budget degrades
        # monotonically until every failure promotes immediately.
        path = redelivery_attempts_file(env, "gptme/gptme", 3468)
        assert path is not None
        rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        assert path.exists()
        promote_item_state(env, "gptme/gptme", 3468)
        assert not path.exists()

    def test_corrupt_counter_does_not_crash_the_rollback(
        self, env: RunItemConfig
    ) -> None:
        path = redelivery_attempts_file(env, "gptme/gptme", 3468)
        assert path is not None
        path.write_text("garbage")
        assert rollback_failed_delivery(env, "gptme/gptme", 3468, "gptme/gptme#3468")
        assert path.read_text() == "1"
