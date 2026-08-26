"""Tests for gptme_bob_status provider."""

from __future__ import annotations

from gptme_bob_status.provider import BobStatusProvider, make_provider


def test_provider_satisfies_protocol():
    """BobStatusProvider satisfies the StatusProvider protocol."""
    provider = make_provider()
    # Use structural check to avoid dependency on @runtime_checkable decoration
    # in gptme's StatusProvider (which may vary across versions).
    assert hasattr(provider, "collect") and hasattr(provider, "narrative_sections")


def test_provider_name():
    provider = make_provider()
    assert provider.name == "bob"


def test_collect_returns_empty_outside_bob_workspace(monkeypatch, tmp_path):
    """Outside Bob's workspace, collect() returns an empty dict."""
    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_is_bob_workspace", lambda: False)
    provider = BobStatusProvider()
    result = provider.collect()
    assert result == {}


def test_narrative_sections_returns_empty_outside_bob_workspace(monkeypatch):
    """Outside Bob's workspace, narrative_sections() returns an empty list."""
    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_is_bob_workspace", lambda: False)
    provider = BobStatusProvider()
    result = provider.narrative_sections()
    assert result == []


def test_collect_returns_expected_keys_in_bob_workspace(monkeypatch):
    """Inside Bob's workspace, collect() returns the expected key set."""
    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_is_bob_workspace", lambda: True)
    monkeypatch.setattr(mod, "_active_tasks", lambda n=3: [])
    monkeypatch.setattr(mod, "_pr_queue", lambda: [])
    monkeypatch.setattr(mod, "_service_status", lambda: [])
    monkeypatch.setattr(mod, "_dead_timers", lambda: 0)
    monkeypatch.setattr(mod, "_blockers", lambda limit=3: [])
    monkeypatch.setattr(mod, "_ready_tasks", lambda limit=3: [])
    monkeypatch.setattr(mod, "_journal_entries", lambda limit=5: [])

    provider = BobStatusProvider()
    result = provider.collect()

    expected_keys = {
        "bob_active_tasks",
        "bob_pr_queue",
        "bob_services",
        "bob_dead_timers",
        "bob_blockers",
        "bob_ready_tasks",
        "bob_journal_entries",
    }
    assert set(result.keys()) == expected_keys


def test_narrative_sections_non_empty_in_bob_workspace(monkeypatch):
    """Inside Bob's workspace, narrative_sections() returns non-empty list."""
    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_is_bob_workspace", lambda: True)
    monkeypatch.setattr(
        mod, "_active_tasks", lambda n=3: [{"id": "t1", "title": "Test task"}]
    )
    monkeypatch.setattr(
        mod, "_pr_queue", lambda: [{"repo": "gptme/gptme", "count": 2, "watermark": 10}]
    )
    monkeypatch.setattr(
        mod,
        "_service_status",
        lambda: [{"label": "Autonomous", "icon": "✓", "status": "active"}],
    )
    monkeypatch.setattr(mod, "_dead_timers", lambda: 0)
    monkeypatch.setattr(mod, "_blockers", lambda limit=3: [])
    monkeypatch.setattr(mod, "_ready_tasks", lambda limit=3: [])
    monkeypatch.setattr(mod, "_journal_entries", lambda limit=5: [])

    provider = BobStatusProvider()
    sections = provider.narrative_sections()

    assert isinstance(sections, list)
    assert len(sections) > 0
    assert all(isinstance(s, str) for s in sections)
    # Should include the active task
    combined = "\n".join(sections)
    assert "t1" in combined
    assert "gptme/gptme" in combined


def test_collect_keys_use_bob_prefix(monkeypatch):
    """All keys returned by collect() use the bob_ prefix (avoids collisions)."""
    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_is_bob_workspace", lambda: True)
    monkeypatch.setattr(mod, "_active_tasks", lambda n=3: [])
    monkeypatch.setattr(mod, "_pr_queue", lambda: [])
    monkeypatch.setattr(mod, "_service_status", lambda: [])
    monkeypatch.setattr(mod, "_dead_timers", lambda: 0)
    monkeypatch.setattr(mod, "_blockers", lambda limit=3: [])
    monkeypatch.setattr(mod, "_ready_tasks", lambda limit=3: [])
    monkeypatch.setattr(mod, "_journal_entries", lambda limit=5: [])

    provider = BobStatusProvider()
    result = provider.collect()
    for key in result:
        assert key.startswith("bob_"), f"Key {key!r} does not use bob_ prefix"


def test_pr_queue_display_marks_watermark_for_triage_not_blocking():
    """Crossing the watermark reads as triage pressure, never as a cap.

    Queue depth is a rot-attention signal: a deep queue means "triage stale /
    CI-red / duplicate PRs", not "stop opening PRs".
    """
    from gptme_bob_status.provider import _pr_queue_display

    assert _pr_queue_display(2, 10) == "2/10"
    assert _pr_queue_display(10, 10) == "10/10 ⚠ triage"
    assert _pr_queue_display(16, 10) == "16/10 ⚠ triage"
    # No watermark configured → bare count, no pressure marker.
    assert _pr_queue_display(7, None) == "7"


def test_active_tasks_strips_compact_recency_without_space_before_unit(monkeypatch):
    """gptodo compact recency is '(5m ago)' / '(<1m ago)', not '(5 m ago)'."""
    import gptme_bob_status.provider as mod

    compact = (
        "📋 Tasks Status\n"
        "  my-task  Do the thing  (5m ago)\n"
        "  other-task  Second thing  (<1m ago)\n"
        "📋 Summary: 2 total\n"
    )
    monkeypatch.setattr(
        mod, "_run", lambda cmd, **k: compact if cmd[0] == "gptodo" else ""
    )
    tasks = mod._active_tasks(3)
    assert tasks == [
        {"id": "my-task", "title": "Do the thing"},
        {"id": "other-task", "title": "Second thing"},
    ]


def test_active_tasks_preserves_parenthetical_ago_in_title(monkeypatch):
    """Task titles ending with a broad '(... ago)' phrase must NOT be stripped."""
    import gptme_bob_status.provider as mod

    compact = (
        "📋 Tasks Status\n"
        "  review-pr  Review PR (approved 2 days ago)\n"
        "  normal-task  Do thing  (3m ago)\n"
        "📋 Summary: 2 total\n"
    )
    monkeypatch.setattr(
        mod, "_run", lambda cmd, **k: compact if cmd[0] == "gptodo" else ""
    )
    tasks = mod._active_tasks(3)
    # The broad parenthetical in the first title must survive; the compact
    # recency marker in the second must be stripped.
    assert tasks == [
        {"id": "review-pr", "title": "Review PR (approved 2 days ago)"},
        {"id": "normal-task", "title": "Do thing"},
    ]
