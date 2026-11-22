"""Tests for gptme_bob_status provider."""

from __future__ import annotations

import pytest
from gptme_bob_status.provider import BobStatusProvider, make_provider

_TASK_JSON = {
    "id": "compact-task",
    "name": "Compact task title",
    "priority": "high",
    "body": "A large markdown body that must not leak into a table cell.",
    "tags": ["status", "test"],
    "created": "2026-08-27T20:00:00+00:00",
}


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
    monkeypatch.setattr(
        mod,
        "_blockers",
        lambda limit=3: [
            {
                "id": "blocked-1",
                "title": "Blocked task title",
                "waiting_for": "\n  Important blocker detail",
            }
        ],
    )
    monkeypatch.setattr(
        mod,
        "_ready_tasks",
        lambda limit=3: [{"id": "ready-1", "title": "Ready task title"}],
    )
    monkeypatch.setattr(mod, "_journal_entries", lambda limit=5: [])

    provider = BobStatusProvider()
    sections = provider.narrative_sections()

    assert isinstance(sections, list)
    assert len(sections) > 0
    assert all(isinstance(s, str) for s in sections)
    combined = "\n".join(sections)
    assert "t1" in combined
    assert "gptme/gptme" in combined
    assert "`blocked-1`: Important blocker detail" in combined
    assert "`ready-1` — Ready task title" in combined


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


def test_pr_queue_delegates_to_upstream_helper_and_maps_cap(monkeypatch):
    """One implementation of the gh loop: gptme's generic helper.

    Upstream calls the watermark ``cap``; the provider renders it as
    ``watermark`` so the display keeps treating depth as rot-attention.
    """
    import gptme.cli.cmd_status as cmd_status
    import gptme_bob_status.provider as mod

    seen: dict = {}

    def fake_upstream(repos):
        seen["repos"] = repos
        return [{"repo": "gptme/gptme", "count": 4, "cap": 10}]

    monkeypatch.setattr(cmd_status, "_pr_queue", fake_upstream, raising=False)
    assert mod._pr_queue() == [{"repo": "gptme/gptme", "count": 4, "watermark": 10}]
    assert seen["repos"] == mod._TRACKED_REPOS


def test_upstream_pr_queue_accepts_a_single_repos_argument():
    """The provider calls upstream with only the repo list.

    Upstream's ``author`` parameter is optional; pin that so a future signature
    change cannot turn the delegation into a TypeError at status time.
    """
    import inspect

    import gptme.cli.cmd_status as cmd_status

    if not hasattr(cmd_status, "_pr_queue"):
        pytest.skip("Installed gptme predates the upstream helper")
    params = list(inspect.signature(cmd_status._pr_queue).parameters.values())
    assert params and params[0].name == "repos"
    required = [
        p.name
        for p in params[1:]
        if p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert required == []


def test_pr_queue_upstream_matches_local_author_filter_and_watermarks(monkeypatch):
    """Exercise the real helper, not a fake with assumed author semantics."""
    import gptme.cli.cmd_status as cmd_status
    import gptme_bob_status.provider as mod

    if not hasattr(cmd_status, "_pr_queue"):
        pytest.skip("Installed gptme predates the upstream helper")
    calls = []

    def fake_run(cmd, *, timeout=10):
        calls.append((cmd, timeout))
        if cmd == ["gh", "api", "user", "--jq", ".login"]:
            return "example-author"
        assert cmd[cmd.index("--author") + 1] == "example-author"
        return '[{"number": 1, "title": "Test PR"}]'

    monkeypatch.setattr(cmd_status, "_run", fake_run)
    monkeypatch.setattr(mod, "_run", fake_run)
    delegated = mod._pr_queue()
    upstream_calls = calls.copy()
    calls.clear()
    assert delegated == mod._pr_queue_local()
    assert delegated == [
        {"repo": repo, "count": 1, "watermark": watermark}
        for repo, watermark in mod._TRACKED_REPOS
    ]
    assert calls == upstream_calls


def test_pr_queue_falls_back_when_upstream_helper_missing(monkeypatch):
    """Older gptme (>=0.29 floor) has the module but not the helper."""
    import gptme.cli.cmd_status as cmd_status
    import gptme_bob_status.provider as mod

    # The real older-gptme failure is ``ImportError: cannot import name
    # '_pr_queue'``, not a missing module.
    monkeypatch.delattr(cmd_status, "_pr_queue", raising=False)
    monkeypatch.setattr(
        mod, "_pr_queue_local", lambda: [{"repo": "r", "count": 1, "watermark": None}]
    )
    assert mod._pr_queue() == [{"repo": "r", "count": 1, "watermark": None}]


def test_pr_queue_falls_back_when_upstream_helper_raises(monkeypatch, caplog):
    """A raising upstream helper degrades to the local loop, not a crash."""
    import gptme.cli.cmd_status as cmd_status
    import gptme_bob_status.provider as mod

    def broken(repos):
        raise RuntimeError("upstream private helper drifted")

    monkeypatch.setattr(cmd_status, "_pr_queue", broken, raising=False)
    monkeypatch.setattr(
        mod, "_pr_queue_local", lambda: [{"repo": "r", "count": 2, "watermark": 5}]
    )
    assert mod._pr_queue() == [{"repo": "r", "count": 2, "watermark": 5}]
    assert "upstream private helper drifted" in caplog.text


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


def test_ready_tasks_returns_compact_summaries(monkeypatch):
    """Ready-task cells exclude bodies and other full-task metadata."""
    import json

    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_run", lambda cmd, **k: json.dumps(_TASK_JSON))

    assert mod._ready_tasks() == [
        {"id": "compact-task", "title": "Compact task title", "priority": "high"}
    ]


def test_blockers_return_compact_summaries_with_narrative_fields(monkeypatch):
    """Blocker cells stay compact while retaining fields used by narratives."""
    import json

    import gptme_bob_status.provider as mod

    task = {
        **_TASK_JSON,
        "name": None,
        "waiting_for": "\n  A blocker explanation that stays available\ninternal detail",
        "waiting_since": "2026-08-27T20:30:00+00:00",
    }
    monkeypatch.setattr(mod, "_run", lambda cmd, **k: json.dumps(task))

    assert mod._blockers() == [
        {
            "id": "compact-task",
            "title": "compact-task",
            "priority": "high",
            "waiting_for": "A blocker explanation that stays available",
            "waiting_since": "2026-08-27T20:30:00+00:00",
        }
    ]


def test_blockers_skip_whitespace_only_explanations(monkeypatch):
    """A task without visible blocker context is not a top blocker."""
    import json

    import gptme_bob_status.provider as mod

    task = {**_TASK_JSON, "waiting_for": " \n\t "}
    monkeypatch.setattr(mod, "_run", lambda cmd, **k: json.dumps(task))

    assert mod._blockers() == []


def test_blockers_skip_missing_waiting_for(monkeypatch):
    """A waiting task without waiting_for is skipped, not a KeyError."""
    import json

    import gptme_bob_status.provider as mod

    monkeypatch.setattr(mod, "_run", lambda cmd, **k: json.dumps(_TASK_JSON))

    assert "waiting_for" not in _TASK_JSON
    assert mod._blockers() == []
