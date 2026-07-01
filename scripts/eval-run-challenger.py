#!/usr/bin/env python3
"""Run a challenger model against Bob's production eval traces (idea #567 Phase 2).

Loads traces produced by the brain repo's `scripts/extract-eval-traces.py`
(Phase 1), replays each trace's oracle input against a challenger model in an
isolated worktree, and reports behavioral-envelope comparison metrics:
commits-made match, duration ratio, and outcome match. See the design doc:
knowledge/technical-designs/2026-06-22-sovereign-ai-model-eval-harness.md
(brain repo).

Usage:
  # Inspect which traces are usable (no model calls, no worktree needed)
  python3 scripts/eval-run-challenger.py --traces state/eval-traces/traces-2026-06-22.jsonl --dry-run

  # Live run: replay up to 2 traces against a challenger command in an isolated worktree
  python3 scripts/eval-run-challenger.py --traces traces.jsonl \\
      --brain-worktree /tmp/worktrees/brain-challenger-run \\
      --challenger-cmd "claude -p --no-session-persistence" \\
      --limit 2 --output state/eval-results/run-2026-07-01.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "packages" / "gptme-sessions" / "src"),
)

from gptme_sessions.transcript import read_transcript  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 900
# Env vars that must be cleared before spawning a nested `claude -p` subprocess,
# otherwise it silently attaches to the parent session and returns empty stdout.
NESTED_SUBPROCESS_ENV_BLOCKLIST = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CC_SESSION_ID",
    "CC_MODEL",
)


@dataclass
class ChallengerResult:
    """Outcome of replaying one trace's oracle input against the challenger."""

    exit_code: int
    duration_seconds: float
    commits_made: int
    files_changed: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    timed_out: bool = False


def load_traces(
    path: Path, *, category: str | None = None, limit: int | None = None
) -> list[dict]:
    """Load trace records with a usable trajectory_path, optionally filtered."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("trajectory_path"):
                continue
            if not Path(rec["trajectory_path"]).expanduser().exists():
                continue
            if category and rec.get("category") != category:
                continue
            records.append(rec)
    if limit is not None:
        records = records[:limit]
    return records


def extract_oracle_input(trajectory_path: str) -> str | None:
    """Extract the first user message from a session trajectory.

    This reconstructs the CASCADE-injected prompt the oracle session actually
    received, which is what the challenger is replayed against.
    """
    transcript = read_transcript(Path(trajectory_path).expanduser())
    for message in transcript.messages:
        if message.role == "user" and message.content:
            return str(message.content)
    return None


def _git_head(worktree: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_commits_since(worktree: Path, base_sha: str) -> tuple[int, list[str]]:
    """Return (commit_count, changed_files) made in *worktree* since base_sha."""
    count_out = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    files_out = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", base_sha, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    files = [line for line in files_out.splitlines() if line]
    return int(count_out or "0"), files


def run_challenger(
    oracle_input: str,
    *,
    worktree: Path,
    challenger_cmd: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ChallengerResult:
    """Replay *oracle_input* against the challenger command inside *worktree*.

    The worktree must be a disposable checkout (isolated brain worktree, no
    push). Commits are measured by diffing HEAD before/after the run.
    """
    base_sha = _git_head(worktree)
    env = os.environ.copy()
    for key in NESTED_SUBPROCESS_ENV_BLOCKLIST:
        env.pop(key, None)

    argv = shlex.split(challenger_cmd) + [oracle_input]
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(worktree),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        exit_code = proc.returncode
        stdout_tail = proc.stdout[-2000:] if proc.stdout else ""
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        timed_out = True
        raw_stdout = exc.stdout
        stdout_tail = (
            raw_stdout.decode("utf-8", errors="replace")
            if isinstance(raw_stdout, bytes)
            else (raw_stdout or "")
        )[-2000:]
    duration = time.monotonic() - start

    commits_made, files_changed = _git_commits_since(worktree, base_sha)
    return ChallengerResult(
        exit_code=exit_code,
        duration_seconds=duration,
        commits_made=commits_made,
        files_changed=files_changed,
        stdout_tail=stdout_tail,
        timed_out=timed_out,
    )


def compute_metrics(oracle: dict, challenger: ChallengerResult) -> dict:
    """Compare challenger behavior against the oracle's recorded envelope."""
    oracle_commits = oracle.get("commits_made", 0)
    oracle_duration = oracle.get("duration_seconds", 0)
    oracle_outcome = oracle.get("outcome", "unknown")

    challenger_outcome = "productive" if challenger.commits_made >= 1 else "noop"
    commits_made_match = (oracle_commits >= 1) == (challenger.commits_made >= 1)
    outcome_match = challenger_outcome == oracle_outcome
    duration_ratio = (
        challenger.duration_seconds / oracle_duration if oracle_duration else None
    )

    return {
        "commits_made_match": commits_made_match,
        "outcome_match": outcome_match,
        "duration_ratio": duration_ratio,
        "oracle_commits_made": oracle_commits,
        "challenger_commits_made": challenger.commits_made,
        "oracle_outcome": oracle_outcome,
        "challenger_outcome": challenger_outcome,
        # Composite score per the design doc, LLM-judge term omitted (0) unless
        # supplied by the caller via --llm-judge.
        "composite_score": (
            0.4 * commits_made_match
            + 0.2 * outcome_match
            + 0.4 * (1.0 if commits_made_match and outcome_match else 0.0)
        ),
    }


def build_report(results: list[dict]) -> str:
    """Render a markdown win/tie/loss table grouped by category."""
    by_category: dict[str, list[dict]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)

    lines = [
        "| Category | N | Commits Match | Outcome Match | Avg Composite |",
        "|---|---|---|---|---|",
    ]
    for category, rows in sorted(by_category.items()):
        n = len(rows)
        commits_match = sum(1 for r in rows if r["metrics"]["commits_made_match"])
        outcome_match = sum(1 for r in rows if r["metrics"]["outcome_match"])
        avg_composite = sum(r["metrics"]["composite_score"] for r in rows) / n
        lines.append(
            f"| {category} | {n} | {commits_match}/{n} | {outcome_match}/{n} | {avg_composite:.2f} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--traces",
        type=Path,
        required=True,
        help="Path to trace JSONL (Phase 1 output)",
    )
    parser.add_argument("--category", help="Filter to a single category")
    parser.add_argument("--limit", type=int, help="Max traces to process")
    parser.add_argument(
        "--brain-worktree",
        type=Path,
        help="Disposable brain-repo worktree to replay the challenger in (required unless --dry-run)",
    )
    parser.add_argument(
        "--challenger-cmd",
        default="claude -p --no-session-persistence",
        help="Command to invoke the challenger model; oracle input is appended as the final argv token",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--output", type=Path, help="Write per-trace comparison JSONL here"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only extract oracle inputs and report trace usability; no model calls",
    )
    args = parser.parse_args()

    traces = load_traces(args.traces, category=args.category, limit=args.limit)
    if not traces:
        print(
            "No usable traces (need trajectory_path pointing at an existing file).",
            file=sys.stderr,
        )
        return 1

    if not args.dry_run and not args.brain_worktree:
        parser.error("--brain-worktree is required unless --dry-run is set")

    results = []
    for trace in traces:
        oracle_input = extract_oracle_input(trace["trajectory_path"])
        if oracle_input is None:
            print(
                f"skip {trace['trace_id']}: no user message in transcript",
                file=sys.stderr,
            )
            continue

        if args.dry_run:
            print(
                f"{trace['trace_id']} [{trace['category']}]: oracle_input {len(oracle_input)} chars — usable"
            )
            continue

        challenger = run_challenger(
            oracle_input,
            worktree=args.brain_worktree,
            challenger_cmd=args.challenger_cmd,
            timeout=args.timeout,
        )
        metrics = compute_metrics(trace, challenger)
        results.append(
            {
                "trace_id": trace["trace_id"],
                "category": trace["category"],
                "challenger": asdict(challenger),
                "metrics": metrics,
            }
        )
        print(f"{trace['trace_id']} [{trace['category']}]: {metrics}")

    if results:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                for r in results:
                    f.write(json.dumps(r) + "\n")
        print()
        print(build_report(results))

    return 0


if __name__ == "__main__":
    sys.exit(main())
