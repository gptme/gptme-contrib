"""Replay the workflow discovery shell against local Git history and fake Actions runs."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# jq executes the workflow's actual --jq expression rather than duplicating
# the selector in Python. Missing tooling must not masquerade as a regression.
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="requires jq")


@pytest.mark.parametrize("kind", ["packages", "plugins"])
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("success", ["alpha"]),
        ("failed_discovery", ["alpha"]),
        ("only_failure", ["alpha", "beta"]),
        ("in_progress", ["alpha", "beta"]),
        ("empty", ["alpha", "beta"]),
        ("api_error", ["alpha", "beta"]),
        ("nonancestor", ["alpha", "beta"]),
        ("burst", ["alpha"]),
        ("pr", ["alpha"]),
        ("schedule", ["alpha", "beta"]),
    ],
)
def test_discovery_retains_uncovered_changes(tmp_path, kind, scenario, expected):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    # Synthetic history must not invoke the developer's global Git hooks.
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    git("config", "commit.gpgsign", "false")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")

    def commit(path, content):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git("add", path)
        git("commit", "-qm", content)
        return git("rev-parse", "HEAD")

    base = commit("README.md", "baseline")
    git("update-ref", "refs/remotes/origin/master", base)
    commit(f"{kind}/alpha/src.py", "uncovered change")
    before = commit("README.md", "first documentation push")
    commit("README.md", "surviving documentation push")

    def run(run_id, status, conclusion, sha):
        return dict(id=run_id, status=status, conclusion=conclusion, head_sha=sha)

    runs = [run(1, "completed", "success", base)]
    if scenario == "failed_discovery":
        runs.insert(0, run(2, "completed", "failure", before))
    elif scenario == "only_failure":
        runs = [run(2, "completed", "failure", before)]
    elif scenario == "in_progress":
        runs = [run(2, "in_progress", None, before)]
    elif scenario == "empty":
        runs = []
    elif scenario == "nonancestor":
        head = git("rev-parse", "HEAD")
        git("checkout", "--orphan", "unrelated")
        git("rm", "-rf", ".")
        unrelated = commit("unrelated.txt", "other history")
        git("checkout", "--detach", head)
        runs = [run(2, "completed", "success", unrelated)]
    elif scenario == "burst":
        runs = [
            run(i, "completed", "cancelled", before) for i in range(70, 1, -1)
        ] + runs

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make = bin_dir / "make"
    make.write_text(
        "#!/bin/sh\n"
        'case "$*" in *excluded*) exit 0;; esac\n'
        "printf '%s\\n' '[\"alpha\", \"beta\"]'\n"
    )
    make.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "if os.environ['SCENARIO'] == 'api_error': sys.exit(1)\n"
        "runs = json.loads(os.environ['RUNS'])\n"
        "query = parse_qs(urlsplit(sys.argv[2]).query)\n"
        "if query.get('status') == ['success']:\n"
        "    runs = [r for r in runs if r['status'] == 'completed' and r['conclusion'] == 'success']\n"
        "runs = runs[:int(query.get('per_page', ['30'])[0])]\n"
        "result = subprocess.run(['jq', '-r', sys.argv[sys.argv.index('--jq') + 1]],\n"
        "    input=json.dumps({'workflow_runs': runs}), text=True)\n"
        "sys.exit(result.returncode)\n"
    )
    gh.chmod(0o755)
    workflow = yaml.safe_load((ROOT / f".github/workflows/test-{kind}.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["discover"]["steps"] if s.get("id") == "discover"
    )
    event = (
        "pull_request"
        if scenario == "pr"
        else "schedule"
        if scenario == "schedule"
        else "push"
    )
    script = step["run"]
    for key, value in {
        "github.event_name": event,
        "github.repository": "example/repo",
        "github.ref_name": "master",
        "github.run_id": "100",
    }.items():
        script = script.replace("${{ " + key + " }}", value)
    # Keep each replay's matrix files private, including Python's open() paths.
    script = script.replace("/tmp/all_", str(tmp_path / "all_"))
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BASE_REF": "master",
            "BEFORE": before,
            "GITHUB_OUTPUT": str(output),
            "SCENARIO": scenario,
            "RUNS": json.dumps(runs),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text().split("=", 1)[1]) == expected, result.stdout
