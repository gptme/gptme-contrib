"""Package evidence for the dashboard's code-size section."""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from .codesize import DEFAULT_THRESHOLDS, build_report


def _package_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    return re.sub(r"[-_.]+", "-", match[0]).lower() if match else ""


def _manifests(workspace: Path) -> list[Path]:
    paths = [workspace / "pyproject.toml", *sorted(workspace.glob("packages/*/pyproject.toml"))]
    # Agent brains often symlink contrib packages into their own workspace.
    # Reading those as dependents would count contrib's declarations again.
    return [path for path in paths if path.is_file() and path.resolve().is_relative_to(workspace)]


def _read_project(path: Path) -> dict:
    with path.open("rb") as manifest:
        project = tomllib.load(manifest).get("project", {})
    if not isinstance(project, dict):
        raise ValueError(f"{path.name} has an invalid [project] table")
    return project


def _dependencies(project: dict) -> set[str]:
    requirements = list(project.get("dependencies", []))
    for optional in project.get("optional-dependencies", {}).values():
        requirements.extend(optional)
    return {_package_name(req) for req in requirements}


def _maturity(project: dict) -> str:
    for classifier in project.get("classifiers", []):
        if classifier.startswith(
            ("Development Status :: 1", "Development Status :: 2", "Development Status :: 3")
        ):
            return "experimental"
        if classifier.startswith(("Development Status :: 5", "Development Status :: 6")):
            return "stable"
        if classifier.startswith("Development Status ::"):
            return "unspecified"
    version = str(project.get("version", ""))
    if re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)*", version):
        return "stable"
    return "unspecified"


def collect_codesize_section(
    workspace: Path,
    *,
    agent_repos: tuple[Path, ...] = (),
    ci_packages: set[str] | None = None,
) -> dict:
    """Use declared Python dependencies as evidence for package review."""
    workspace = workspace.resolve()
    paths = _manifests(workspace)
    projects = {
        path.parent.relative_to(workspace).as_posix(): _read_project(path) for path in paths
    }
    # Package evidence must stay per-package even when the detected profile is
    # brain/metarepo or a repo-level group map merges package directories.
    package_groups = {
        path: path if (workspace / Path(path).name).is_dir() else Path(path).name
        for path in projects
        if path.startswith("packages/")
    }
    report = build_report(workspace, code_only=True, group_map=package_groups)
    data = report.to_dict()
    del data["root"]

    internal: Counter[str] = Counter()
    for project in projects.values():
        dependencies = _dependencies(project)
        dependencies.discard(_package_name(project.get("name", "")))
        internal.update(dependencies)

    external: Counter[str] = Counter()
    checked_repos = {path.resolve() for path in agent_repos} - {workspace}
    for repo in sorted(checked_repos):
        manifests = _manifests(repo)
        if not manifests:
            raise ValueError("each --dependent-repo must contain a Python project manifest")
        external.update(set().union(*(_dependencies(_read_project(p)) for p in manifests)))

    threshold = DEFAULT_THRESHOLDS[0]
    oversized = report.over_threshold[str(threshold)]
    package_rows = []
    prior_sample = (
        report.history[-2] if report.profile != "metarepo" and len(report.history) >= 2 else None
    )
    inactive_before = datetime.now(timezone.utc).date() - timedelta(days=90)
    for group in data["groups"]:
        package_project = projects.get(group["path"])
        if package_project is None or not group["path"].startswith("packages/"):
            continue

        name = _package_name(package_project.get("name", Path(group["path"]).name))
        maturity = _maturity(package_project)
        last_commit = group["last_commit"]
        flags = []
        if external[name] >= 2 and maturity == "stable":
            flags.append("Upstream")
        if (
            checked_repos
            and not internal[name]
            and not external[name]
            and maturity == "experimental"
            and last_commit
            and date.fromisoformat(last_commit) < inactive_before
        ):
            flags.append("Remove")
        if any(f.path.startswith(group["path"] + "/") for f in oversized):
            flags.append("Split")

        package_rows.append(
            {
                **group,
                "source_delta": (
                    group["source_lines"] - prior_sample.group_source_lines.get(group["name"], 0)
                    if prior_sample is not None
                    else None
                ),
                "internal_dependents": internal[name],
                "external_dependents": external[name] if checked_repos else None,
                "ci_selected": (
                    Path(group["path"]).name in ci_packages if ci_packages is not None else None
                ),
                "flags": flags,
            }
        )
    data["packages"] = package_rows
    data["agent_repos_scanned"] = len(checked_repos)
    data["comparison_week"] = prior_sample.week if prior_sample is not None else None
    return data
