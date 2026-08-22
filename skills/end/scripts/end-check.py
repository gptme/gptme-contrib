#!/usr/bin/env python3
"""Session wrap-up gate for the `/end` skill.

Runtime-agnostic (Claude Code, gptme, Codex): stdlib only, reads the harness
from the environment / process tree, and reports whether the session is safe
to close.

Exit codes:
    0  clean   — nothing blocks ending the session
    2  blocked — uncommitted/unpushed work, dirty worktrees, missing journal
    1  error   — could not run the checks (not a git repo, etc.)

The verdict line is the contract the SKILL.md acts on:
    VERDICT: BLOCKED            -> refuse to wrap up, fix the blockers
    VERDICT: CLEAN_LIGHT        -> wrap up and exit the harness
    VERDICT: CLEAN_SUBSTANTIAL  -> wrap up, hand off, stay alive for review
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HARNESS_BY_ARGV0 = {
    "claude": "claude-code",
    "gptme": "gptme",
    "codex": "codex",
}


# --------------------------------------------------------------------------- #
# Harness / session discovery
# --------------------------------------------------------------------------- #


@dataclass
class Harness:
    name: str = "unknown"
    pid: int | None = None
    cmdline: str = ""
    started_at: datetime | None = None
    session_id: str | None = None


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_ppid(pid: int) -> int | None:
    for line in _read(f"/proc/{pid}/status").splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _proc_cmdline(pid: int) -> list[str]:
    raw = _read(f"/proc/{pid}/cmdline")
    return [a for a in raw.split("\0") if a]


def _proc_start_time(pid: int) -> datetime | None:
    """Process start time from /proc/<pid>/stat + /proc/stat btime."""
    stat = _read(f"/proc/{pid}/stat")
    if not stat or ")" not in stat:
        return None
    # Field 22 (1-based) is starttime, counted after the ")" of comm.
    fields = stat.rsplit(")", 1)[1].split()
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None
    btime = None
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime "):
            btime = int(line.split()[1])
            break
    if btime is None:
        return None
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return datetime.fromtimestamp(btime + start_ticks / hz, tz=timezone.utc)


def _classify_cmdline(argv: list[str]) -> str | None:
    """Map a process cmdline to a harness name, or None."""
    if not argv:
        return None
    # argv[0] may be an interpreter (node/python); look at the first two args.
    for arg in argv[:2]:
        base = os.path.basename(arg)
        base = re.sub(r"\.(js|py|exe)$", "", base)
        if base in HARNESS_BY_ARGV0:
            return HARNESS_BY_ARGV0[base]
    return None


def find_harness() -> Harness:
    h = Harness()
    env_pid = os.environ.get("CLAUDE_PID")
    if env_pid and env_pid.isdigit() and os.path.exists(f"/proc/{env_pid}"):
        h.name = "claude-code"
        h.pid = int(env_pid)
    else:
        pid: int | None = os.getppid()
        hops = 0
        while pid and pid > 1 and hops < 12:
            kind = _classify_cmdline(_proc_cmdline(pid))
            if kind:
                h.name, h.pid = kind, pid
                break
            pid = _proc_ppid(pid)
            hops += 1
        if h.pid is None:
            # Env-only fallbacks (process tree unavailable, e.g. sandboxed).
            if os.environ.get("CLAUDECODE"):
                h.name = "claude-code"
            elif os.environ.get("GPTME_AGENT_NAME") or os.environ.get("GPTME_LOGS_DIR"):
                h.name = "gptme"
            elif os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
                h.name = "codex"
    if h.pid:
        h.cmdline = " ".join(_proc_cmdline(h.pid))[:200]
        h.started_at = _proc_start_time(h.pid)
    h.session_id = (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CC_SESSION_ID")
        or os.environ.get("GPTME_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )
    return h


def parse_since(value: str) -> datetime:
    """Accept ISO-8601 or a relative duration like '90m', '2h', '1d'."""
    m = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #


def git(args: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "").rstrip("\n")


def repo_root(start: Path) -> Path | None:
    rc, out = git(["rev-parse", "--show-toplevel"], start)
    return Path(out) if rc == 0 and out else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.lstat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _is_submodule(root: Path, rel: str) -> bool:
    rc, out = git(["ls-files", "-s", "--", rel], root)
    return rc == 0 and out.startswith("160000 ")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    name: str
    status: str  # ok | info | warn | block
    summary: str
    items: list[str] = field(default_factory=list)


@dataclass
class Report:
    harness: Harness
    root: Path
    since: datetime | None
    since_source: str
    checks: list[Check] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    files_changed: int = 0
    prs: list[dict] = field(default_factory=list)
    verdict: str = ""
    reason: str = ""

    def add(self, c: Check) -> None:
        self.checks.append(c)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.status == "block"]


def _touched_this_session(path: Path, since: datetime | None) -> bool:
    if since is None:
        return True
    mt = _mtime(path)
    return mt is None or mt >= since


def _dirty_entries(
    root: Path, since: datetime | None
) -> tuple[list[str], list[str], list[str], str | None]:
    """Classify `git status` entries: (this-session, older, submodules, error)."""
    rc, out = git(["status", "--porcelain", "--untracked-files=normal", "-z"], root)
    if rc != 0:
        return [], [], [], out
    entries = [e for e in out.split("\0") if e]
    mine: list[str] = []
    others: list[str] = []
    submods: list[str] = []
    i = 0
    while i < len(entries):
        e = entries[i]
        code, rel = e[:2], e[3:]
        if code[0] == "R" or code[1] == "R":  # rename: next entry is the source
            i += 1
        i += 1
        if _is_submodule(root, rel):
            submods.append(rel)
            continue
        label = f"{code.strip() or '??'} {rel}"
        if _touched_this_session(root / rel, since):
            mine.append(label)
        else:
            others.append(label)
    return mine, others, submods, None


def check_dirty(rep: Report) -> None:
    mine, others, submods, err = _dirty_entries(rep.root, rep.since)
    if err is not None:
        rep.add(Check("uncommitted", "warn", "git status failed", [err]))
        return
    if mine:
        rep.add(
            Check(
                "uncommitted",
                "block",
                f"{len(mine)} uncommitted change(s) touched this session",
                mine[:40],
            )
        )
    else:
        rep.add(Check("uncommitted", "ok", "no uncommitted changes from this session"))
    if others:
        rep.add(
            Check(
                "uncommitted-other",
                "info",
                f"{len(others)} dirty path(s) older than this session (other sessions / pre-existing)",
                others[:15],
            )
        )
    if submods:
        rep.add(Check("submodules", "info", "submodule pointer(s) differ", submods))


def check_commits(rep: Report) -> None:
    if rep.since is None:
        rep.add(
            Check(
                "commits", "info", "session start unknown — commit attribution skipped"
            )
        )
        return
    since_iso = rep.since.isoformat()
    rc, out = git(
        ["log", f"--since={since_iso}", "--format=%h%x1f%s%x1f%b%x1e", "-n", "200"],
        rep.root,
    )
    if rc != 0:
        rep.add(Check("commits", "warn", "git log failed", [out]))
        return
    subjects: list[str] = []
    shas: list[str] = []
    sid = rep.harness.session_id
    records = [r for r in out.split("\x1e") if r.strip()]
    tagged = []
    for r in records:
        parts = r.strip("\n").split("\x1f")
        if len(parts) < 2:
            continue
        sha, subject = parts[0], parts[1]
        body = parts[2] if len(parts) > 2 else ""
        if sid and sid[:8] in body:
            tagged.append((sha, subject))
        subjects.append(f"{sha} {subject}")
        shas.append(sha)
    # If any commit carries this session's id, trust the tag over the time window.
    if tagged:
        subjects = [f"{s} {j}" for s, j in tagged]
        shas = [s for s, _ in tagged]
    rep.commits = subjects
    if shas:
        rc, out = git(["diff", "--shortstat", f"{shas[-1]}^", shas[0]], rep.root)
        m = re.search(r"(\d+) files? changed", out) if rc == 0 else None
        rep.files_changed = int(m.group(1)) if m else 0
    rep.add(
        Check(
            "commits",
            "info",
            f"{len(subjects)} commit(s) attributed to this session",
            subjects[:20],
        )
    )


def _newest_commit_date(cwd: Path, rev_range: list[str]) -> datetime | None:
    rc, out = git(["log", *rev_range, "--format=%cI", "-n", "50"], cwd)
    if rc != 0:
        return None
    newest = None
    for line in out.splitlines():
        try:
            dt = datetime.fromisoformat(line.strip())
        except ValueError:
            continue
        if newest is None or dt > newest:
            newest = dt
    return newest


def check_unpushed(rep: Report, cwd: Path, label: str) -> tuple[Check, datetime | None]:
    """Returns the check plus the newest unpushed commit date (None if nothing unpushed)."""
    rc, up = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    if rc != 0:
        rc2, cnt = git(["rev-list", "--count", "HEAD", "--not", "--remotes"], cwd)
        n = int(cnt) if rc2 == 0 and cnt.isdigit() else 0
        if n:
            newest = _newest_commit_date(cwd, ["HEAD", "--not", "--remotes"])
            return (
                Check(
                    label,
                    "block",
                    f"branch has no upstream and {n} commit(s) not on any remote",
                    ["publish it: git push -u origin HEAD"],
                ),
                newest,
            )
        return Check(label, "ok", "no upstream, nothing unpublished"), None
    rc, out = git(["log", "@{u}..HEAD", "--format=%h %s"], cwd)
    lines = [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    if lines:
        newest = _newest_commit_date(cwd, ["@{u}..HEAD"])
        return (
            Check(
                label, "block", f"{len(lines)} commit(s) not pushed to {up}", lines[:20]
            ),
            newest,
        )
    return Check(label, "ok", f"in sync with {up}"), None


def check_worktrees(rep: Report) -> None:
    rc, out = git(["worktree", "list", "--porcelain"], rep.root)
    if rc != 0:
        return
    paths: list[Path] = []
    for block in out.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("worktree "):
                p = Path(line[len("worktree ") :])
                if p.resolve() != rep.root.resolve():
                    paths.append(p)
    blocked: list[str] = []
    infos: list[str] = []
    for wt in paths:
        if not wt.exists():
            infos.append(f"{wt} (missing on disk — `git worktree prune`)")
            continue
        # Display the checkout path, not a gitdir (submodule main checkouts list as .git/modules/...)
        rc, top = git(["rev-parse", "--show-toplevel"], wt)
        shown = top if rc == 0 and top else str(wt)
        mine, others, _, err = _dirty_entries(wt, rep.since)
        if err is not None:
            infos.append(f"{shown}: git status failed")
            continue
        unpushed, newest = check_unpushed(rep, wt, "worktree")
        unpushed_recent = unpushed.status == "block" and (
            rep.since is None or newest is None or newest >= rep.since
        )
        recent_problems: list[str] = []
        old_problems: list[str] = []
        if mine:
            recent_problems.append(f"{len(mine)} uncommitted change(s)")
        elif others:
            old_problems.append(f"{len(others)} uncommitted change(s)")
        if unpushed.status == "block":
            (recent_problems if unpushed_recent else old_problems).append(
                unpushed.summary
            )
        if recent_problems:
            blocked.append(f"{shown}: {', '.join(recent_problems)}")
        elif old_problems:
            infos.append(
                f"{shown}: {', '.join(old_problems)} (older than this session)"
            )
    if blocked:
        rep.add(
            Check(
                "worktrees",
                "block",
                f"{len(blocked)} worktree(s) with unsaved work from this session",
                blocked,
            )
        )
    elif infos:
        rep.add(
            Check("worktrees", "info", "other worktrees with leftover state", infos)
        )
    elif paths:
        rep.add(Check("worktrees", "ok", f"{len(paths)} linked worktree(s), all clean"))


def check_journal(rep: Report) -> None:
    jdir = rep.root / "journal"
    if not jdir.is_dir():
        return
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = jdir / today
    recent = []
    if today_dir.is_dir():
        for f in today_dir.glob("*.md"):
            if _touched_this_session(f, rep.since):
                recent.append(str(f.relative_to(rep.root)))
    did_work = bool(rep.commits) or any(
        c.name == "uncommitted" and c.status == "block" for c in rep.checks
    )
    if recent:
        rep.add(Check("journal", "ok", "journal entry written this session", recent))
    elif did_work:
        rep.add(
            Check(
                "journal",
                "block",
                f"work happened this session but no journal entry in journal/{today}/",
                [f"write journal/{today}/<session-file>.md before ending"],
            )
        )
    else:
        rep.add(
            Check("journal", "info", "no journal entry (no attributed work either)")
        )


def check_prs(rep: Report, timeout: int = 20) -> None:
    if not shutil.which("gh"):
        rep.add(Check("prs", "info", "gh not installed — PR check skipped"))
        return
    since_day = (
        rep.since or (datetime.now(timezone.utc) - timedelta(days=1))
    ).strftime("%Y-%m-%d")
    try:
        p = subprocess.run(
            [
                "gh",
                "search",
                "prs",
                "--author=@me",
                "--state=open",
                f"--created=>={since_day}",
                "--json",
                "url,title,number,repository",
                "--limit",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        rep.add(Check("prs", "info", f"gh search failed: {e}"))
        return
    if p.returncode != 0:
        rep.add(
            Check("prs", "info", "gh search failed (auth?)", [p.stderr.strip()[:200]])
        )
        return
    try:
        prs = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        prs = []
    if not prs:
        rep.add(Check("prs", "ok", "no open PRs opened since session start"))
        return
    red: list[str] = []
    listing: list[str] = []
    for pr in prs[:10]:
        url = pr.get("url", "")
        repo = (pr.get("repository") or {}).get("nameWithOwner", "")
        listing.append(f"{repo}#{pr.get('number')} {pr.get('title', '')} — {url}")
        try:
            c = subprocess.run(
                ["gh", "pr", "checks", url],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if c.returncode != 0 and "fail" in (c.stdout + c.stderr).lower():
                red.append(f"{repo}#{pr.get('number')}: failing checks")
        except (OSError, subprocess.TimeoutExpired):
            pass
    rep.prs = prs
    if red:
        rep.add(
            Check("prs", "warn", f"{len(red)} PR(s) with failing CI", red + listing)
        )
    else:
        rep.add(
            Check("prs", "info", f"{len(prs)} open PR(s) from this session", listing)
        )


# --------------------------------------------------------------------------- #
# Verdict + rendering
# --------------------------------------------------------------------------- #


def decide(rep: Report, light_commits: int, light_files: int) -> None:
    if rep.blockers:
        rep.verdict = "BLOCKED"
        rep.reason = "; ".join(c.summary for c in rep.blockers)
        return
    substantial = (
        len(rep.commits) > light_commits
        or rep.files_changed > light_files
        or bool(rep.prs)
        or any(c.name == "worktrees" and c.status != "ok" for c in rep.checks)
        or any(c.status == "warn" for c in rep.checks)
    )
    if substantial:
        rep.verdict = "CLEAN_SUBSTANTIAL"
        rep.reason = (
            f"{len(rep.commits)} commit(s), {rep.files_changed} file(s), "
            f"{len(rep.prs)} PR(s) — worth a human look before closing"
        )
    else:
        rep.verdict = "CLEAN_LIGHT"
        rep.reason = f"{len(rep.commits)} commit(s), {rep.files_changed} file(s), nothing pending"


STATUS_ICON = {"ok": "✅", "info": "ℹ️ ", "warn": "⚠️ ", "block": "❌"}


def render(rep: Report) -> str:
    h = rep.harness
    lines = [
        f"# /end check — {rep.root}",
        f"harness: {h.name}" + (f" (pid {h.pid})" if h.pid else ""),
        f"session start: {rep.since.isoformat(timespec='seconds') if rep.since else 'unknown'}"
        f" [{rep.since_source}]",
        "",
    ]
    for c in rep.checks:
        lines.append(f"{STATUS_ICON.get(c.status, '•')} {c.name}: {c.summary}")
        for it in c.items:
            lines.append(f"      - {it}")
    lines.append("")
    lines.append(f"VERDICT: {rep.verdict} — {rep.reason}")
    if rep.verdict == "BLOCKED":
        lines.append("→ Do NOT wrap up. Fix the ❌ items, then re-run this check.")
    elif rep.verdict == "CLEAN_LIGHT":
        lines.append(
            "→ Write the closing summary, then exit the harness (scripts/end-exit.py)."
        )
    else:
        lines.append(
            "→ Write the closing summary + hand-off and STAY ALIVE for review."
        )
    return "\n".join(lines)


def to_json(rep: Report) -> dict:
    return {
        "root": str(rep.root),
        "harness": {
            "name": rep.harness.name,
            "pid": rep.harness.pid,
            "session_id": rep.harness.session_id,
            "started_at": rep.harness.started_at.isoformat()
            if rep.harness.started_at
            else None,
        },
        "since": rep.since.isoformat() if rep.since else None,
        "since_source": rep.since_source,
        "checks": [
            {"name": c.name, "status": c.status, "summary": c.summary, "items": c.items}
            for c in rep.checks
        ],
        "commits": rep.commits,
        "files_changed": rep.files_changed,
        "prs": rep.prs,
        "verdict": rep.verdict,
        "reason": rep.reason,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--workspace", type=Path, default=Path.cwd(), help="any path inside the repo"
    )
    ap.add_argument(
        "--since",
        help="session start: ISO-8601 or relative (90m, 2h). Default: harness process start",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-prs", action="store_true", help="skip the gh PR check")
    ap.add_argument(
        "--light-commits",
        type=int,
        default=2,
        help="max commits for CLEAN_LIGHT (default 2)",
    )
    ap.add_argument(
        "--light-files",
        type=int,
        default=10,
        help="max files changed for CLEAN_LIGHT (default 10)",
    )
    args = ap.parse_args(argv)

    root = repo_root(args.workspace)
    if root is None:
        print(
            f"error: {args.workspace} is not inside a git repository", file=sys.stderr
        )
        return 1

    harness = find_harness()
    if args.since:
        since, source = parse_since(args.since), "--since"
    elif harness.started_at:
        since, source = harness.started_at, f"{harness.name} process start"
    else:
        since, source = None, "unknown (all dirty files count as this session's)"

    rep = Report(harness=harness, root=root, since=since, since_source=source)
    check_dirty(rep)
    check_commits(rep)
    rep.add(check_unpushed(rep, root, "unpushed")[0])
    check_worktrees(rep)
    check_journal(rep)
    if not args.no_prs:
        check_prs(rep)
    decide(rep, args.light_commits, args.light_files)

    if args.json:
        print(json.dumps(to_json(rep), indent=2))
    else:
        print(render(rep))
    return 2 if rep.verdict == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
