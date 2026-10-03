"""Identity-portability conformance test (gptme/gptme-contrib#1705).

Generic packages must work for any agent, not just Bob. #1705 removed the
hard-coded Bob identity from them one surface at a time; this test is the
regression gate that keeps it removed.

Each test runs a generic surface under a *randomized* identity (agent name,
operator name, ``$HOME``, workspace repo root) that is configured the way a
forked agent would configure it, then asserts that nothing Bob-specific
(``bob``, ``erik``, ``/home/bob``, ``TimeToBuildBob``, ``ErikBjare``) shows up
in rendered output, generated files, or the environment handed to child
processes. Names are random per run so a surface that echoes a hard-coded
default can never pass by coincidence.

Documented legacy aliases (the ``BOB_*`` env vars kept so Bob's deployment
keeps working) are allowed explicitly per surface, never globally.

To cover a new generic surface: build its output under the ``identity``
fixture and pass it to :func:`assert_no_identity_leak`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

#: Case-insensitive markers of Bob's deployment. ``bob`` / ``erik`` match as
#: whole tokens, where ``_`` counts as a separator so env var names such as
#: ``BOB_BACKEND`` are caught too.
_LEAK_RE = re.compile(
    r"/home/bob|timetobuildbob|erikbjare|gptme-bob"
    r"|(?<![a-z0-9])(?:bob|erik)(?![a-z0-9])",
    re.IGNORECASE,
)

#: Agent/operator name stems. Each run appends a random suffix.
_AGENT_STEMS = ("Quinn", "Juno", "Rhea", "Otto")
_OPERATOR_STEMS = ("Mira", "Ada", "Noor", "Ilse")


@dataclass(frozen=True)
class Identity:
    """A randomized, non-Bob agent deployment."""

    agent: str
    operator: str
    home: Path
    workspace: Path
    #: Per-test sandbox holding ``home`` and ``workspace``. Its own path is
    #: host-specific (pytest names it after the local user, e.g.
    #: ``/tmp/pytest-of-bob``), so it is stripped before scanning.
    sandbox: Path

    @property
    def unit_prefix(self) -> str:
        return f"{self.agent.lower()}-pm"

    def env(self) -> dict[str, str]:
        """A child-process environment carrying only this identity.

        Built from scratch rather than copied from ``os.environ``, so the
        developer's (or Bob's) own ``BOB_*`` / ``AGENT_*`` variables cannot
        mask a leak.
        """
        env = {
            "HOME": str(self.home),
            "USER": self.agent.lower(),
            "LOGNAME": self.agent.lower(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AGENT_WORKSPACE": str(self.workspace),
            "PM_UNIT_PREFIX": self.unit_prefix,
            "GPTME_WORKSPACE": str(self.workspace),
        }
        # Interpreter plumbing only; none of these carry identity.
        for var in ("LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "PYTHONPATH"):
            if var in os.environ:
                env[var] = os.environ[var]
        return env

    def cli(
        self,
        entrypoint: str,
        *args: str,
        setup: str = "",
        env: dict[str, str] | None = None,
    ) -> str:
        """Run the installed CLI in a fresh, identity-isolated interpreter.

        ``setup`` replaces external service boundaries only. Package imports,
        CLI parsing, identity resolution, and filesystem writes stay real.
        """
        script = textwrap.dedent(setup) + textwrap.dedent("""
            import sys
            from importlib.metadata import entry_points
            sys.argv = sys.argv[1:]
            command = next(iter(entry_points(group="console_scripts", name=sys.argv[0])))
            sys.exit(command.load()())
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, entrypoint, *args],
            env=self.env() | (env or {}),
            cwd=self.workspace,
            input="",
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert_no_identity_leak(
            output, where=f"{entrypoint} {' '.join(args)}", sandbox=self.sandbox
        )
        return output


def find_identity_leaks(text: str, allow: tuple[str, ...] = ()) -> list[str]:
    """Return Bob-identity leaks in *text*, ignoring the exact *allow* strings."""
    for allowed in allow:
        # Use identifier boundaries so a superset like "BOB_BACKEND_EXTRA" is
        # not accidentally allowed when the allow list contains "BOB_BACKEND".
        text = re.sub(
            r"(?<![A-Za-z0-9_])" + re.escape(allowed) + r"(?![A-Za-z0-9_])",
            "",
            text,
            flags=re.IGNORECASE,
        )
    leaks = []
    for match in _LEAK_RE.finditer(text):
        start = max(match.start() - 40, 0)
        leaks.append(text[start : match.end() + 40].replace("\n", "\\n"))
    return leaks


def assert_no_identity_leak(
    text: str,
    *,
    where: str,
    allow: tuple[str, ...] = (),
    sandbox: Path | None = None,
) -> None:
    if sandbox is not None:
        text = text.replace(str(sandbox), "<sandbox>")
    leaks = find_identity_leaks(text, allow)
    assert not leaks, f"Bob identity leaked in {where}:\n  " + "\n  ".join(leaks)


def assert_tree_has_no_identity_leak(root: Path, sandbox: Path) -> None:
    """Scan every file name and text file under *root*."""
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        assert_no_identity_leak(rel, where=f"file name {rel}")
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert_no_identity_leak(
                content, where=f"generated file {rel}", sandbox=sandbox
            )


@pytest.fixture
def identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Identity:
    """A randomized non-Bob identity, applied to this process's environment."""
    suffix = secrets.token_hex(2)
    agent = f"{secrets.choice(_AGENT_STEMS)}{suffix}"
    operator = f"{secrets.choice(_OPERATOR_STEMS)}{suffix}"
    home = tmp_path / f"home-{agent.lower()}"
    workspace = tmp_path / f"{agent.lower()}-workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "gptme.toml").write_text(f'[agent]\nname = "{agent}"\n')
    ident = Identity(
        agent=agent, operator=operator, home=home, workspace=workspace, sandbox=tmp_path
    )

    # Drop the ambient deployment's identity so in-process surfaces resolve
    # only what this fixture configures.
    clean_env = ident.env()
    for var in list(os.environ):
        if var not in clean_env:
            monkeypatch.delenv(var)
    for var, value in clean_env.items():
        if var != "PATH":
            monkeypatch.setenv(var, value)
    return ident


def test_leak_detector_catches_bob_markers() -> None:
    """The detector itself must not be a silent no-op."""
    for sample in (
        "You are Bob",
        "/home/bob/bob/insights",
        "--setenv=BOB_BACKEND=x",
        "bob-pm-fast-slot-1",
        "ask Erik",
        "TimeToBuildBob",
        "ErikBjare/gptme-bob",
    ):
        assert find_identity_leaks(sample), sample
    for clean in ("Bobby", "kebob", "Erika", "erikson", "Quinn1a2b"):
        assert not find_identity_leaks(clean), clean
    assert not find_identity_leaks("x BOB_A y", allow=("BOB_A",))
    # Superset strings must still be caught even when a prefix is allowed.
    assert find_identity_leaks("x BOB_A_EXTRA y", allow=("BOB_A",))


# --- gptme-ace -------------------------------------------------------------


def test_ace_identity_resolves_to_configured_agent(identity: Identity) -> None:
    pytest.importorskip("gptme_ace")
    from gptme_ace.generator import domain_context
    from gptme_ace.identity import agent_name, default_workspace_root

    assert default_workspace_root() == identity.workspace
    assert agent_name(identity.workspace) == identity.agent

    context = domain_context()
    assert identity.agent in context
    assert_no_identity_leak(
        context, where="gptme-ace generator domain context", sandbox=identity.sandbox
    )


def test_ace_home_fallback_uses_configured_home(identity: Identity) -> None:
    """With no workspace configured, storage falls back under ``$HOME``."""
    pytest.importorskip("gptme_ace")
    from gptme_ace.identity import default_workspace_root

    # ``start`` outside any agent repo, so repo auto-detection finds nothing.
    root = default_workspace_root(environ={}, start=identity.home)
    assert root.is_relative_to(identity.home)
    assert_no_identity_leak(
        str(root), where="gptme-ace fallback storage root", sandbox=identity.sandbox
    )


def test_ace_storage_cli(identity: Identity) -> None:
    """``python -m gptme_ace.storage`` writes only under the agent's workspace."""
    pytest.importorskip("gptme_ace")
    for args in (["stats"], ["list"]):
        result = subprocess.run(
            [sys.executable, "-m", "gptme_ace.storage", *args],
            env=identity.env(),
            cwd=identity.workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert_no_identity_leak(
            result.stdout + result.stderr,
            where=f"gptme-ace storage {args[0]}",
            sandbox=identity.sandbox,
        )

    assert (identity.workspace / "insights" / "raw").is_dir()
    assert not (identity.home / "bob").exists()
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)

    # Scan the env dict handed to the subprocess.  Interpreter-plumbing vars
    # (PYTHONPATH etc.) may legitimately contain host-specific paths that include
    # the developer's username, so they are excluded from this scan.
    _PLUMBING = frozenset(
        {"PYTHONPATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "PATH"}
    )
    env_text = "\n".join(
        f"{k}={v}" for k, v in identity.env().items() if k not in _PLUMBING
    )
    assert_no_identity_leak(
        env_text, where="subprocess env (non-plumbing vars)", sandbox=identity.sandbox
    )


# --- gptme-runloops: erik_decision prompt ----------------------------------


def test_erik_decision_prompt(identity: Identity) -> None:
    pytest.importorskip("gptme_runloops")
    from gptme_runloops.prompt_templates import (
        ItemPromptKind,
        ItemPromptParams,
        render_item_investigate,
        render_main_prompt,
    )

    params = ItemPromptParams(
        repo=f"{identity.operator.lower()}/{identity.agent.lower()}-workspace",
        number=7,
        workspace=str(identity.workspace),
        detail="decision_id=d-1 verdict=YES kind=issue",
        agent_name=identity.agent,
        operator_name=identity.operator,
    )
    arm = render_item_investigate(ItemPromptKind.ERIK_DECISION, params)
    prompt = render_main_prompt(
        params,
        item_type="erik_decision",
        title="Approve the release",
        investigate=arm,
        monitoring_rules="",
        time_desc="30 minutes",
    )
    assert identity.operator in arm
    assert identity.agent in prompt
    # ``erik_decision`` is the legacy wire name of the item kind (shared with
    # the bash lib), not rendered identity; the caller passes it in.
    assert_no_identity_leak(
        prompt,
        where="erik_decision prompt",
        allow=("erik_decision",),
        sandbox=identity.sandbox,
    )


# --- gptme-runloops: pm_dispatch -------------------------------------------

#: Legacy env var spellings pm_dispatch dual-writes beside the neutral
#: ``AGENT_*`` names so Bob's deployment keeps working (#1748).
_PM_DISPATCH_LEGACY_ALIASES = (
    "BOB_BACKEND",
    "BOB_SELECTED_MODEL",
    "BOB_PM_BANDIT_SHADOW",
)


def test_pm_dispatch_launch(
    identity: Identity, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The systemd unit pm_dispatch launches carries only the agent's identity."""
    pytest.importorskip("gptme_runloops")
    from gptme_runloops.pm_dispatch import LaneDispatcher, SlotItem, SlotManager

    launched: list[list[str]] = []
    launched_envs: list[dict[str, str]] = []

    def fake_run(cmd, *args, **kwargs):
        launched.append(list(cmd))
        if kwargs.get("env"):
            launched_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Slot files go to the system temp dir; point it at the sandbox.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "slot-tmp"))
    (tmp_path / "slot-tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)

    manager = SlotManager(
        count_running=lambda: 0,
        count_running_lane=lambda lane: 0,
        is_busy=lambda unit: False,
    )
    dispatcher = LaneDispatcher(
        slot_manager=manager, slot_log_dir=identity.workspace / "logs"
    )
    item = SlotItem(
        repo=f"{identity.operator.lower()}/project",
        number=42,
        types=["ci_failure"],
        title="CI red",
    )
    launched_count, _, _ = dispatcher.dispatch(
        [item],
        backend="claude-code",
        model="model-x",
        script_path=str(identity.workspace / "slot.sh"),
    )

    assert launched_count == 1
    assert len(launched) == 1
    cmd = launched[0]
    unit = next(arg for arg in cmd if arg.startswith("--unit="))
    assert unit.startswith(f"--unit={identity.unit_prefix}-")
    assert "--setenv=AGENT_BACKEND=claude-code" in cmd
    assert_no_identity_leak(
        "\n".join(cmd),
        where="pm_dispatch systemd-run command",
        allow=_PM_DISPATCH_LEGACY_ALIASES,
        sandbox=identity.sandbox,
    )
    # Also scan any explicit env dict passed to subprocess.run.
    _PLUMBING = frozenset(
        {"PYTHONPATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "PATH"}
    )
    for env in launched_envs:
        env_text = "\n".join(f"{k}={v}" for k, v in env.items() if k not in _PLUMBING)
        assert_no_identity_leak(
            env_text,
            where="pm_dispatch subprocess env (non-plumbing vars)",
            allow=_PM_DISPATCH_LEGACY_ALIASES,
            sandbox=identity.sandbox,
        )
    assert_tree_has_no_identity_leak(tmp_path / "slot-tmp", identity.sandbox)


# --- Generic package CLIs --------------------------------------------------


def test_gptodo_cli_assignment(identity: Identity) -> None:
    pytest.importorskip("gptodo")
    (identity.workspace / "tasks").mkdir()
    identity.cli("gptodo", "add", "Identity smoke task")
    task = identity.workspace / "tasks" / "identity-smoke-task.md"
    assert f"assigned_to: {identity.agent.lower()}" in task.read_text()
    identity.cli("gptodo", "show", "identity-smoke-task")
    identity.cli("gptodo", "claim", "identity-smoke-task")
    assert "state: active" in task.read_text()
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)


def test_sessions_cli_neutral_lineage(identity: Identity) -> None:
    pytest.importorskip("gptme_sessions")
    session_id = f"{identity.agent.lower()}-child"
    parent_id = f"{identity.agent.lower()}-parent"
    dispatch_id = f"{identity.unit_prefix}-slot-1"
    identity.cli(
        "gptme-sessions",
        "post-session",
        "--harness",
        "codex",
        "--model",
        "test-model",
        "--run-type",
        "autonomous",
        "--session-id",
        session_id,
        "--json",
        env={
            "AGENT_PARENT_SESSION_ID": parent_id,
            "AGENT_DISPATCH_KIND": "worker",
            "AGENT_DISPATCH_ID": dispatch_id,
        },
    )
    store = (
        identity.home / ".local" / "share" / "gptme-sessions" / "session-records.jsonl"
    )
    records = [json.loads(line) for line in store.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["session_id"] == session_id
    assert records[0]["parent_session_id"] == parent_id
    assert records[0]["dispatch_kind"] == "worker"
    assert records[0]["dispatch_id"] == dispatch_id
    output = identity.cli("gptme-sessions", "query", "--json", "--since", "all")
    assert session_id in output
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)


def test_coordination_cli_workspace_and_owner(identity: Identity) -> None:
    pytest.importorskip("gptme_coordination")
    key = "identity:smoke"
    identity.cli("gptme-coordination", "work-claim", identity.agent, key)
    output = identity.cli("gptme-coordination", "work-list", "--claimed")
    assert f"{key} [claimed] by {identity.agent}" in output
    db_path = identity.workspace / "state" / "coordination" / "coord.db"
    assert db_path.is_file()
    # SQLite is binary: inspect persisted owner/key rather than relying on the
    # text-tree scanner to cover it.
    import sqlite3

    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT task_id, claimer FROM work").fetchall()
    assert rows == [(key, identity.agent)]
    assert_no_identity_leak(repr(rows), where="coordination persisted claims")
    identity.cli("gptme-coordination", "work-complete", identity.agent, key)
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)


@pytest.mark.parametrize("github_enabled", [False, True], ids=["offline", "github"])
def test_activity_summary_cli_workspace_repos(
    identity: Identity, github_enabled: bool
) -> None:
    pytest.importorskip("gptme_activity_summary")
    repo = f"{identity.operator.lower()}/{identity.agent.lower()}-workspace"
    for args in (
        ["init", "--quiet"],
        ["remote", "add", "origin", f"https://github.com/{repo}.git"],
        [
            "-c",
            f"user.name={identity.operator}",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "test: identity fixture",
        ],
    ):
        subprocess.run(
            ["git", *args],
            cwd=identity.workspace,
            env=identity.env(),
            check=True,
            capture_output=True,
            text=True,
        )
    from datetime import date

    today = date.today().isoformat()
    journal = identity.workspace / "journal" / today
    journal.mkdir(parents=True)
    (journal / "session.md").write_text(f"# Built a tool\nAgent: {identity.agent}\n")
    # GitHub and the LLM are external services. Keep the real git remote lookup,
    # data aggregation, prompt construction, CLI, and summary persistence.
    identity.cli(
        "gptme-activity-summary",
        "daily",
        "--date",
        today,
        setup="""
        import gptme_activity_summary.github_data as github
        import gptme_activity_summary.cc_backend as backend
        import gptme_activity_summary.cli as summary
        from gptme_activity_summary.aw_data import AWActivity
        import os, json
        from pathlib import Path
        real_command = github._run_command
        def service_command(cmd, timeout=30):
            if cmd[0] != "gh":
                return real_command(cmd, timeout)
            if os.environ["TEST_GITHUB_ENABLED"] == "0":
                return None
            log_path = Path(os.environ.get("GITHUB_CMD_LOG", "/tmp/github-commands.jsonl"))
            with log_path.open("a") as log:
                log.write(json.dumps(cmd) + "\\n")
            if cmd[1:3] == ["auth", "status"]:
                return "authenticated"
            if cmd[1] == "api" and cmd[2].startswith("repos/"):
                return cmd[2].removeprefix("repos/")
            if cmd[1] == "api":
                return "0"
            return "[]"
        github._run_command = service_command
        summary.fetch_aw_activity = lambda start, end: AWActivity(start_date=start, end_date=end)
        backend.summarize_daily_with_cc = lambda entries, date, **kw: {
            "narrative": kw["extra_context"], "accomplishments": [entries[0][1]],
        }
    """,
        env={
            "TEST_GITHUB_ENABLED": "1" if github_enabled else "0",
            # Write the diagnostic log outside the workspace so the identity
            # tree scan doesn't find gh commands that include the real GH username.
            "GITHUB_CMD_LOG": str(identity.sandbox / "github-commands.jsonl"),
        },
    )
    summaries = identity.workspace / "knowledge" / "summaries"
    assert summaries.is_dir()
    generated = "\n".join(p.read_text() for p in summaries.rglob("*") if p.is_file())
    assert repo in generated
    assert identity.agent in generated
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)


@pytest.mark.parametrize("declared", [True, False], ids=["configured", "undeclared"])
def test_voice_cli_identity(identity: Identity, declared: bool) -> None:
    pytest.importorskip("gptme_voice")
    if not declared:
        (identity.workspace / "gptme.toml").write_text("")
    output = identity.cli(
        "gptme-voice-server",
        "--workspace",
        str(identity.workspace),
        setup="""
            import gptme_voice.realtime.server as voice
            def capture_startup(server):
                print(server._instructions)
                print(voice._resolve_protocol_identity(server.workspace, None))
                print(voice._build_fresh_call_greeting_instructions("", server.workspace))
            voice.VoiceServer.run = capture_startup
        """,
        env={
            "GPTME_VOICE_STATE_DIR": str(identity.workspace / "state" / "voice"),
            # Ensure the voice server resolves to the test identity's name
            # rather than falling back to the deployment-specific default.
            "GPTME_VOICE_AGENT_NAME": identity.agent,
            "AGENT_NAME": identity.agent,
        },
    )
    if declared:
        assert f"You are {identity.agent}" in output
        assert identity.agent.lower() in output
    assert_tree_has_no_identity_leak(identity.workspace, identity.sandbox)
    assert_tree_has_no_identity_leak(identity.home, identity.sandbox)
