"""CLI-side assembly for ``run-item``: config loading + default collaborators.

The step-1-3 pattern (inject Python callables) cannot cross the systemd→CLI
boundary, so the *library* (:mod:`gptme_runloops.run_item`) stays
callable-injected and this module assembles the default collaborators from
workspace conventions (design §4.3):

- **Scalar policy** comes from :class:`~gptme_runloops.run_item.RunItemConfig`
  defaults, optionally overridden by a TOML file
  (``<workspace>/config/pm-run-item.toml`` by convention) and by the same
  environment variables the bash honors
  (``PROJECT_MONITORING_SELF_MERGE_REPOS`` etc. — env wins, matching bash
  ``${VAR:-default}``).
- **Command hooks** default to the conventional workspace script paths and
  degrade to ``None`` when the script is missing — matching the bash
  ``[ -f ]`` / ``[ -x ]`` guards, and keeping the package usable by agents
  with fewer hooks.
- **In-process collaborators** (``post_session``, ``SessionRecord``,
  worker-result builders, latency recorder) are imported dynamically with
  graceful degradation, mirroring the worker.sh heredocs' import-failure
  semantics (a missing package skips that bookkeeping step, WARN + continue).

TOML format::

    [config]
    author = "TimeToBuildBob"
    agent_name = "Bob"
    operator_name = "Erik"
    primary_repo = "ErikBjare/bob"
    greptile_repos_pattern = "^(gptme/gptme|gptme/gptme-contrib)$"
    self_merge_repos = "ErikBjare/bob owner/repo2"
    ambient_harness_env = "BOB_AMBIENT_HARNESS"
    extra_sys_path = ["packages/custom/src"]
    # any other RunItemConfig field by name; relative paths resolve
    # against the workspace

    [hooks]
    sysprompt_builder = ["scripts/build-system-prompt.sh", "--context-args", "--type monitoring"]
    post_run = ["scripts/github/pm-post-run-summary.sh"]
    # any argv-list hook by name; [] disables a default hook
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import fields as dataclass_fields
from functools import partial
from pathlib import Path
from typing import Any

from gptme_runloops.merge_lifecycle import SubprocessMergeLifecycleIO
from gptme_runloops.run_item import RunItemConfig, RunItemHooks, promote_item_state
from gptme_runloops.utils.git import git_pull_with_retry

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10
    import tomli as tomllib

# RunItemConfig fields that hold paths (resolved relative to the workspace
# when given as relative strings in TOML).
_PATH_FIELDS = {
    "monitoring_rules_file",
    "records_dir",
    "dispatch_ledger",
    "wait_merge_gate_log",
    "backend_quota_dir",
    "state_dir",
    "pending_state_dir",
    "lock_dir",
    "lock_history",
    "cc_projects_dir",
    "cc_credentials_path",
    "copilot_state_dir",
    "codex_sessions_dir",
}

# Env vars that override config fields (bash `${VAR:-default}` parity).
_ENV_OVERRIDES = {
    "self_merge_repos": "PROJECT_MONITORING_SELF_MERGE_REPOS",
    "self_merge_allowed_paths": "PROJECT_MONITORING_SELF_MERGE_ALLOWED_PATHS",
    "wait_merge_auto_enabled_repos": "PROJECT_MONITORING_AUTO_WAIT_AND_MERGE_REPOS",
}


def default_config_path(workspace: Path) -> Path:
    return workspace / "config" / "pm-run-item.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_run_item_config(
    workspace: Path,
    config_path: Path | None = None,
    **overrides: Any,
) -> tuple[RunItemConfig, dict[str, Any]]:
    """Build a :class:`RunItemConfig` from defaults + TOML + env + overrides.

    Returns ``(config, raw_toml)`` — the raw dict is reused for hook argv
    overrides and ``extra_sys_path``. A missing config file produces the
    package defaults exactly (convention over configuration).
    """
    workspace = Path(workspace).resolve()
    path = config_path or default_config_path(workspace)
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = _load_toml(path)

    section = raw.get("config", {})
    if not isinstance(section, dict):
        section = {}

    valid_fields = {f.name for f in dataclass_fields(RunItemConfig)}
    kwargs: dict[str, Any] = {}
    for key, value in section.items():
        if key not in valid_fields:
            continue
        if key in _PATH_FIELDS and isinstance(value, str):
            p = Path(value)
            value = p if p.is_absolute() else workspace / p
        kwargs[key] = value

    for field_name, env_name in _ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            kwargs[field_name] = env_value

    for key, value in overrides.items():
        if value is not None and key in valid_fields:
            kwargs[key] = value

    return RunItemConfig(workspace=workspace, **kwargs), raw


def _import_attr(module: str, attr: str) -> Any:
    try:
        return getattr(importlib.import_module(module), attr)
    except Exception:
        return None


def _insert_sys_paths(workspace: Path, raw: dict[str, Any]) -> None:
    """Mirror the worker.sh shim sys.path insertions (worker.sh:308-315 etc.)."""
    candidates = [
        "gptme-contrib/packages/gptme-sessions/src",
        "packages/metaproductivity/src",
        "packages/agent-events/src",
    ]
    section = raw.get("config", {})
    extra = section.get("extra_sys_path", []) if isinstance(section, dict) else []
    if isinstance(extra, list):
        candidates = [str(p) for p in extra] + candidates
    for rel in candidates:
        p = Path(rel)
        if not p.is_absolute():
            p = workspace / p
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _hook_argv(
    raw: dict[str, Any], name: str, default: list[str] | None, workspace: Path
) -> list[str] | None:
    """Resolve a hook argv: TOML override (``[]`` disables) or the default.

    A TOML argv whose first element is a relative existing workspace path is
    resolved absolute, so config files can say ``["scripts/foo.sh"]``.
    """
    section = raw.get("hooks", {})
    if isinstance(section, dict) and name in section:
        value = section[name]
        if not isinstance(value, list) or not value:
            return None
        argv = [str(a) for a in value]
        first = Path(argv[0])
        if not first.is_absolute() and (workspace / first).exists():
            argv[0] = str(workspace / first)
        return argv
    return default


def _default_if_exists(argv: list[str], probe: Path) -> list[str] | None:
    return argv if probe.exists() else None


def assemble_hooks(config: RunItemConfig, raw: dict[str, Any]) -> RunItemHooks:
    """Assemble the default hook set from workspace conventions + TOML.

    Every command hook degrades to ``None`` (skip that step) when the
    conventional script is missing, matching the bash guards. In-process
    collaborators degrade to ``None`` on import failure, matching the
    heredocs' failure semantics.
    """
    ws = config.workspace
    _insert_sys_paths(ws, raw)

    # In-process collaborators (worker.sh heredoc imports)
    post_session = _import_attr("gptme_sessions.post_session", "post_session")
    session_store_cls = _import_attr("gptme_sessions.store", "SessionStore")
    make_store = None
    if session_store_cls is not None:
        make_store = lambda sessions_dir: session_store_cls(sessions_dir=sessions_dir)  # noqa: E731
    session_record_cls = _import_attr("metaproductivity.sessions", "SessionRecord")
    make_record = None
    if session_record_cls is not None:
        make_record = lambda **kwargs: session_record_cls(**kwargs).to_dict()  # noqa: E731

    hooks = RunItemHooks(
        runner=_hook_argv(raw, "runner", [str(ws / "run.sh")], ws)
        or [str(ws / "run.sh")],
        sysprompt_builder=_hook_argv(
            raw,
            "sysprompt_builder",
            _default_if_exists(
                [
                    str(ws / "scripts/build-system-prompt.sh"),
                    "--context-args",
                    "--type monitoring",
                ],
                ws / "scripts/build-system-prompt.sh",
            ),
            ws,
        ),
        delivery_check=_hook_argv(
            raw,
            "delivery_check",
            _default_if_exists(
                ["python3", str(ws / "scripts/runs/github/check-pm-delivery.py")],
                ws / "scripts/runs/github/check-pm-delivery.py",
            ),
            ws,
        ),
        wait_merge_gate=_hook_argv(
            raw,
            "wait_merge_gate",
            _default_if_exists(
                ["python3", str(ws / "scripts/github/should-auto-wait-and-merge.py")],
                ws / "scripts/github/should-auto-wait-and-merge.py",
            ),
            ws,
        ),
        wait_merge_helper=_hook_argv(
            raw,
            "wait_merge_helper",
            _default_if_exists(
                ["bash", str(ws / "scripts/github/pr-address-wait-and-merge.sh")],
                ws / "scripts/github/pr-address-wait-and-merge.sh",
            ),
            ws,
        ),
        arc_manager=_hook_argv(
            raw,
            "arc_manager",
            _default_if_exists(
                ["python3", str(ws / "scripts/tasks/arc_manager.py")],
                ws / "scripts/tasks/arc_manager.py",
            ),
            ws,
        ),
        assigned_issue_ack=_hook_argv(
            raw,
            "assigned_issue_ack",
            _default_if_exists(
                [
                    "python3",
                    str(ws / "scripts/project_monitoring_assigned_issue_ack.py"),
                ],
                ws / "scripts/project_monitoring_assigned_issue_ack.py",
            ),
            ws,
        ),
        claim_tool=_hook_argv(raw, "claim_tool", ["uv", "run", "coordination"], ws),
        legacy_record_append=_hook_argv(
            raw,
            "legacy_record_append",
            _default_if_exists(
                ["uv", "run", "python3", str(ws / "scripts/session-records.py")],
                ws / "scripts/session-records.py",
            ),
            ws,
        ),
        post_run=_hook_argv(raw, "post_run", None, ws),
        git_pull=partial(git_pull_with_retry, ws),
        post_session=post_session,
        make_store=make_store,
        make_record=make_record,
        build_worker_result=_import_attr(
            "agent_events.worker_results", "build_worker_result"
        ),
        write_worker_result=_import_attr(
            "agent_events.worker_results", "write_worker_result"
        ),
        load_worker_result=_import_attr(
            "agent_events.worker_results", "load_worker_result"
        ),
        append_latency_records=_import_attr(
            "metaproductivity.project_monitoring_latency", "append_latency_records"
        ),
        capture_latency_context=_import_attr(
            "metaproductivity.project_monitoring_latency", "capture_latency_context"
        ),
        upgrade_outcome=_import_attr(
            "metaproductivity.pr_outcome", "upgrade_outcome_from_pr_state"
        ),
    )

    # Merge-lifecycle I/O (step 1's subprocess adapter, lib.sh:530-539 paths)
    self_merge_check = ws / "scripts/github/self-merge-check.py"
    self_merge_script = ws / "scripts/github/self-merge-if-eligible.sh"
    greptile_helper = ws / "scripts/github/greptile-helper.sh"
    hooks.self_merge_gate_available = self_merge_script.is_file() and os.access(
        self_merge_script, os.X_OK
    )
    hooks.greptile_helper_available = greptile_helper.is_file() and os.access(
        greptile_helper, os.X_OK
    )
    if self_merge_check.is_file():
        hooks.merge_lifecycle_io = SubprocessMergeLifecycleIO(
            self_merge_check_cmd=["python3", str(self_merge_check)],
            self_merge_cmd=[str(self_merge_script)],
            greptile_helper=str(greptile_helper),
            env={
                "WORKSPACE_REPO": config.self_merge_repos,
                "SELF_MERGE_ALLOWED_PATHS": config.self_merge_allowed_paths,
            },
            promote_state=partial(promote_item_state, config),
        )

    return hooks
