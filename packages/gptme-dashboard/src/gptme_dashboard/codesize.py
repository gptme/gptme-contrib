"""Repo-agnostic code-size and review-pain report.

The measure set is deliberately small and chosen for how well it predicts review
pain, not how easy it is to count:

- **largest source files first** — a 3,000-line module is hard to review no matter
  how small the repo is (the `cascade-selector.py` / `self-review.py` lesson);
- **lines of code per group**, split into source and tests, so a monorepo shows
  which package is carrying the weight and which one is untested;
- **hotspots**: file size x change frequency over a window, i.e. code that is both
  big and churning — the highest-value refactor target;
- **history**: the same totals sampled from git at weekly points, so a trend is
  visible without storing any state.

Profiles only decide how files are *grouped*; the measures are shared. A monorepo
groups by package, a brain groups by workspace surface, a metarepo groups by
submodule, a monolith groups by top-level source directory.

Nothing here deletes or rewrites anything: the report is a review aid.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Directories that never belong to the report: generated output, vendored deps,
# virtualenvs, caches and build trees.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "_site",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    "site-packages",
    ".next",
    "coverage",
}

SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".vue",
    ".kt",
    ".java",
    ".go",
    ".c",
    ".h",
    ".cpp",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".rst",
}

DEFAULT_THRESHOLDS = (1500, 3000)

# A file counts as a test when any path component says so.
TEST_MARKERS = ("test_", "_test.", "tests/", "/tests/", "test/", "/test/", "spec.")


@dataclass
class FileSize:
    path: str
    lines: int
    group: str
    is_test: bool
    changes: int = 0

    @property
    def hotspot_score(self) -> float:
        """Size weighted by churn: big files that also change often hurt most."""
        if self.changes <= 0:
            return 0.0
        return self.lines * math.sqrt(1.0 + self.changes)


@dataclass
class GroupSize:
    name: str
    path: str = ""
    source_lines: int = 0
    test_lines: int = 0
    source_files: int = 0
    test_files: int = 0
    last_commit: str = ""

    @property
    def tests_ratio(self) -> float:
        total = self.source_lines + self.test_lines
        return round(self.test_lines / total, 3) if total else 0.0


@dataclass
class HistoryPoint:
    week: str
    commit: str
    source_lines: int
    test_lines: int
    largest_file: str = ""
    largest_file_lines: int = 0


@dataclass
class CodeSizeReport:
    root: str
    profile: str
    generated_at: str
    thresholds: list[int]
    groups: list[GroupSize] = field(default_factory=list)
    largest_files: list[FileSize] = field(default_factory=list)
    over_threshold: dict[str, list[FileSize]] = field(default_factory=dict)
    hotspots: list[FileSize] = field(default_factory=list)
    history: list[HistoryPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_source_lines(self) -> int:
        return sum(g.source_lines for g in self.groups)

    @property
    def total_test_lines(self) -> int:
        return sum(g.test_lines for g in self.groups)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["totals"] = {
            "source_lines": self.total_source_lines,
            "test_lines": self.total_test_lines,
            "source_files": sum(g.source_files for g in self.groups),
            "test_files": sum(g.test_files for g in self.groups),
        }
        data["groups"] = [{**asdict(g), "tests_ratio": g.tests_ratio} for g in self.groups]
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _is_git_repo(root: Path) -> bool:
    return bool(_git(root, "rev-parse", "--git-dir").strip())


def _change_counts(root: Path, since: str) -> dict[str, int]:
    """Number of commits touching each path since ``since`` (git date spec)."""
    out = _git(root, "log", f"--since={since}", "--name-only", "--pretty=format:")
    counts: dict[str, int] = {}
    for line in out.splitlines():
        path = line.strip()
        if path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def _last_commit_date(root: Path, path: str) -> str:
    out = _git(root, "log", "-1", "--format=%cs", "--", path).strip()
    return out


# --------------------------------------------------------------------------- #
# profile detection
# --------------------------------------------------------------------------- #


def detect_profile(root: Path) -> str:
    """Classify the repo so the report groups files the way that repo is shaped."""
    if (root / ".gitmodules").exists():
        modules = _git(root, "config", "--file", ".gitmodules", "--get-regexp", "path")
        if len([ln for ln in modules.splitlines() if ln.strip()]) >= 2:
            return "metarepo"
    if (root / "packages").is_dir() and any(
        (p / "pyproject.toml").exists() for p in (root / "packages").iterdir() if p.is_dir()
    ):
        return "monorepo"
    if (root / "tasks").is_dir() and (root / "lessons").is_dir() and (root / "journal").is_dir():
        return "brain"
    return "monolith"


def _load_group_map(root: Path, group_map: dict[str, str] | None) -> dict[str, str]:
    """Prefix -> group override. Accepts an inline dict or a JSON file path."""
    if group_map is None:
        candidate = root / "codesize-groups.json"
        if not candidate.exists():
            return {}
        try:
            group_map = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {k.strip("/"): v for k, v in group_map.items()}


def _group_path(rel_path: str, profile: str, overrides: dict[str, str]) -> str:
    """The directory that defines a group, used for its last-commit date."""
    for prefix in overrides:
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return prefix
    parts = rel_path.split("/")
    if profile == "monorepo" and parts[0] == "packages" and len(parts) > 1:
        return f"packages/{parts[1]}"
    return parts[0] if len(parts) > 1 else "."


def assign_group(root: Path, rel_path: str, profile: str, overrides: dict[str, str]) -> str:
    for prefix, name in overrides.items():
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return name
    parts = rel_path.split("/")
    if profile == "monorepo":
        if parts[0] == "packages" and len(parts) > 1:
            return parts[1]
        return parts[0] if len(parts) > 1 else "(root)"
    # metarepo, brain and monolith all group by the first path component
    return parts[0] if len(parts) > 1 else "(root)"


# --------------------------------------------------------------------------- #
# working-tree scan
# --------------------------------------------------------------------------- #


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _count_lines(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _is_test_path(rel_path: str) -> bool:
    lowered = rel_path.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def iter_source_files(root: Path):
    """Yield (relative path, absolute path) for tracked-looking source files."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        yield str(rel), path


def build_report(
    repo: Path,
    *,
    group_map: dict[str, str] | None = None,
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
    history_weeks: int = 8,
    hotspot_window: str = "90 days ago",
    largest_n: int = 15,
) -> CodeSizeReport:
    """Compute the size report for ``repo``."""
    root = Path(repo).resolve()
    profile = detect_profile(root)
    overrides = _load_group_map(root, group_map)
    report = CodeSizeReport(
        root=str(root),
        profile=profile,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        thresholds=sorted(thresholds),
    )

    groups: dict[str, GroupSize] = {}
    files: list[FileSize] = []
    change_counts = _change_counts(root, hotspot_window) if _is_git_repo(root) else {}

    for rel, path in iter_source_files(root):
        lines = _count_lines(path)
        if lines == 0:
            continue
        group = assign_group(root, rel, profile, overrides)
        is_test = _is_test_path(rel)
        entry = groups.setdefault(
            group,
            GroupSize(name=group, path=_group_path(rel, profile, overrides)),
        )
        if is_test:
            entry.test_lines += lines
            entry.test_files += 1
        else:
            entry.source_lines += lines
            entry.source_files += 1
        files.append(
            FileSize(
                path=rel,
                lines=lines,
                group=group,
                is_test=is_test,
                changes=change_counts.get(rel, 0),
            )
        )

    for sized in groups.values():
        if _is_git_repo(root):
            sized.last_commit = _last_commit_date(root, sized.path)

    source_files = [f for f in files if not f.is_test]
    source_files.sort(key=lambda f: f.lines, reverse=True)
    report.groups = sorted(groups.values(), key=lambda g: g.source_lines, reverse=True)
    report.largest_files = source_files[:largest_n]
    for threshold in report.thresholds:
        report.over_threshold[str(threshold)] = [f for f in source_files if f.lines >= threshold]
    report.hotspots = sorted(
        (f for f in source_files if f.changes > 0),
        key=lambda f: f.hotspot_score,
        reverse=True,
    )[:largest_n]

    if _is_git_repo(root):
        report.history = _history(root, history_weeks, profile, overrides)
    else:
        report.warnings.append("not a git repository: history trend unavailable")

    return report


# --------------------------------------------------------------------------- #
# history trend from git
# --------------------------------------------------------------------------- #


def _weekly_commits(root: Path, weeks: int) -> list[tuple[str, str]]:
    """One (iso week, commit) pair per week, oldest first, for the last N weeks."""
    out = _git(
        root,
        "rev-list",
        f"--since={weeks * 7 + 7}.days.ago",
        "--reverse",
        "HEAD",
    )
    commits = [c for c in out.split() if c]
    by_week: dict[str, str] = {}
    for commit in commits:
        date = _git(root, "show", "-s", "--format=%cs", commit).strip()
        if not date:
            continue
        try:
            iso = datetime.strptime(date, "%Y-%m-%d").isocalendar()
        except ValueError:
            continue
        by_week[f"{iso.year}-W{iso.week:02d}"] = commit
    return sorted(by_week.items())


def _lines_at_commit(root: Path, commit: str) -> list[tuple[str, int]]:
    """Line counts for every text source file at ``commit`` via one git grep."""
    patterns = [f"*{suffix}" for suffix in sorted(SOURCE_SUFFIXES)]
    out = _git(root, "grep", "-c", "-I", "", commit, "--", *patterns)
    rows: list[tuple[str, int]] = []
    prefix = f"{commit}:"
    for line in out.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        path, _, count = rest.rpartition(":")
        if not path or not count.isdigit():
            continue
        if any(part in SKIP_DIRS for part in path.split("/")[:-1]):
            continue
        rows.append((path, int(count)))
    return rows


def _history(
    root: Path,
    weeks: int,
    profile: str,
    overrides: dict[str, str],
) -> list[HistoryPoint]:
    points: list[HistoryPoint] = []
    for week, commit in _weekly_commits(root, weeks):
        rows = _lines_at_commit(root, commit)
        if not rows:
            continue
        source = test = 0
        largest_path, largest_lines = "", 0
        for path, lines in rows:
            if _is_test_path(path):
                test += lines
            else:
                source += lines
                if lines > largest_lines:
                    largest_path, largest_lines = path, lines
        points.append(
            HistoryPoint(
                week=week,
                commit=commit[:10],
                source_lines=source,
                test_lines=test,
                largest_file=largest_path,
                largest_file_lines=largest_lines,
            )
        )
    return points


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render_markdown(report: CodeSizeReport) -> str:
    """Human-readable report. Largest source files come first, by design."""
    lines: list[str] = [
        f"# Code size report — {Path(report.root).name}",
        "",
        (
            f"Profile: **{report.profile}** · "
            f"{report.total_source_lines:,} source lines · "
            f"{report.total_test_lines:,} test lines · "
            f"generated {report.generated_at}"
        ),
        "",
    ]
    if report.warnings:
        lines += [f"> warning: {w}" for w in report.warnings] + [""]

    lines += ["## Largest source files", ""]
    if report.largest_files:
        lines += ["| Lines | File | Group | Changes |", "|---:|---|---|---:|"]
        for f in report.largest_files:
            lines.append(f"| {f.lines:,} | `{f.path}` | {f.group} | {f.changes} |")
    else:
        lines.append("_no source files found_")
    lines.append("")

    lines += ["## Over threshold", ""]
    for threshold in report.thresholds:
        offenders = report.over_threshold.get(str(threshold), [])
        names = ", ".join(f"`{f.path}` ({f.lines:,})" for f in offenders) or "none"
        lines.append(f"- **≥ {threshold:,} lines** ({len(offenders)}): {names}")
    lines.append("")

    lines += ["## Hotspots (size × churn)", ""]
    if report.hotspots:
        lines += ["| Score | Lines | Changes | File |", "|---:|---:|---:|---|"]
        for f in report.hotspots:
            lines.append(f"| {f.hotspot_score:.0f} | {f.lines:,} | {f.changes} | `{f.path}` |")
    else:
        lines.append("_no churn data (not a git repo or no changes in window)_")
    lines.append("")

    lines += [
        "## By group",
        "",
        "| Group | Source | Tests | Test ratio | Last commit |",
        "|---|---:|---:|---:|---|",
    ]
    for g in report.groups:
        lines.append(
            f"| {g.name} | {g.source_lines:,} | {g.test_lines:,} | "
            f"{g.tests_ratio:.0%} | {g.last_commit or '—'} |"
        )
    lines.append("")

    if report.history:
        lines += [
            "## Trend (weekly git samples)",
            "",
            "| Week | Commit | Source | Tests | Largest file |",
            "|---|---|---:|---:|---|",
        ]
        for p in report.history:
            largest = f"`{p.largest_file}` ({p.largest_file_lines:,})" if p.largest_file else "—"
            lines.append(
                f"| {p.week} | {p.commit} | {p.source_lines:,} | {p.test_lines:,} | {largest} |"
            )
        lines.append("")
    return "\n".join(lines)
