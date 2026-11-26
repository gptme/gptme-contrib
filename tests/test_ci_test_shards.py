"""Guard: the package and plugin test matrices stay bundled into shards.

Both workflows used to run one job per package/plugin. A test there takes
~0.5 min, so most of each job was fixed per-job overhead (runner provisioning,
checkout, Python/uv setup), and every job held one of the gptme org's 20
shared concurrent job slots. `discover` now bundles the list into a few
shards and each `test` job loops over its shard. These tests fail if a later
edit (or a squash of an older branch) silently goes back to a per-item matrix,
or if the shard split drops or duplicates an item.
"""

import json
import math
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "test-packages.yml": "packages",
    "test-plugins.yml": "plugins",
}


def _workflow(name: str) -> dict:
    workflow: dict = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return workflow


def _shard_script(name: str) -> str:
    steps = _workflow(name)["jobs"]["discover"]["steps"]
    step = next(step for step in steps if step.get("id") == "shard")
    script: str = step["run"]
    return script


def _run_shard_step(name: str, items: list[str], tmp_path: Path) -> list[dict]:
    output = tmp_path / "github_output"
    output.write_text("")
    subprocess.run(
        ["bash", "-e", "-c", _shard_script(name)],
        env={**os.environ, "ITEMS": json.dumps(items), "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
        text=True,
    )
    (line,) = output.read_text().splitlines()
    key, _, value = line.partition("=")
    assert key == "shards"
    shards: list[dict] = json.loads(value)
    return shards


@pytest.mark.parametrize("name", WORKFLOWS)
def test_test_matrix_is_shard_based(name):
    workflow = _workflow(name)
    kind = WORKFLOWS[name]
    discover = workflow["jobs"]["discover"]
    assert discover["outputs"]["shards"] == "${{ steps.shard.outputs.shards }}"
    test = workflow["jobs"]["test"]
    matrix = test["strategy"]["matrix"]
    assert set(matrix) == {"python-version", "shard"}, (
        "the test matrix must be python-version x shard, not one job per "
        f"{kind[:-1]}: {sorted(matrix)}"
    )
    assert matrix["shard"] == "${{ fromJson(needs.discover.outputs.shards) }}"
    assert test["strategy"]["fail-fast"] is False
    assert test["timeout-minutes"] == "${{ matrix.shard.timeout }}"
    assert "shard ${{ matrix.shard.id }}/${{ matrix.shard.of }}" in test["name"]
    loop = next(
        step for step in test["steps"] if "in the shard" in step.get("name", "")
    )
    assert loop["env"][f"SHARD_{kind.upper()}"] == f"${{{{ matrix.shard.{kind} }}}}"


def test_package_shard_runs_the_per_package_make_target():
    test = _workflow("test-packages.yml")["jobs"]["test"]
    loop = next(
        step for step in test["steps"] if "in the shard" in step.get("name", "")
    )
    assert 'make test-package PKG="$pkg"' in loop["run"]


def test_plugin_shard_installs_each_plugin_in_a_fresh_venv():
    test = _workflow("test-plugins.yml")["jobs"]["test"]
    loop = next(
        step for step in test["steps"] if "in the shard" in step.get("name", "")
    )
    assert 'python3 -m venv "$RUNNER_TEMP/venv-$1"' in loop["run"]
    assert '(test_plugin "$plugin")' in loop["run"]


@pytest.mark.parametrize("name", WORKFLOWS)
@pytest.mark.parametrize("n", [0, 1, 8, 9, 19, 35, 100])
def test_shards_cover_every_item_once(name, n, tmp_path):
    kind = WORKFLOWS[name]
    items = [f"item-{i:03d}" for i in range(n)]
    shards = _run_shard_step(name, list(reversed(items)), tmp_path)
    count = min(5, math.ceil(n / 8))
    assert len(shards) == count
    assert [shard["id"] for shard in shards] == list(range(1, count + 1))
    assert all(shard["of"] == count for shard in shards)
    members = [item for shard in shards for item in shard[kind].split()]
    assert sorted(members) == items, "every item must run in exactly one shard"
    # Round-robin over the sorted list: deterministic, and sizes differ by <= 1.
    for i, shard in enumerate(shards):
        assert shard[kind].split() == items[i::count]
        assert 10 < shard["timeout"] <= 25


def test_package_shard_keeps_going_and_names_failures(tmp_path):
    """Run the shard loop with a stand-in make that fails one package."""
    test = _workflow("test-packages.yml")["jobs"]["test"]
    loop = next(
        step for step in test["steps"] if "in the shard" in step.get("name", "")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make = bin_dir / "make"
    make.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$2" >> "$CALL_LOG"\n' '[ "$2" != "PKG=bad-pkg" ]\n'
    )
    make.chmod(0o755)
    log = tmp_path / "calls.log"
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-c", loop["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "GITHUB_STEP_SUMMARY": str(summary),
            "SHARD_PACKAGES": "good-a bad-pkg good-b",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert log.read_text().split() == ["PKG=good-a", "PKG=bad-pkg", "PKG=good-b"]
    assert "::error::Package tests failed: bad-pkg" in result.stdout
    assert "::group::good-b" in result.stdout
    assert "bad-pkg" in summary.read_text()
