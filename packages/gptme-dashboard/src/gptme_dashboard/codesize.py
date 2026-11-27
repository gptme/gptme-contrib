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

Profiles only decide which files belong to the repo and how they are *grouped*;
the measures are shared. A monorepo groups by package, a brain groups by workspace
surface, a metarepo groups by submodule, a monolith groups by top-level source
directory. Only a metarepo counts submodule contents; elsewhere a submodule is
someone else's code.

In a git repository only tracked files are counted, in the current numbers and in
the trend alike. The trend reads commits (and, in a metarepo, the submodule commits
they pin); the current numbers read the working tree.

Nothing here deletes or rewrites anything: the report is a review aid.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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
    ".svelte",
    ".html",
    ".css",
    ".scss",
    ".sql",
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

CONTENT_SUFFIXES = {".md", ".rst", ".toml", ".yaml", ".yml"}

DEFAULT_THRESHOLDS = (1500, 3000)

# The file-size-ratchet pre-commit hook's baseline. When a repo has one, its
# threshold is always reported, so the report flags what the gate enforces.
RATCHET_BASELINE = "file-size-baseline.json"

# Generated files that carry a source suffix and would otherwise top every list.
SKIP_FILES = {"pnpm-lock.yaml"}

# Any of these under packages/<name>/ marks a monorepo.
PACKAGE_MANIFESTS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")

# A file counts as a test when a directory or its file name says so. Matching is
# on whole path components: "latest/" and "contest_rules.py" are not tests.
TEST_DIRS = {"test", "tests", "__tests__", "androidtest"}
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}


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

    def to_dict(self) -> dict:
        return {**asdict(self), "hotspot_score": round(self.hotspot_score, 1)}


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
    threshold_counts: dict[str, int] = field(default_factory=dict)
    group_source_lines: dict[str, int] = field(default_factory=dict)


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
    code_only: bool = False

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
        data["largest_files"] = [f.to_dict() for f in self.largest_files]
        data["hotspots"] = [f.to_dict() for f in self.hotspots]
        data["over_threshold"] = {
            threshold: [f.to_dict() for f in files]
            for threshold, files in self.over_threshold.items()
        }
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


# User config that would change what the output means or looks like.
PINNED_CONFIG = (
    "core.quotePath=false",  # non-ASCII paths must come out verbatim to match up
    "grep.fullName=false",  # cwd-relative paths
    "submodule.recurse=false",  # grep must not descend; submodules are handled here
    "log.showSignature=false",  # no signature text mixed into formatted log output
)


def _git(root: Path, *args: str) -> str:
    pinned = [arg for setting in PINNED_CONFIG for arg in ("-c", setting)]
    proc = subprocess.run(
        ["git", *pinned, "-C", str(root), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _is_git_repo(root: Path) -> bool:
    return bool(_git(root, "rev-parse", "--git-dir").strip())


def _declared_submodules(root: Path) -> list[str]:
    """Submodule paths listed in this repo's own ``.gitmodules``."""
    if not (root / ".gitmodules").exists():
        return []
    out = _git(root, "config", "--file", ".gitmodules", "-z", "--get-regexp", r"\.path$")
    # -z gives "key\nvalue\0": both the key and the path may contain spaces
    return [entry.split("\n", 1)[1] for entry in out.split("\0") if "\n" in entry]


def _submodules(root: Path) -> tuple[list[str], list[str]]:
    """(checked-out, missing) submodule paths relative to ``root``, nested included."""
    present: list[str] = []
    missing: list[str] = []
    for path in _declared_submodules(root):
        # Without its own .git, `git -C path` would silently answer for the parent.
        if not (root / path / ".git").exists():
            missing.append(path)
            continue
        present.append(path)
        nested_present, nested_missing = _submodules(root / path)
        present += [f"{path}/{p}" for p in nested_present]
        missing += [f"{path}/{p}" for p in nested_missing]
    return present, missing


def _off_pin_submodules(root: Path) -> list[str]:
    """Submodules checked out at a commit other than the one their parent pins."""
    out = _git(root, "submodule", "status", "--recursive")
    # "+<sha> <path> (<describe>)"; the path may contain spaces
    return [
        line.split(" ", 1)[1].rsplit(" (", 1)[0]
        for line in out.splitlines()
        if line.startswith("+")
    ]


def _change_counts(root: Path, since: str) -> dict[str, int]:
    """Number of commits touching each path since ``since``, relative to ``root``."""
    out = _git(
        root,
        "log",
        f"--since={since}",
        "--name-only",
        "-z",
        "--relative",
        "--pretty=format:",
        "--",
        ".",
    )
    return dict(Counter(path for path in out.split("\0") if path))


def _last_commit_date(root: Path, path: str, submodules: list[str]) -> str:
    # "." is the (root) group: top-level files only, not the whole tree.
    pathspec = ":(glob)*" if path == "." else path
    # Inside a submodule, ask the submodule: the parent only sees pin bumps.
    for sub in sorted(submodules, key=len, reverse=True):
        if path == sub or path.startswith(sub + "/"):
            root, pathspec = root / sub, path[len(sub) :].lstrip("/") or "."
            break
    return _git(root, "log", "-1", "--format=%cs", "--", pathspec).strip()


# --------------------------------------------------------------------------- #
# profile detection
# --------------------------------------------------------------------------- #


def detect_profile(root: Path) -> str:
    """Classify the repo so the report groups files the way that repo is shaped."""
    # Brain first: agent workspaces usually also have packages/ and submodules.
    if (root / "tasks").is_dir() and (root / "lessons").is_dir() and (root / "journal").is_dir():
        return "brain"
    if len(_declared_submodules(root)) >= 2:
        return "metarepo"
    if (root / "packages").is_dir() and any(
        (package / manifest).exists()
        for package in (root / "packages").iterdir()
        for manifest in PACKAGE_MANIFESTS
    ):
        return "monorepo"
    return "monolith"


def parse_group_map(data: object) -> dict[str, str]:
    """Validate a prefix -> group name mapping. Raises ``ValueError``."""
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError("group map must be a JSON object of path prefix -> group name")
    return {k.strip("/"): v for k, v in data.items()}


def _load_group_map(
    root: Path, group_map: dict[str, str] | None, warnings: list[str]
) -> dict[str, str]:
    """The caller's override, else the repo's ``codesize-groups.json`` if usable."""
    if group_map is not None:
        return parse_group_map(group_map)
    candidate = root / "codesize-groups.json"
    if not candidate.exists():
        return {}
    try:
        return parse_group_map(json.loads(candidate.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        warnings.append(f"ignoring {candidate.name}: {exc}")
        return {}


def assign_group(
    rel_path: str,
    profile: str,
    overrides: dict[str, str],
    submodules: list[str] | None = None,
    shadowed: set[str] | None = None,
) -> tuple[str, str]:
    """(group name, directory that defines it) for a file.

    The longest matching prefix wins, so overrides and submodules may nest.
    ``shadowed`` names packages that share a name with a top-level directory.
    """
    for prefix in sorted(overrides, key=len, reverse=True):
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return overrides[prefix], prefix
    for sub in sorted(submodules or [], key=len, reverse=True):
        if rel_path.startswith(sub + "/"):
            return sub, sub
    parts = rel_path.split("/")
    if profile == "monorepo" and parts[0] == "packages" and len(parts) > 2:
        path = f"packages/{parts[1]}"
        return (path if parts[1] in (shadowed or ()) else parts[1]), path
    # everything else groups by the first path component
    return (parts[0], parts[0]) if len(parts) > 1 else ("(root)", ".")


# --------------------------------------------------------------------------- #
# working-tree scan
# --------------------------------------------------------------------------- #


def _shadowed_packages(root: Path) -> set[str]:
    packages = root / "packages"
    if not packages.is_dir():
        return set()
    return {p.name for p in packages.iterdir() if p.is_dir() and (root / p.name).is_dir()}


def ratchet_threshold(root: Path, warnings: list[str] | None = None) -> int | None:
    """The line threshold the repo's file-size ratchet enforces, if it has one."""
    candidate = root / RATCHET_BASELINE
    if not candidate.exists():
        return None
    try:
        value = json.loads(candidate.read_text(encoding="utf-8")).get("threshold", 2000)
        return int(value)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if warnings is not None:
            warnings.append(f"ignoring {candidate.name}: {exc}")
        return None


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _count_lines(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data or _looks_binary(data):
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _is_test_path(rel_path: str) -> bool:
    *dirs, name = rel_path.lower().split("/")
    if any(d in TEST_DIRS for d in dirs):
        return True
    path = Path(name)
    if name == "conftest.py" or name.startswith("test_") or path.stem.endswith("_test"):
        return True
    # foo.test.ts / foo.spec.tsx; an openapi.spec.yaml is not a test
    return path.suffix in JS_SUFFIXES and path.stem.endswith((".test", ".spec"))


def _is_source_path(path: str, code_only: bool = False) -> bool:
    """Whether a path, relative to the repository that tracks it, is reported.

    The current numbers and the trend both filter through this. A submodule's
    mount path is not part of it: one mounted at ``build/toolchain`` still counts.
    """
    *dirs, name = path.split("/")
    suffix = Path(name).suffix.lower()
    return (
        suffix in SOURCE_SUFFIXES
        and (not code_only or suffix not in CONTENT_SUFFIXES)
        and name not in SKIP_FILES
        and not any(d in SKIP_DIRS for d in dirs)
    )


def iter_source_files(root: Path, submodules: list[str] | None = None, *, code_only: bool = False):
    """Yield (relative path, absolute path) for source files.

    In a git repository these are the tracked files of ``root`` and of each listed
    submodule; elsewhere every file.
    """
    if _is_git_repo(root):
        # Ask each repo in turn: `ls-files --recurse-submodules` silently skips
        # a submodule that is checked out but not active.
        # dict.fromkeys: an unmerged path is listed once per index stage.
        candidates = [
            (root / base / p, p)
            for base in ("", *(submodules or []))
            for p in dict.fromkeys(_git(root / base, "ls-files", "-z").split("\0"))
        ]
    else:
        candidates = [(p, p.relative_to(root).as_posix()) for p in root.rglob("*")]
    for path, own_path in candidates:
        # a symlink would count its target a second time
        if not _is_source_path(own_path, code_only) or path.is_symlink() or not path.is_file():
            continue
        yield path.relative_to(root).as_posix(), path


def build_report(
    repo: Path,
    *,
    group_map: dict[str, str] | None = None,
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
    history_weeks: int = 8,
    hotspot_window: str = "90 days ago",
    largest_n: int = 15,
    code_only: bool = False,
) -> CodeSizeReport:
    """Compute the size report for ``repo``."""
    root = Path(repo).resolve()
    profile = detect_profile(root)
    report = CodeSizeReport(
        root=str(root),
        profile=profile,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        thresholds=sorted(set(thresholds)),
        code_only=code_only,
    )
    gate = ratchet_threshold(root, report.warnings)
    if gate is not None:
        report.thresholds = sorted({*report.thresholds, gate})
    overrides = _load_group_map(root, group_map, report.warnings)
    shadowed = _shadowed_packages(root)

    is_git = _is_git_repo(root)
    recurse = profile == "metarepo"
    submodules, missing = _submodules(root) if recurse else ([], [])
    report.warnings += [f"submodule {p} is not checked out: not counted" for p in missing]
    if recurse:
        report.warnings += [
            f"submodule {p} is not at its pinned commit: current numbers and trend differ"
            for p in _off_pin_submodules(root)
        ]

    groups: dict[str, GroupSize] = {}
    group_paths: dict[str, set[str]] = {}
    files: list[FileSize] = []
    change_counts: dict[str, int] = {}
    if is_git:
        change_counts = _change_counts(root, hotspot_window)
        for sub in submodules:
            for path, count in _change_counts(root / sub, hotspot_window).items():
                change_counts[f"{sub}/{path}"] = count

    for rel, path in iter_source_files(root, submodules, code_only=code_only):
        lines = _count_lines(path)
        if lines == 0:
            continue
        group, group_path = assign_group(rel, profile, overrides, submodules, shadowed)
        own_path = rel
        for sub in sorted(submodules, key=len, reverse=True):
            if rel.startswith(sub + "/"):
                own_path = rel[len(sub) + 1 :]
                break
        is_test = _is_test_path(own_path)
        entry = groups.setdefault(group, GroupSize(name=group, path=group_path))
        group_paths.setdefault(group, set()).add(group_path)
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

    if is_git:
        for name, sized in groups.items():
            # several prefixes can map to one group: take the newest
            sized.last_commit = max(
                _last_commit_date(root, p, submodules) for p in group_paths[name]
            )

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

    if not is_git:
        report.warnings.append("not a git repository: history trend unavailable")
    elif history_weeks > 0:
        unreadable: set[str] = set()

        def group_for_path(path: str) -> str:
            return assign_group(path, profile, overrides, submodules, shadowed)[0]

        report.history = _history(
            root,
            history_weeks,
            recurse,
            unreadable,
            code_only,
            report.thresholds,
            group_for_path if not recurse else None,
        )
        report.warnings += [
            f"trend omits submodule {p} where its pinned commit is not available locally"
            for p in sorted(unreadable)
        ]

    return report


# --------------------------------------------------------------------------- #
# history trend from git
# --------------------------------------------------------------------------- #


def _weekly_commits(root: Path, weeks: int) -> list[tuple[str, str]]:
    """One (iso week, commit) pair per week, oldest first, for the last N weeks."""
    now = datetime.now(timezone.utc)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    samples = []
    for ago in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=ago)
        end = min(now, start + timedelta(weeks=1, seconds=-1))
        commit = _git(
            root,
            "log",
            "-1",
            "--first-parent",
            f"--before={end.isoformat()}",
            "--format=%H",
            "HEAD",
        ).strip()
        if commit:
            iso = start.isocalendar()
            samples.append((f"{iso.year}-W{iso.week:02d}", commit))
    return samples


def _lines_at_commit(
    root: Path,
    commit: str,
    recurse: bool,
    unreadable: set[str],
    prefix: str = "",
    *,
    code_only: bool = False,
) -> list[tuple[str, int, bool]]:
    """(workspace path, lines, is_test) for each source file via one git grep.

    With ``recurse``, follows each submodule pin recorded in that commit; pins that
    cannot be read are added to ``unreadable``.
    """
    suffixes = SOURCE_SUFFIXES - CONTENT_SUFFIXES if code_only else SOURCE_SUFFIXES
    patterns = [f":(icase)*{suffix}" for suffix in sorted(suffixes)]
    out = _git(root, "grep", "--no-color", "-z", "-c", "-I", "", commit, "--", *patterns)
    rows: list[tuple[str, int, bool]] = []
    marker = f"{commit}:"
    # -z leaves one NUL between the path and count. Only the count is line-delimited.
    for match in re.finditer(r"([^\0]+)\0([0-9]+)\n", out):
        label, count = match.groups()
        if label.startswith(marker):
            path = label[len(marker) :]
            if _is_source_path(path, code_only):
                # Classify before adding mount prefixes, including nested ones.
                rows.append((prefix + path, int(count), _is_test_path(path)))
    if not recurse:
        return rows
    for entry in _git(root, "ls-tree", "-r", "-z", commit).split("\0"):
        meta, _, path = entry.partition("\t")
        if not meta.startswith("160000 "):
            continue
        pin = meta.split()[2]
        subroot = root / path
        if not (subroot / ".git").exists() or not _git(subroot, "cat-file", "-t", pin):
            unreadable.add(prefix + path)
            continue
        rows += _lines_at_commit(
            subroot, pin, recurse, unreadable, f"{prefix}{path}/", code_only=code_only
        )
    return rows


def _history(
    root: Path,
    weeks: int,
    recurse: bool,
    unreadable: set[str],
    code_only: bool,
    thresholds: list[int],
    group_for_path: Callable[[str], str] | None,
) -> list[HistoryPoint]:
    points: list[HistoryPoint] = []
    previous_commit = ""
    rows: list[tuple[str, int, bool]] = []
    for week, commit in _weekly_commits(root, weeks):
        if commit != previous_commit:
            rows = _lines_at_commit(root, commit, recurse, unreadable, code_only=code_only)
            previous_commit = commit
        source = test = 0
        threshold_counts = dict.fromkeys(map(str, thresholds), 0)
        group_source_lines: Counter[str] = Counter()
        largest_path, largest_lines = "", 0
        for path, lines, is_test in rows:
            if is_test:
                test += lines
            else:
                source += lines
                if group_for_path is not None:
                    group_source_lines[group_for_path(path)] += lines
                for threshold in thresholds:
                    if lines >= threshold:
                        threshold_counts[str(threshold)] += 1
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
                threshold_counts=threshold_counts,
                group_source_lines=dict(group_source_lines),
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
    if report.code_only:
        lines += ["Counts exclude prose and configuration files.", ""]
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
