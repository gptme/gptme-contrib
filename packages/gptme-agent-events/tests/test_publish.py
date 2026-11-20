"""The publisher keeps a privacy-filtered status row per agent."""

from __future__ import annotations

import json

from click.testing import CliRunner

from gptme_agent_events import AgentEvent
from gptme_agent_events.cli import main
from gptme_agent_events.publish import Publisher, RepoVisibility


class FakeVisibility(RepoVisibility):
    def __init__(self, public: set[str]):
        super().__init__(token="")
        self.public = public
        self.lookups: list[str] = []

    def lookup(self, slug: str) -> bool:
        self.lookups.append(slug)
        return slug in self.public


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def ev(kind: str, agent: str = "bob", **kw) -> AgentEvent:
    return AgentEvent(
        kind=kind, ts="t", harness="claude-code", session=kw.pop("session", "s1"), agent=agent, **kw
    )


def make(public=("gptme/gptme",), **kw):
    rows: list[dict] = []
    clock = Clock()
    vis = FakeVisibility(set(public))

    def collect(row: dict) -> bool:
        rows.append(row)
        return True

    pub = Publisher(
        {"bob": "bob", "alice-ct": "alice"},
        collect,
        visibility=vis,
        clock=clock,
        **kw,
    )
    return pub, rows, clock, vis


def test_working_row_has_only_public_safe_fields():
    pub, rows, _clock, _ = make()
    pub.handle(
        ev(
            "tool",
            tool="Bash",
            tool_class="test",
            detail={"cwd": "/home/bob/secret-project", "path": "/x"},
        )
    )
    assert pub.tick() == 1
    (row,) = rows
    assert row == {
        "agent_id": "bob",
        "state": "working",
        "activity": "test",
        "milestone_kind": None,
        "milestone_repo": None,
        "milestone_url": None,
        "milestone_at": None,
        "private_commits_1h": 0,
        "active_sessions": 1,
    }
    assert "secret" not in json.dumps(rows)


def test_public_milestone_linked_private_only_counted():
    pub, rows, _clock, vis = make()
    pub.handle(ev("commit", detail={"repo": "ErikBjare/bob", "sha": "abc1234", "dir": "/x"}))
    pub.handle(
        ev(
            "pr_open",
            detail={"repo": "gptme/gptme", "url": "https://github.com/gptme/gptme/pull/1"},
        )
    )
    pub.tick()
    row = rows[-1]
    assert row["milestone_kind"] == "pr_open"
    assert row["milestone_url"] == "https://github.com/gptme/gptme/pull/1"
    assert row["private_commits_1h"] == 1
    assert "ErikBjare/bob" not in json.dumps(rows)
    # visibility is cached
    pub.handle(ev("commit", detail={"repo": "ErikBjare/bob", "sha": "def"}))
    assert vis.lookups.count("ErikBjare/bob") == 1


def test_commit_and_push_urls():
    pub, rows, clock, _ = make()
    pub.handle(ev("commit", detail={"repo": "gptme/gptme", "sha": "abc1234"}))
    pub.tick()
    assert rows[-1]["milestone_url"] == "https://github.com/gptme/gptme/commit/abc1234"
    clock.t += 1
    pub.handle(ev("push", detail={"repo": "gptme/gptme", "ref": "origin/feat/x"}))
    pub.tick()
    assert rows[-1]["milestone_url"] == "https://github.com/gptme/gptme/tree/feat/x"


def test_unknown_repo_fails_closed():
    pub, rows, _clock, _ = make(public=())
    pub.handle(ev("pr_open", detail={"url": "https://github.com/o/r/pull/2"}))
    pub.tick()
    assert rows[-1]["milestone_kind"] is None


def test_debounce_heartbeat_and_idle():
    pub, rows, clock, _ = make(min_interval=20, heartbeat=240, idle_after=300)
    pub.handle(ev("tool", tool_class="read"))
    pub.tick()
    clock.t += 5
    pub.handle(ev("tool", tool_class="write"))
    assert pub.tick() == 0  # debounced
    clock.t += 20
    assert pub.tick() == 1 and rows[-1]["activity"] == "write"
    assert pub.tick() == 0  # nothing new
    clock.t += 240
    assert pub.tick() == 1  # heartbeat while working
    clock.t += 100  # 345 s after the last event
    assert pub.tick() == 1
    assert rows[-1]["state"] == "idle" and rows[-1]["activity"] is None
    assert rows[-1]["active_sessions"] == 0
    clock.t += 1000
    assert pub.tick() == 0  # idle rows are not refreshed


def test_milestone_bypasses_debounce():
    pub, _rows, clock, _ = make(min_interval=20)
    pub.handle(ev("tool", tool_class="shell"))
    pub.tick()
    clock.t += 1
    pub.handle(
        ev(
            "pr_open",
            detail={"repo": "gptme/gptme", "url": "https://github.com/gptme/gptme/pull/9"},
        )
    )
    assert pub.tick() == 1


def test_agent_mapping_and_unknown_agents_ignored():
    pub, rows, _clock, _ = make()
    pub.handle(ev("tool", agent="alice-ct", tool_class="read"))
    pub.handle(ev("tool", agent="stranger", tool_class="read"))
    pub.tick()
    assert [r["agent_id"] for r in rows] == ["alice"]


def test_private_commit_count_expires():
    pub, rows, clock, _ = make(public=())
    pub.handle(ev("commit", detail={"repo": "o/private"}))
    pub.tick()
    assert rows[-1]["private_commits_1h"] == 1
    clock.t += 3601
    pub.handle(ev("tool", tool_class="read"))
    pub.tick()
    assert rows[-1]["private_commits_1h"] == 0


def test_failed_write_is_retried():
    clock = Clock()
    attempts: list[dict] = []
    ok = {"value": False}

    def write(row):
        attempts.append(row)
        return ok["value"]

    pub = Publisher({"bob": "bob"}, write, visibility=FakeVisibility(set()), clock=clock)
    pub.handle(ev("tool", tool_class="read"))
    assert pub.tick() == 0
    ok["value"] = True
    assert pub.tick() == 0
    clock.t += 20
    assert pub.tick() == 1
    assert len(attempts) == 2


def test_cli_dry_run():
    lines = "\n".join(
        [
            ev("tool", tool_class="test").to_json(),
            "garbage",
            ev("tool", agent="nobody", tool_class="read").to_json(),
        ]
    )
    result = CliRunner().invoke(main, ["publish", "--agent", "bob", "--dry-run"], input=lines)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines()]
    assert [(r["agent_id"], r["state"], r["activity"]) for r in rows] == [
        ("bob", "working", "test")
    ]


def test_cli_requires_backend_settings():
    result = CliRunner().invoke(main, ["publish", "--agent", "bob"], input="", env={})
    assert result.exit_code != 0
    assert "--token-cmd" in result.output


def test_new_milestone_rechecks_positive_and_clears_revoked_details():
    pub, rows, clock, vis = make()
    pub.handle(ev("commit", detail={"repo": "gptme/gptme", "sha": "abc1234"}))
    pub.tick()
    vis.public.clear()
    clock.t += 1
    pub.handle(ev("commit", detail={"repo": "gptme/gptme", "sha": "deadbeef"}))
    pub.tick()
    assert rows[-1]["milestone_repo"] is None
    assert rows[-1]["private_commits_1h"] == 1
    assert "deadbeef" not in json.dumps(rows)


def test_token_failure_is_retryable_without_disclosing_command(monkeypatch, capsys):
    import subprocess

    from gptme_agent_events.publish import SupabaseWriter

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "secret-token-command", output="secret-token")

    monkeypatch.setattr(subprocess, "run", fail)
    writer = SupabaseWriter("https://example.com", "anon", "secret-token-command")
    assert writer({}) is False
    stderr = capsys.readouterr().err
    assert "CalledProcessError" in stderr
    assert "secret" not in stderr


def test_failed_urgent_write_is_spaced_and_keeps_latest_row():
    clock = Clock()
    attempts: list[dict] = []

    def fail(row):
        attempts.append(row)
        return False

    pub = Publisher(
        {"bob": "bob"},
        fail,
        visibility=FakeVisibility({"gptme/gptme"}),
        clock=clock,
    )
    pub.handle(ev("commit", detail={"repo": "gptme/gptme", "sha": "abc1234"}))
    pub.tick()
    for _ in range(100):
        pub.tick()
    assert len(attempts) == 1
    pub.handle(ev("tool", tool_class="write"))
    clock.t += 20
    pub.tick()
    assert len(attempts) == 2
    assert attempts[-1]["activity"] == "write"
    assert attempts[-1]["milestone_url"].endswith("/abc1234")
