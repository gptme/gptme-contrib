"""The shared task-frontmatter pre-commit hook must agree with gptodo's schema.

Regression for gptme-contrib#2161: the hook kept its own hard-coded state list,
which drifted from gptodo's. `gptodo expire` writes `state: expired` (canonical
in gptodo), and the hook rejected it, so any workspace using the hook had
commits blocked by tasks gptodo itself produced.

The hook now loads states and priorities from gptodo. These tests fail if any
state gptodo accepts is rejected by the hook entry point.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "precommit" / "validate_task_frontmatter.py"
GPTODO_SRC = REPO_ROOT / "packages" / "gptodo" / "src"


def _gptodo_states() -> tuple[list[str], list[str]]:
    """(canonical states, deprecated aliases) straight from gptodo's source."""
    if str(GPTODO_SRC) not in sys.path:
        sys.path.insert(0, str(GPTODO_SRC))
    from gptodo.utils import (  # type: ignore[import-not-found]
        DEPRECATED_STATE_ALIASES,
        get_canonical_states,
    )

    return list(get_canonical_states()), list(DEPRECATED_STATE_ALIASES)


def _run(
    script: Path, args: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run([str(script), *args], capture_output=True, text=True, cwd=cwd)


def _task(tmp_path: Path, name: str, frontmatter: str) -> Path:
    f = tmp_path / f"{name}.md"
    f.write_text(f"---\n{frontmatter}created: 2026-07-01\n---\n# Task\n")
    return f


def test_every_gptodo_state_is_accepted(tmp_path: Path) -> None:
    canonical, aliases = _gptodo_states()
    assert "expired" in canonical, "test premise: gptodo expire writes this state"
    files = [_task(tmp_path, s, f"state: {s}\n") for s in canonical + aliases]
    # Run from an unrelated cwd: the hook must locate gptodo relative to itself.
    result = _run(SCRIPT, [str(f) for f in files], cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_invalid_state_lists_gptodo_canonical_states(tmp_path: Path) -> None:
    canonical, aliases = _gptodo_states()
    result = _run(SCRIPT, [str(_task(tmp_path, "bad", "state: bogus\n"))])
    assert result.returncode == 1
    output = " ".join((result.stdout + result.stderr).split())
    assert "Invalid state: bogus" in output
    for state in canonical:
        assert (
            state in output
        ), f"canonical state {state!r} missing from hint: {output!r}"


def test_priority_follows_gptodo(tmp_path: Path) -> None:
    ok = [
        _task(tmp_path, f"p-{p}", f"state: todo\npriority: {p}\n")
        for p in ("high", "medium", "low", "null")
    ]
    result = _run(SCRIPT, [str(f) for f in ok])
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    # `urgent` is a PRIORITY_RANK sort key in gptodo, not a valid field value.
    bad = _task(tmp_path, "p-urgent", "state: todo\npriority: urgent\n")
    result = _run(SCRIPT, [str(bad)])
    assert result.returncode == 1
    assert "Invalid priority: urgent" in result.stdout + result.stderr


def test_fails_loudly_without_gptodo(tmp_path: Path) -> None:
    """A copy of the hook detached from its gptme-contrib checkout must not
    silently fall back to a stale local schema — it must fail and say why."""
    detached = tmp_path / "scripts" / "precommit" / SCRIPT.name
    detached.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, detached)
    task = _task(tmp_path, "t", "state: todo\n")
    result = _run(detached, [str(task)])
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "Cannot load the task schema from gptodo" in " ".join(result.stderr.split())


def test_tweets_do_not_need_gptodo(tmp_path: Path) -> None:
    detached = tmp_path / "scripts" / "precommit" / SCRIPT.name
    detached.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, detached)
    tweet = tmp_path / "tweet.yml"
    tweet.write_text("---\nstate: approved\ncreated: 2026-07-01\n---\n")
    result = _run(detached, ["--type", "tweets", str(tweet)])
    assert result.returncode == 0, f"stderr={result.stderr!r}"


def test_hook_has_no_hardcoded_task_state_list() -> None:
    """The second-copy drift is what #2161 fixed; keep it from coming back."""
    source = SCRIPT.read_text()
    for literal in ('"cancelled"', '"ready_for_review"', '"backlog"'):
        assert (
            literal not in source
        ), f"hard-coded task state {literal} in {SCRIPT.name}"
