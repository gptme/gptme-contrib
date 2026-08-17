"""Tests for gptme_bob_status provider."""

from __future__ import annotations

from gptme_bob_status.provider import BobStatusProvider, StatusProvider, make_provider


def test_provider_satisfies_protocol():
    """BobStatusProvider satisfies the StatusProvider protocol."""
    provider = make_provider()
    assert isinstance(provider, StatusProvider)


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
        mod, "_pr_queue", lambda: [{"repo": "gptme/gptme", "count": 2, "cap": 10}]
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
