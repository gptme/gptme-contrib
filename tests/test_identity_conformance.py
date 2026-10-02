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

import os
import re
import secrets
import subprocess
import sys
import tempfile
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
        }
        # Interpreter plumbing only; none of these carry identity.
        for var in ("LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "PYTHONPATH"):
            if var in os.environ:
                env[var] = os.environ[var]
        return env


def find_identity_leaks(text: str, allow: tuple[str, ...] = ()) -> list[str]:
    """Return Bob-identity leaks in *text*, ignoring the exact *allow* strings."""
    for allowed in allow:
        text = text.replace(allowed, "")
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
    for var in list(os.environ):
        if var.startswith(("BOB_", "AGENT_")) or var == "PM_UNIT_PREFIX":
            monkeypatch.delenv(var)
    for var, value in ident.env().items():
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

    def fake_run(cmd, *args, **kwargs):
        launched.append(list(cmd))
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
    assert_tree_has_no_identity_leak(tmp_path / "slot-tmp", identity.sandbox)
