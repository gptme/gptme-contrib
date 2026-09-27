"""Command-line interface for run loops."""

import os
import re
import subprocess
import sys
from pathlib import Path

import click

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.email import EmailRun
from gptme_runloops.pr_review.schema import MergeSafety
from gptme_runloops.project_monitoring import ProjectMonitoringRun
from gptme_runloops.team import TeamRun
from gptme_runloops.utils.executor import get_executor, list_backends


@click.group()
def main():
    """Run loop framework for autonomous AI agent operation."""
    pass


def _backend_option(f=None, *, default: str = "gptme", note: str | None = None):
    """Shared --backend option for all commands."""
    backends = list_backends()
    help_text = f"Execution backend (available: {', '.join(backends)})"
    if note:
        help_text = f"{help_text}. {note}"

    option = click.option(
        "--backend",
        default=default,
        type=click.Choice(backends),
        help=help_text,
    )
    return option(f) if f is not None else option


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def autonomous(
    workspace: Path, model: str | None, tool_format: str | None, backend: str
):
    """Run autonomous operation loop."""
    executor = get_executor(backend)
    run = AutonomousRun(
        workspace, model=model, tool_format=tool_format, executor=executor
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def email(workspace: Path, model: str | None, tool_format: str | None, backend: str):
    """Run email processing loop."""
    executor = get_executor(backend)
    run = EmailRun(workspace, model=model, tool_format=tool_format, executor=executor)
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--tools",
    default=None,
    help="Override coordinator tools (default: gptodo,save,append,...)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def team(
    workspace: Path,
    tools: str | None,
    model: str | None,
    tool_format: str | None,
    backend: str,
):
    """Run autonomous team coordination loop.

    The coordinator agent runs with restricted tools and delegates
    all work to subagents via gptodo. Inspired by Claude Code Agent Teams.
    """
    executor = get_executor(backend)
    run = TeamRun(
        workspace,
        tools=tools,
        model=model,
        tool_format=tool_format,
        executor=executor,
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--org",
    "orgs",
    multiple=True,
    help="GitHub organization(s) to monitor (can be specified multiple times)",
)
@click.option(
    "--repo",
    "repos",
    multiple=True,
    help="Specific repository to monitor in owner/repo format (can be specified multiple times)",
)
@click.option(
    "--author",
    default=os.environ.get("GITHUB_AUTHOR", ""),
    help="GitHub username for filtering (default: $GITHUB_AUTHOR env var)",
)
@click.option(
    "--agent-name",
    default=os.environ.get("AGENT_NAME", "Agent"),
    help="Agent name for prompts (default: $AGENT_NAME env var or 'Agent')",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option(
    default="claude-code",
    note=(
        "Monitoring defaults to claude-code because gptme+slow-models is "
        "100% NOOP on monitoring (82/82 sessions over 3 days — produces "
        "analysis but zero concrete actions)"
    ),
)
def monitoring(
    workspace: Path,
    orgs: tuple[str, ...],
    repos: tuple[str, ...],
    author: str,
    agent_name: str,
    model: str | None,
    tool_format: str | None,
    backend: str,
):
    """Run project monitoring loop.

    Monitoring defaults to claude-code (not gptme) because the gptme
    harness with slower/larger models historically produced 100% NOOP
    sessions — verbose analysis output with zero commits or tool actions.
    gptme remains available as an explicit --backend gptme override for
    testing or quota-diversion scenarios.
    """
    executor = get_executor(backend)
    run = ProjectMonitoringRun(
        workspace,
        target_orgs=list(orgs) if orgs else None,
        target_repos=list(repos) if repos else None,
        author=author,
        agent_name=agent_name,
        model=model,
        tool_format=tool_format,
        executor=executor,
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command("run-item")
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Agent workspace repo root",
)
@click.option(
    "--work-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Grouped-item JSONL work file (default: $PM_WORK_FILE)",
)
@click.option(
    "--backend",
    default=None,
    help="Session backend, passed to the runner verbatim (default: $BOB_BACKEND). "
    "Routing stays in the dispatcher — run-item never re-routes.",
)
@click.option(
    "--model",
    default=None,
    help="Model override (default: $BOB_SELECTED_MODEL; empty = backend default)",
)
@click.option(
    "--lane",
    default=None,
    help="Dispatch lane for ledger correlation (default: $PM_LANE, else mixed)",
)
@click.option(
    "--dispatch-id",
    default=None,
    help="Dispatch/unit id for ledger correlation (default: $PM_DISPATCH_ID)",
)
@click.option(
    "--slot-key",
    default=None,
    help="Slot key — selects the per-slot lockfile (default: $PM_SLOT_KEY)",
)
@click.option(
    "--author",
    default=None,
    help="GitHub author login (default: $GITHUB_AUTHOR)",
)
@click.option(
    "--agent-name",
    default=None,
    help="Agent display name for prompts (default: $AGENT_NAME)",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Policy TOML (default: <workspace>/config/pm-run-item.toml if present)",
)
@click.option(
    "--claim-mode",
    type=click.Choice(["acquire", "preheld", "none"]),
    default="acquire",
    help="Coordination-claim handling (preheld is reserved for the "
    "dispatcher-held-claim migration step)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve decisions + render prompts + print the ExecutionPlan JSON; "
    "execute nothing (read-only gate/status probes still run)",
)
def run_item_cmd(
    workspace: Path,
    work_file: Path | None,
    backend: str | None,
    model: str | None,
    lane: str | None,
    dispatch_id: str | None,
    slot_key: str | None,
    author: str | None,
    agent_name: str | None,
    config_path: Path | None,
    claim_mode: str,
    dry_run: bool,
):
    """Execute ONE PM work-file end-to-end (the uniform executor surface).

    Reads a grouped work item (the slot JSONL shape pm_dispatch emits),
    resolves the action via the merge-lifecycle decisions, renders the
    prompt via the prompt templates, executes the session via the runner
    (run.sh), and records outcomes via worker_records — one uniform call
    replacing the PM_DETACHED bash re-exec path.
    """
    import logging
    import signal

    from gptme_runloops.run_item import build_execution_plan, run_work_file
    from gptme_runloops.run_item_config import assemble_hooks, load_run_item_config

    # Progress lines (the bash-echo mirror) go to stderr so --dry-run stdout
    # stays pure ExecutionPlan JSON; under a slot unit both streams hit the
    # journal.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    run_item_logger = logging.getLogger("gptme_runloops.run_item")
    run_item_logger.addHandler(handler)
    run_item_logger.setLevel(logging.INFO)

    # EXIT-trap parity: systemd RuntimeMaxSec sends SIGTERM; convert it to
    # SystemExit so try/finally (claim abandon, lock release, record write
    # cleanup) still runs on timeout-killed slots.
    def _sigterm(_signum, _frame):  # pragma: no cover - signal path
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _sigterm)

    work_file = work_file or (
        Path(os.environ["PM_WORK_FILE"]) if os.environ.get("PM_WORK_FILE") else None
    )
    if work_file is None:
        raise click.UsageError("--work-file is required (or set $PM_WORK_FILE)")
    backend = backend or os.environ.get("BOB_BACKEND", "")
    if not backend:
        raise click.UsageError("--backend is required (or set $BOB_BACKEND)")
    model = model if model is not None else os.environ.get("BOB_SELECTED_MODEL", "")
    lane = lane or os.environ.get("PM_LANE", "mixed")
    dispatch_id = dispatch_id or os.environ.get("PM_DISPATCH_ID", "")
    slot_key = slot_key or os.environ.get("PM_SLOT_KEY", "")

    overrides: dict[str, str] = {}
    if author or os.environ.get("GITHUB_AUTHOR"):
        overrides["author"] = author or os.environ.get("GITHUB_AUTHOR", "")
    if agent_name or os.environ.get("AGENT_NAME"):
        overrides["agent_name"] = agent_name or os.environ.get("AGENT_NAME", "")

    config, raw = load_run_item_config(workspace, config_path, **overrides)
    hooks = assemble_hooks(config, raw)

    if dry_run:
        plan = build_execution_plan(
            work_file,
            config,
            hooks,
            backend=backend,
            model=model,
            lane=lane,
            dispatch_id=dispatch_id,
            slot_key=slot_key,
            claim_mode=claim_mode,
        )
        click.echo(plan.to_json())
        sys.exit(0)

    exit_code = run_work_file(
        work_file,
        config,
        hooks,
        backend=backend,
        model=model,
        lane=lane,
        dispatch_id=dispatch_id,
        slot_key=slot_key,
        claim_mode=claim_mode,
    )
    sys.exit(exit_code)


@main.command()
@click.option(
    "--checkout",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    help="Repository root to review (default: current directory)",
)
@click.option(
    "--working-tree",
    is_flag=True,
    default=False,
    help="Review the current working-tree diff (git diff HEAD). "
    "No PR or forge access needed.",
)
@click.option(
    "--base",
    "base_sha",
    default=None,
    help="Base commit SHA for a local range review (requires --head).",
)
@click.option(
    "--head",
    "head_sha",
    default=None,
    help="Head commit SHA for a local range review (requires --base).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write ReviewArtifact JSON to this path (default: print to stdout).",
)
@click.option(
    "--model",
    default=None,
    help="gptme model spec (e.g. 'anthropic/claude-sonnet-4-6'). "
    "Defaults to gptme's configured model.",
)
def review(
    checkout: Path,
    working_tree: bool,
    base_sha: str | None,
    head_sha: str | None,
    output_path: Path | None,
    model: str | None,
) -> None:
    """Review a local diff and emit a ReviewArtifact JSON.

    Phase 1: local-only, no forge API calls, artifact-only output.

    Examples:

      # Review uncommitted working-tree changes (in-session, before a PR):
      gptme-runloops review --working-tree

      # Review a specific commit range:
      gptme-runloops review --base abc123 --head def456

      # Write the artifact to a file:
      gptme-runloops review --working-tree --output /tmp/review.json

      # Use a specific model:
      gptme-runloops review --working-tree --model anthropic/claude-sonnet-4-6
    """

    from gptme_runloops.pr_review.reviewer import resolve_local_target, run_review

    if not working_tree and not (base_sha and head_sha):
        raise click.UsageError(
            "Specify --working-tree or both --base <SHA> and --head <SHA>."
        )
    if working_tree and (base_sha or head_sha):
        raise click.UsageError(
            "--working-tree is mutually exclusive with --base / --head."
        )

    try:
        target, diff = resolve_local_target(
            checkout,
            working_tree=working_tree,
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"git command failed: {exc.stderr.strip()}") from exc

    if not diff.strip():
        click.echo("No changes to review.", err=True)
        sys.exit(0)

    click.echo(f"Reviewing {target.description} …", err=True)

    try:
        artifact = run_review(
            checkout,
            target,
            diff,
            model=model,
            output_path=output_path,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(f"Invalid review response: {exc}") from exc

    if output_path:
        click.echo(f"Artifact written to {output_path}", err=True)
    else:
        click.echo(artifact.model_dump_json(indent=2))

    # Summary to stderr
    n = len(artifact.findings)
    click.echo(
        f"Review complete — {n} finding(s), merge_safety={artifact.merge_safety.value}",
        err=True,
    )
    # Fail closed: automation must only continue after an explicit safe verdict.
    if artifact.merge_safety != MergeSafety.safe:
        sys.exit(1)


@main.command("review-pr")
@click.argument("pr_ref")
@click.option(
    "--shadow/--publish",
    default=True,
    help="Shadow mode (default): run review but do not post GitHub comments. "
    "--publish posts findings as inline PR review comments.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write ReviewArtifact JSON to this path (default: print to stdout).",
)
@click.option(
    "--model",
    default=None,
    help="gptme model spec (e.g. 'anthropic/claude-sonnet-4-6'). "
    "Defaults to gptme's configured model.",
)
@click.option(
    "--checkout",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Local checkout of the repository, used to read AGENTS.md/CLAUDE.md "
    "for reviewer context. Optional.",
)
@click.option(
    "--min-confidence",
    type=float,
    default=0.6,
    show_default=True,
    help="Skip findings below this confidence threshold when publishing.",
)
def review_pr(
    pr_ref: str,
    shadow: bool,
    output_path: Path | None,
    model: str | None,
    checkout: Path | None,
    min_confidence: float,
) -> None:
    """Review a GitHub pull request and optionally post findings as inline comments.

    PR_REF must be in the form OWNER/REPO#NUM, e.g. ``gptme/gptme-contrib#42``.

    Phase 2: GitHub-backed review with idempotent inline comment publication.

    Examples:

      # Shadow run — review without posting (safe to run on any PR):
      gptme-runloops review-pr gptme/gptme-contrib#42

      # Publish findings as inline PR comments:
      gptme-runloops review-pr gptme/gptme-contrib#42 --publish

      # Shadow run, save artifact:
      gptme-runloops review-pr gptme/gptme-contrib#42 --output /tmp/review.json

      # Use a local checkout for AGENTS.md context:
      gptme-runloops review-pr gptme/gptme-contrib#42 --checkout /path/to/checkout

      # Use a specific model:
      gptme-runloops review-pr gptme/gptme-contrib#42 --model anthropic/claude-sonnet-4-6
    """
    # Parse OWNER/REPO#NUM
    m = re.match(r"^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)$", pr_ref)
    if not m:
        raise click.UsageError(
            f"Invalid PR reference '{pr_ref}'. Expected format: OWNER/REPO#NUM "
            "(e.g. gptme/gptme-contrib#42)."
        )
    repo = m.group(1)
    pr_number = int(m.group(2))

    from gptme_runloops.pr_review.github_adapter import run_github_review

    mode_label = "shadow" if shadow else "publish"
    click.echo(f"Reviewing {repo}#{pr_number} ({mode_label} mode) …", err=True)

    try:
        artifact, posted, skipped = run_github_review(
            repo,
            pr_number,
            model=model,
            checkout=checkout,
            shadow=shadow,
            output_path=output_path,
            min_confidence=min_confidence,
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"GitHub CLI command failed: {exc.stderr.strip() if exc.stderr else exc}"
        ) from exc
    except (RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(f"Review failed: {exc}") from exc

    if output_path:
        click.echo(f"Artifact written to {output_path}", err=True)
    else:
        click.echo(artifact.model_dump_json(indent=2))

    n = len(artifact.findings)
    if shadow:
        click.echo(
            f"Review complete (shadow) — {n} finding(s), merge_safety={artifact.merge_safety.value}",
            err=True,
        )
    else:
        click.echo(
            f"Review complete — {n} finding(s), {posted} posted, {skipped} skipped, "
            f"merge_safety={artifact.merge_safety.value}",
            err=True,
        )

    if artifact.merge_safety != MergeSafety.safe:
        sys.exit(1)


@main.command("select")
@click.option(
    "--profile",
    default=None,
    help="Profile name (reserved for future multi-profile support; currently unused)",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to harness-quota.toml (default: ~/.config/gptme/harness-quota.toml)",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Block-registry state directory (default: ~/.local/share/gptme/block-registry)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def select_cmd(
    profile: str | None,
    config_path: Path | None,
    state_dir: Path | None,
    as_json: bool,
) -> None:
    """Select the first viable backend + model from the candidate list.

    Reads harness-quota.toml, filters out binary-unavailable and block-registry-blocked
    candidates, and prints the first viable {backend, model} pair.

    Exit code 1 when no viable candidate is found.
    """
    import json as _json

    from gptme_runloops.select import load_select_config, select_backend

    try:
        config = load_select_config(config_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not config.candidates:
        raise click.ClickException(
            "No candidates configured. "
            "Create ~/.config/gptme/harness-quota.toml with [[candidates]] entries."
        )

    result = select_backend(config=config, state_dir=state_dir)
    if result is None:
        raise click.ClickException(
            "No viable backend found (all candidates blocked or unavailable)."
        )

    if as_json:
        click.echo(_json.dumps(result.to_dict()))
    else:
        click.echo(f"backend={result.backend} model={result.model}")


@main.command("run")
@click.argument("prompt", required=False)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the prompt from a file instead of the argument",
)
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Working directory (default: current directory)",
)
@click.option(
    "--backend",
    envvar="AGENT_BACKEND",
    default="gptme",
    show_default=True,
    type=click.Choice(list_backends()),
    help="Execution backend (env: AGENT_BACKEND)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (backend-specific; default: backend default)",
)
@click.option(
    "--resume",
    default=None,
    help="session_id from an earlier `run` result to continue",
)
@click.option(
    "--allowed-tool",
    "allowed_tools",
    multiple=True,
    help="Backend-native tool allowlist entry (repeatable): claude-code "
    "--allowedTools pattern, gptme --tools name. Rejected on codex.",
)
@click.option(
    "--sandbox",
    default=None,
    help="Sandbox mode where the backend has one (codex: read-only, "
    "workspace-write [default], danger-full-access)",
)
@click.option(
    "--timeout",
    type=int,
    default=1800,
    show_default=True,
    help="Timeout in seconds",
)
@click.option(
    "--on-resume-failure",
    type=click.Choice(["error", "fresh"]),
    default="error",
    show_default=True,
    help="If the resumed session produced no agent output (backend rejected the "
    "id: unknown/expired session): report the "
    "error, or explicitly start a fresh session (result has resumed=false, "
    "resume_failed=true). Backends that cannot resume at all always error.",
)
@click.option(
    "--fallback-prompt-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Prompt for the fresh session when --on-resume-failure=fresh "
    "(default: the same prompt). Use it to carry prior context.",
)
def run_cmd(
    prompt: str | None,
    prompt_file: Path | None,
    workspace: Path,
    backend: str,
    model: str | None,
    resume: str | None,
    allowed_tools: tuple[str, ...],
    sandbox: str | None,
    timeout: int,
    on_resume_failure: str,
    fallback_prompt_file: Path | None,
):
    """Run ONE prompt on any backend and print the result as a JSON line.

    Output (stdout, one line): backend, model, session_id, result, exit_code,
    is_error, resumed, timed_out, cost_usd (and resume_failed on fallback).
    Pass session_id back via --resume (with the same --backend/--model) to
    continue the session. Exit code 0 on success, 1 on an error result, 2 on
    usage errors (e.g. resume on a backend without resume support).
    """
    import json as _json

    from gptme_runloops.utils.run_once import ResumeNotSupportedError

    if prompt_file:
        prompt = prompt_file.read_text()
    if not prompt:
        raise click.UsageError("PROMPT or --prompt-file is required")
    executor = get_executor(backend)
    tools = list(allowed_tools) or None

    def _once(text: str, resume_id: str | None):
        return executor.run_once(
            text,
            workspace,
            timeout,
            model=model,
            resume=resume_id,
            allowed_tools=tools,
            sandbox=sandbox,
        )

    try:
        result = _once(prompt, resume)
    except (ResumeNotSupportedError, ValueError, NotImplementedError) as e:
        raise click.UsageError(str(e))
    out = result.to_dict()
    if resume and not result.agent_output and not result.timed_out:
        if on_resume_failure == "fresh":
            fresh_prompt = (
                fallback_prompt_file.read_text() if fallback_prompt_file else prompt
            )
            failure = result.result
            result = _once(fresh_prompt, None)
            out = {
                **result.to_dict(),
                "resume_failed": True,
                "resume_error": failure[-500:],
            }
    click.echo(_json.dumps(out))
    sys.exit(1 if out["is_error"] else 0)


if __name__ == "__main__":
    main()
