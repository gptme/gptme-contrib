"""Regression test for scripts/precommit/validate_task_frontmatter.py exit code.

`@click.command()` discards return values from the callback, so any earlier
`sys.exit(main())` resolved to `sys.exit(None)` (which exits 0). This let the
precommit hook silently pass when validate_frontmatter() reported errors.

Real impact: see ErikBjare/bob session 21b1 (2026-04-25), where the broken
next_action in tasks/gptme-tauri-recurring-user-testing.md slipped past
precommit and disappeared from CASCADE for 2 days.

Invokes the script as a subprocess so the test doesn't depend on click being
installed in the bare test environment — uv resolves the script's inline
PEP 723 dependencies on each invocation.
"""

import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "precommit" / "validate_task_frontmatter.py"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke the script via its own shebang so uv resolves PEP 723 deps."""
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _make_task(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "test-task.md"
    f.write_text(content)
    return f


def test_exits_zero_on_valid_task(tmp_path: Path) -> None:
    f = _make_task(
        tmp_path,
        "---\nstate: active\ncreated: 2026-03-02T10:00:00+02:00\n---\n# Task\n",
    )
    result = _run([str(f)])
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_exits_nonzero_on_unparseable_yaml(tmp_path: Path) -> None:
    """Reproducer: unquoted block scalar with `:1` (backtick-colon-digit)
    causes PyYAML to parse the continuation as a mapping."""
    f = _make_task(
        tmp_path,
        "---\n"
        "state: backlog\n"
        "created: '2026-04-25T00:00:00+00:00'\n"
        "next_action: Run user-testing loop on `v0.31.1.dev202604277` (or later) on\n"
        "  display `:1` — verify settings polish from #2247/#2248 in packaged build,\n"
        "  then complete one real in-app message/tool roundtrip\n"
        "---\n# Task\n",
    )
    result = _run([str(f)])
    assert result.returncode != 0, (
        "regression: validator must exit non-zero when frontmatter "
        f"fails to parse — stderr={result.stderr!r}"
    )
    assert "Failed to parse frontmatter" in (result.stdout + result.stderr)


def test_exits_nonzero_on_invalid_state(tmp_path: Path) -> None:
    f = _make_task(
        tmp_path,
        "---\nstate: bogus\ncreated: 2026-03-02\n---\n# Task\n",
    )
    result = _run([str(f)])
    assert result.returncode != 0


def test_exits_nonzero_on_missing_required(tmp_path: Path) -> None:
    f = _make_task(tmp_path, "---\npriority: high\n---\n# Task\n")
    result = _run([str(f)])
    assert result.returncode != 0


def test_no_files_exits_clean() -> None:
    """precommit can pass empty file lists; should be a no-op."""
    result = _run([])
    assert result.returncode == 0
