"""Publishing must consume the directory uv build actually writes to."""

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_publish_build_uses_package_local_dist() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build package")
    publish = next(step for step in steps if step.get("name") == "Publish to PyPI")
    assert build["working-directory"] == publish["working-directory"]
    args = shlex.split(build["run"])
    # In a uv workspace, bare `uv build` defaults to the workspace-root dist,
    # whereas `uv publish` searches dist relative to its working directory.
    assert "--out-dir" in args, "build must override uv's workspace-root dist default"
    assert args[args.index("--out-dir") + 1] == "dist"
    assert shlex.split(publish["run"]) == [
        "uv",
        "publish",
        "--trusted-publishing",
        "always",
    ]
