"""Tests for scripts/git/git-safe-commit — flock-based commit serialization."""

import fcntl
import os
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAFE_COMMIT = REPO_ROOT / "scripts" / "git" / "git-safe-commit"
PRE_COMMIT_HOOK = REPO_ROOT / "scripts" / "git" / "pre-commit-auto-stage"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo for testing."""
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Disable global hooks that block commits to master
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Initial commit so HEAD exists
    (tmp_path / "README.md").write_text("init")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_safe_commit_script_exists():
    """The safe-commit script exists and is executable."""
    assert SAFE_COMMIT.exists()
    assert os.access(SAFE_COMMIT, os.X_OK)


def test_safe_commit_basic(git_repo: Path):
    """Safe commit works for a basic commit with explicit files."""
    test_file = git_repo / "test.txt"
    test_file.write_text("hello")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: basic commit"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    # Verify commit was created
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "test: basic commit" in log.stdout


def test_safe_commit_stages_explicit_dirty_file(git_repo: Path):
    """Documented file-path usage should not require manual pre-staging."""
    test_file = git_repo / "test.txt"
    test_file.write_text("hello")

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: explicit dirty file"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "test: explicit dirty file" in log.stdout

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_safe_commit_creates_lockfile(git_repo: Path):
    """Safe commit creates a lockfile in .git/ during execution."""
    test_file = git_repo / "test.txt"
    test_file.write_text("hello")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    # Run commit
    subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: lockfile"],
        cwd=git_repo,
        capture_output=True,
    )

    # The lockfile should exist (created by flock, persists as empty file)
    lockfile = git_repo / ".git" / "commit.lock"
    assert lockfile.exists()


def test_safe_commit_not_in_git_repo(tmp_path: Path):
    """Safe commit fails gracefully outside a git repo."""
    result = subprocess.run(
        [str(SAFE_COMMIT), "-m", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        "not in a git repository" in result.stderr
        or "not a git repository" in result.stderr
    )


def test_safe_commit_passes_all_args(git_repo: Path):
    """All git commit arguments are passed through correctly."""
    # Test --allow-empty
    result = subprocess.run(
        [str(SAFE_COMMIT), "--allow-empty", "-m", "test: empty commit"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "test: empty commit" in log.stdout


def test_safe_commit_requires_explicit_paths_or_all_staged(git_repo: Path):
    """Bare staged commits must use an explicit opt-in in shared repos."""
    (git_repo / "test.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "-m", "test: implicit all staged blocked"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()
    assert "--all-staged" in result.stderr


def test_safe_commit_error_shows_staged_files_as_concrete_hint(git_repo: Path):
    """When refusing an implicit commit, show staged files + a ready-to-copy command."""
    (git_repo / "test.txt").write_text("hello")
    (git_repo / "other.txt").write_text("world")
    subprocess.run(
        ["git", "add", "test.txt", "other.txt"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "-m", "test: implicit all staged blocked"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    # Lists the staged files so the user sees what they almost committed.
    assert "test.txt" in result.stderr
    assert "other.txt" in result.stderr
    # Shows a concrete copy-paste-ready git-safe-commit invocation; git sorts
    # the staged paths alphabetically.
    assert "git-safe-commit other.txt test.txt" in result.stderr
    # The existing --all-staged hint is still available as a fallback.
    assert "--all-staged" in result.stderr


def test_safe_commit_error_without_staged_files_keeps_generic_hint(git_repo: Path):
    """With nothing staged, the error still shows the generic --all-staged fallback."""
    result = subprocess.run(
        [str(SAFE_COMMIT), "-m", "test: no paths, nothing staged"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()
    assert "--all-staged" in result.stderr


def test_safe_commit_allows_explicit_all_staged_opt_in(git_repo: Path):
    """Intentional whole-index commits require the explicit --all-staged flag."""
    (git_repo / "test.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "--all-staged", "-m", "test: explicit all staged"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: explicit all staged" in log.stdout


def _stage_file(git_repo: Path, name: str, content: str = "hello") -> None:
    (git_repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=git_repo, check=True, capture_output=True)


def test_safe_commit_refuses_exclusion_only_pathspec(git_repo: Path):
    """Exclusion-only args are not a positive selector — refuse whole-index commit."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [str(SAFE_COMMIT), ":(exclude)test.txt", "-m", "should refuse"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()


@pytest.mark.parametrize(
    "pathspec",
    [
        ":!test.txt",
        ":^test.txt",
        ":(exclude)test.txt",
        ":(icase,exclude)test.txt",
        '":!test.txt"',
        '"\\072!test.txt"',
    ],
)
def test_safe_commit_refuses_exclusion_pathspec_variants(git_repo: Path, pathspec: str):
    """All exclusion-only spellings must fail the whole-index guard."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [str(SAFE_COMMIT), pathspec, "-m", "should refuse"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_empty_pathspec_file(git_repo: Path):
    """--pathspec-from-file with no positive entries is not an explicit selector."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs.txt"
    psfile.write_text("")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            f"--pathspec-from-file={psfile}",
            "-m",
            "should refuse",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_exclusion_only_pathspec_file(git_repo: Path):
    """A pathspec file of only exclusions / blanks is not a positive selector."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs.txt"
    psfile.write_text("\n:(exclude)test.txt\n\n:!other.txt\n")
    result = subprocess.run(
        [str(SAFE_COMMIT), "--pathspec-from-file", str(psfile), "-m", "should refuse"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_dev_null_pathspec_file(git_repo: Path):
    """/dev/null is an empty pathspec source."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "--pathspec-from-file=/dev/null",
            "-m",
            "should refuse",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_allows_positive_pathspec_file(git_repo: Path):
    """A pathspec file naming a real file is a positive selector."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs.txt"
    psfile.write_text("test.txt\n")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            f"--pathspec-from-file={psfile}",
            "-m",
            "test: pathspec file",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: pathspec file" in log.stdout


def test_safe_commit_refuses_exclusion_only_stdin_pathspec(git_repo: Path):
    """Stdin pathspecs must be inspected, not treated as inherently positive."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [str(SAFE_COMMIT), "--pathspec-from-file=-", "-m", "should refuse"],
        cwd=git_repo,
        input=":(exclude)test.txt\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_exclusion_only_stdin_pathspec_space_form(
    git_repo: Path,
):
    """Space-separated --pathspec-from-file - must inspect stdin too."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [str(SAFE_COMMIT), "--pathspec-from-file", "-", "-m", "should refuse"],
        cwd=git_repo,
        input=":!test.txt\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_allows_positive_stdin_pathspec(git_repo: Path):
    """Stdin with a real file name is a positive selector after materializing."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "--pathspec-from-file=-",
            "-m",
            "test: stdin pathspec",
        ],
        cwd=git_repo,
        input="test.txt\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: stdin pathspec" in log.stdout


def test_safe_commit_refuses_nul_separated_exclusion_only_pathspec_file(
    git_repo: Path,
):
    """--pathspec-file-nul exclusion-only entries must not count as a selector."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs"
    psfile.write_bytes(b":!test.txt\0:!other.txt\0")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "--pathspec-file-nul",
            f"--pathspec-from-file={psfile}",
            "-m",
            "should refuse",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_nul_exclusions_when_nul_flag_follows_file(
    git_repo: Path,
):
    """--pathspec-file-nul must apply even when it appears after the file arg."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs"
    psfile.write_bytes(b":(exclude)test.txt\0")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            f"--pathspec-from-file={psfile}",
            "--pathspec-file-nul",
            "-m",
            "should refuse",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_allows_nul_separated_positive_pathspec_file(git_repo: Path):
    """NUL-separated file naming a real path is a positive selector."""
    _stage_file(git_repo, "test.txt")
    psfile = git_repo / "pathspecs"
    psfile.write_bytes(b"test.txt\0")
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "--pathspec-file-nul",
            f"--pathspec-from-file={psfile}",
            "-m",
            "test: nul pathspec",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: nul pathspec" in log.stdout


def test_safe_commit_valueless_gpg_sign_does_not_count_message_as_pathspec(
    git_repo: Path,
):
    """-S without a key must not swallow -m and treat the message as a pathspec."""
    _stage_file(git_repo, "test.txt")
    result = subprocess.run(
        [str(SAFE_COMMIT), "-S", "-m", "should refuse"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "implicit whole-index commit" in result.stderr.lower()


def test_safe_commit_refuses_dirty_worktree_without_no_verify(git_repo: Path):
    """Tracked dirty worktrees are blocked before prek can stash unrelated files."""
    (git_repo / "dirty.txt").write_text("tracked\n")
    subprocess.run(
        ["git", "add", "dirty.txt"], cwd=git_repo, check=True, capture_output=True
    )
    (git_repo / "dirty.txt").write_text("dirty\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    sha_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: dirty worktree blocked"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "dirty worktree" in result.stderr.lower()
    assert "--no-verify" in result.stderr

    sha_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha_before == sha_after


def test_safe_commit_allows_dirty_worktree_with_no_verify(git_repo: Path):
    """The explicit --no-verify escape hatch still works after manual checks."""
    (git_repo / "dirty.txt").write_text("dirty\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "commit.txt",
            "--no-verify",
            "-m",
            "test: dirty worktree allowed",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: dirty worktree allowed" in log.stdout


def test_safe_commit_stages_untracked_pathspecs_under_no_verify(git_repo: Path):
    """--no-verify with an untracked pathspec should auto-stage and commit.

    Regression for the case where `git-safe-commit foo.md --no-verify -m ...`
    on a fresh untracked file failed with "pathspec did not match any file(s)
    known to git" because the auto-stage step was gated on hooks-run mode.
    """
    fresh = git_repo / "fresh.txt"
    fresh.write_text("brand new\n")

    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            "fresh.txt",
            "--no-verify",
            "-m",
            "test: untracked under --no-verify",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test: untracked under --no-verify" in log.stdout

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_safe_commit_allows_untracked_worktree_by_default_with_auto_stage_hook(
    git_repo: Path,
):
    """Default mode should allow untracked files only for the known safe hook path."""
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (hooks_dir / "pre-commit").unlink(missing_ok=True)
    (hooks_dir / "pre-commit").symlink_to(PRE_COMMIT_HOOK)

    fake_bin = Path(tempfile.mkdtemp(prefix="fake-prek-"))
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "run" ]; then
                exit 0
            fi
            echo "unexpected args: $*" >&2
            exit 2
            """
        )
    )
    fake_prek.chmod(0o755)

    (git_repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    subprocess.run(
        ["git", "add", ".pre-commit-config.yaml"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "add config"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    (git_repo / "scratch.txt").write_text("scratch\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: untracked allowed"],
        cwd=git_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "untracked path(s); proceeding" in result.stderr


def test_safe_commit_default_mode_blocks_untracked_worktree_for_unknown_hook(
    git_repo: Path,
):
    """Default mode must stay strict for repo-specific hooks we cannot classify."""
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    pre_commit_hook = hooks_dir / "pre-commit"
    pre_commit_hook.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            exit 0
            """
        )
    )
    pre_commit_hook.chmod(0o755)

    (git_repo / "scratch.txt").write_text("scratch\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: untracked blocked"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "dirty worktree" in result.stderr.lower()
    assert "1 untracked path" in result.stderr


def test_safe_commit_off_mode_allows_untracked_worktree_with_auto_stage_hook(
    git_repo: Path,
):
    """Off mode should bypass both the outer and inner untracked-file guards."""
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (hooks_dir / "pre-commit").unlink(missing_ok=True)
    (hooks_dir / "pre-commit").symlink_to(PRE_COMMIT_HOOK)

    fake_bin = Path(tempfile.mkdtemp(prefix="fake-prek-"))
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "run" ]; then
                exit 0
            fi
            echo "unexpected args: $*" >&2
            exit 2
            """
        )
    )
    fake_prek.chmod(0o755)

    (git_repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    subprocess.run(
        ["git", "add", ".pre-commit-config.yaml"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "add config"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    (git_repo / "scratch.txt").write_text("scratch\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    env = os.environ.copy()
    env["GIT_SAFE_COMMIT_DIRTY_GUARD"] = "off"
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: off mode allows untracked"],
        cwd=git_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "detected new dirty paths" not in result.stderr


def test_safe_commit_strict_mode_reports_both_tracked_and_untracked_paths(
    git_repo: Path,
):
    """Strict mode should report both blocking categories when both are present."""
    (git_repo / "dirty.txt").write_text("dirty\n")
    subprocess.run(
        ["git", "add", "dirty.txt"], cwd=git_repo, check=True, capture_output=True
    )
    (git_repo / "dirty.txt").write_text("dirty again\n")
    (git_repo / "scratch.txt").write_text("scratch\n")
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    env = os.environ.copy()
    env["GIT_SAFE_COMMIT_DIRTY_GUARD"] = "strict"
    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: strict mode blocked"],
        cwd=git_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (
        "1 unstaged tracked modification and 1 untracked path (strict mode)"
        in result.stderr
    )
    assert "dirty.txt" in result.stderr
    assert "scratch.txt" in result.stderr


def test_safe_commit_dirty_worktree_message_keeps_truncation_notice(git_repo: Path):
    """Dirty-worktree diagnostics should keep the truncated-path count footer."""
    for i in range(11):
        file_path = git_repo / f"dirty-{i}.txt"
        file_path.write_text(f"dirty {i}\n")
        subprocess.run(
            ["git", "add", file_path.name],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        file_path.write_text(f"dirty again {i}\n")

    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: many dirty files blocked"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "... and 1 more" in result.stderr


def test_safe_commit_rechecks_dirty_worktree_after_waiting_for_lock(git_repo: Path):
    """The tracked-dirty check must happen after lock acquisition, not before."""
    (git_repo / "commit.txt").write_text("commit me\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=git_repo, check=True, capture_output=True
    )
    (git_repo / "dirty.txt").write_text("tracked\n")
    subprocess.run(
        ["git", "add", "dirty.txt"], cwd=git_repo, check=True, capture_output=True
    )

    sha_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Hold the lock in-process via fcntl.flock so there is no subprocess-startup
    # race (bash -lc login shells can take >200ms on CI to reach the flock call).
    lockfile_path = git_repo / ".git" / "commit.lock"
    lock_fd = open(lockfile_path, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        commit_proc = subprocess.Popen(
            [str(SAFE_COMMIT), "commit.txt", "-m", "test: recheck after lock"],
            cwd=git_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Give git-safe-commit time to start and block on flock.
        time.sleep(0.3)
        (git_repo / "dirty.txt").write_text("dirty again\n")
        # Release the lock so git-safe-commit can proceed and recheck.
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()

    stdout, stderr = commit_proc.communicate(timeout=15)
    assert commit_proc.returncode == 1, stdout + stderr
    assert "dirty worktree" in stderr.lower()

    sha_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha_before == sha_after


@pytest.mark.parametrize("timeout_value", ["abc", "-1", "0", "00"])
def test_safe_commit_rejects_invalid_lock_timeout(git_repo: Path, timeout_value: str):
    """Bad lock timeout env vars should fail with a configuration error."""
    (git_repo / "commit.txt").write_text("commit me\n")

    env = os.environ.copy()
    env["GIT_SAFE_COMMIT_LOCK_TIMEOUT"] = timeout_value
    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: invalid lock timeout"],
        cwd=git_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "GIT_SAFE_COMMIT_LOCK_TIMEOUT must be a positive integer" in result.stderr
    assert "timed out waiting for commit lock" not in result.stderr


def test_safe_commit_serialization(git_repo: Path):
    """Two concurrent safe-commits don't interfere with each other."""
    # Create and stage two files
    (git_repo / "a.txt").write_text("file a")
    (git_repo / "b.txt").write_text("file b")
    subprocess.run(
        ["git", "add", "a.txt", "b.txt"], cwd=git_repo, check=True, capture_output=True
    )

    # Start both commits concurrently
    proc_a = subprocess.Popen(
        [str(SAFE_COMMIT), "a.txt", "-m", "test: commit a"],
        cwd=git_repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc_b = subprocess.Popen(
        [str(SAFE_COMMIT), "b.txt", "-m", "test: commit b"],
        cwd=git_repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Both should succeed (one waits for the other)
    out_a, err_a = proc_a.communicate(timeout=30)
    out_b, err_b = proc_b.communicate(timeout=30)

    # At least one should succeed; the other might succeed or fail
    # depending on timing, but neither should produce a corrupted commit
    assert proc_a.returncode == 0, f"Session A failed: {err_a.decode()}"
    assert proc_b.returncode == 0, f"Session B failed: {err_b.decode()}"

    # Verify we have the right number of commits (init + 1 or 2)
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    commits = log.stdout.strip().split("\n")
    # Should have at least 2 commits (init + at least one successful)
    assert len(commits) >= 2


def test_safe_commit_works_with_serialized_pre_commit_hook(tmp_path: Path):
    """safe-commit should not deadlock when the hook also uses commit.lock."""
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (hooks_dir / "pre-commit").unlink(missing_ok=True)
    (hooks_dir / "pre-commit").symlink_to(PRE_COMMIT_HOOK)

    fake_bin = Path(tempfile.mkdtemp(prefix="fake-prek-"))
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "run" ]; then
                exit 0
            fi
            echo "unexpected args: $*" >&2
            exit 2
            """
        )
    )
    fake_prek.chmod(0o755)

    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md", ".pre-commit-config.yaml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    init_commit = subprocess.run(
        [str(SAFE_COMMIT), "README.md", ".pre-commit-config.yaml", "-m", "init"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert init_commit.returncode == 0, init_commit.stderr

    (tmp_path / "test.txt").write_text("hello\n")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=tmp_path, check=True, capture_output=True
    )
    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: hook-safe commit"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


def test_safe_commit_hook_blocks_dirty_worktree_created_after_precheck(tmp_path: Path):
    """The hook should abort if the worktree gets dirtied after wrapper pre-check."""
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    pre_commit_hook = hooks_dir / "pre-commit"
    pre_commit_hook.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            printf 'dirty\\n' > dirty.txt
            exec "{PRE_COMMIT_HOOK}" "$@"
            """
        )
    )
    pre_commit_hook.chmod(0o755)

    fake_bin = Path(tempfile.mkdtemp(prefix="fake-prek-"))
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "run" ]; then
                exit 0
            fi
            echo "unexpected args: $*" >&2
            exit 2
            """
        )
    )
    fake_prek.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    subprocess.run(
        ["git", "add", ".pre-commit-config.yaml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "add config"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    (tmp_path / "commit.txt").write_text("hello\n")
    subprocess.run(
        ["git", "add", "commit.txt"], cwd=tmp_path, check=True, capture_output=True
    )

    sha_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(SAFE_COMMIT), "commit.txt", "-m", "test: hook catches late dirty state"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "dirty" in result.stderr.lower()
    assert "issue #642" in result.stderr.lower()

    sha_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha_before == sha_after


def _init_large_repo(tmp_path: Path, file_count: int) -> Path:
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    for i in range(file_count):
        (files_dir / f"file_{i:04d}.txt").write_text(f"content {i}\n")
    subprocess.run(
        ["git", "add", "files/"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", f"init: {file_count} files"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def large_git_repo(tmp_path: Path) -> Path:
    """Git repo with 1100 tracked files — triggers #642 pre-check threshold."""
    return _init_large_repo(tmp_path, 1100)


@pytest.fixture
def huge_git_repo(tmp_path: Path) -> Path:
    """Git repo with 1300 tracked files — allows deletion of 200 files while
    keeping TRACKED_COUNT above the pre-check threshold, so Layer 2 fires."""
    return _init_large_repo(tmp_path, 1300)


def test_safe_commit_detects_index_corruption(large_git_repo: Path):
    """Pre-commit check aborts when index is corrupted (issue #642 Layer 1).

    Simulates the #642 scenario where prek's stash/restore wiped out tracked
    files from the index. git-safe-commit should detect TRACKED << HEAD and
    refuse to commit (auto-rebuild + request retry).
    """
    # Simulate index corruption: remove all tracked files from the index
    # except one file that user is trying to commit.
    subprocess.run(
        ["git", "rm", "--cached", "-r", "files/"],
        cwd=large_git_repo,
        check=True,
        capture_output=True,
    )
    # Stage a single "new" file the user wanted to commit
    (large_git_repo / "test.txt").write_text("hello\n")
    subprocess.run(
        ["git", "add", "test.txt"],
        cwd=large_git_repo,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: should be blocked"],
        cwd=large_git_repo,
        capture_output=True,
        text=True,
    )

    # Should abort (exit 1) with corruption warning
    assert (
        result.returncode == 1
    ), f"Expected corruption detection, got:\n{result.stdout}\n{result.stderr}"
    assert "corruption" in result.stderr.lower()
    # Index should be auto-rebuilt
    tracked_after = subprocess.run(
        ["git", "ls-files"],
        cwd=large_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_count = len(tracked_after.stdout.strip().splitlines())
    assert (
        tracked_count >= 1100
    ), f"Expected index rebuild to restore ~1100 files, got {tracked_count}"


def test_safe_commit_detects_partial_index_corruption_above_old_threshold(
    huge_git_repo: Path,
):
    """Pre-check catches partial index loss even when >1000 files remain tracked.

    This is the blind spot the old TRACKED_COUNT < 1000 heuristic missed:
    a large repo can lose hundreds of index entries, still track >1000 files,
    and be catastrophically wrong.
    """
    # Remove 200 tracked files from the index, but leave them on disk.
    # TRACKED_COUNT remains 1100, so the old heuristic would not fire.
    files_to_drop = [f"files/file_{i:04d}.txt" for i in range(200)]
    subprocess.run(
        ["git", "rm", "--cached", *files_to_drop],
        cwd=huge_git_repo,
        check=True,
        capture_output=True,
    )

    # Stage a legitimate file the user was trying to commit.
    (huge_git_repo / "test.txt").write_text("hello\n")
    subprocess.run(
        ["git", "add", "test.txt"],
        cwd=huge_git_repo,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: partial corruption blocked"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
    )

    assert (
        result.returncode == 1
    ), f"Expected partial corruption detection, got:\n{result.stdout}\n{result.stderr}"
    assert "corruption" in result.stderr.lower()
    assert "missing from index but still on disk" in result.stderr

    tracked_after = subprocess.run(
        ["git", "ls-files"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_count = len(tracked_after.stdout.strip().splitlines())
    assert (
        tracked_count >= 1300
    ), f"Expected index rebuild to restore ~1300 files, got {tracked_count}"


def test_safe_commit_reverts_catastrophic_deletion(huge_git_repo: Path):
    """Post-commit check auto-reverts if the commit deleted >100 files (issue #642 Layer 2).

    Uses a 1300-file repo so that deleting 200 leaves TRACKED=1100 (above
    pre-check threshold) — ensures Layer 1 doesn't fire first and we actually
    exercise the post-commit safety net.
    """
    # Stage deletion of 200 files (exceeds 100-file threshold, TRACKED stays > 1000)
    for i in range(200):
        (huge_git_repo / "files" / f"file_{i:04d}.txt").unlink()
    subprocess.run(
        ["git", "add", "-A"],
        cwd=huge_git_repo,
        check=True,
        capture_output=True,
    )

    sha_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(SAFE_COMMIT), "--all-staged", "-m", "test: mass deletion"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
    )

    # Should exit non-zero after auto-revert
    assert (
        result.returncode != 0
    ), f"Expected auto-revert, got:\n{result.stdout}\n{result.stderr}"
    assert (
        "CATASTROPHIC" in result.stdout or "reverted" in result.stdout.lower()
    ), f"Expected post-commit revert message, got stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # HEAD should be unchanged (soft revert)
    sha_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert (
        sha_before == sha_after
    ), f"Expected HEAD unchanged after revert, was {sha_before[:8]}, now {sha_after[:8]}"

    # Changes should remain staged (soft reset)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=huge_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    staged_count = len(staged.stdout.strip().splitlines())
    assert (
        staged_count >= 200
    ), f"Expected ~200 staged deletions after soft reset, got {staged_count}"


def test_safe_commit_allows_normal_small_repo_commit(git_repo: Path):
    """Safeguards do not trigger on small repos (HEAD_COUNT < 1000 threshold)."""
    # git_repo has only 1 file (README.md); TRACKED=1, HEAD=1 — below threshold
    (git_repo / "test.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: small repo commit"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_safe_commit_runs_pre_commit_manually_before_no_verify_commit(git_repo: Path):
    """Wrapper should run pre-commit itself, then commit without redispatching it."""
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    pre_commit_count = git_repo / ".git" / "manual-pre-commit.count"
    pre_commit_hook = hooks_dir / "pre-commit"
    pre_commit_hook.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            count_file="{pre_commit_count}"
            count=0
            if [ -f "$count_file" ]; then
                count="$(cat "$count_file")"
            fi
            count=$((count + 1))
            printf '%s' "$count" > "$count_file"
            if [ "${{GIT_SAFE_COMMIT_MANUAL_PRECOMMIT:-0}}" != "1" ]; then
                echo "expected manual pre-commit marker" >&2
                exit 91
            fi
            exit 0
            """
        )
    )
    pre_commit_hook.chmod(0o755)

    prepare_commit_msg_hook = hooks_dir / "prepare-commit-msg"
    prepare_commit_msg_hook.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '\nManual-Hook: yes\n' >> "$1"
            """
        )
    )
    prepare_commit_msg_hook.chmod(0o755)

    test_file = git_repo / "test.txt"
    test_file.write_text("hello\n")
    subprocess.run(
        ["git", "add", "test.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "test.txt", "-m", "test: manual pre-commit bridge"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    # pre-commit ran exactly once (manual run; --no-verify skipped git's redispatch)
    assert pre_commit_count.read_text() == "1"

    commit_message = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "test: manual pre-commit bridge" in commit_message.stdout
    # prepare-commit-msg still runs under --no-verify; commit-msg is intentionally skipped
    assert "Manual-Hook: yes" in commit_message.stdout


def test_safe_commit_real_hook_failure_message_is_actionable(tmp_path: Path):
    """When prek fails without modifying files, surface the hook output as the
    primary signal — don't bury it under a misleading 'checking for auto-staging'
    header that suggests fixes are happening."""
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_dir)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (hooks_dir / "pre-commit").unlink(missing_ok=True)
    (hooks_dir / "pre-commit").symlink_to(PRE_COMMIT_HOOK)

    # Fake prek that fails on `run` without modifying any files (simulates a
    # validator hook like validate-blog-urls catching a real issue).
    fake_bin = Path(tempfile.mkdtemp(prefix="fake-prek-fail-"))
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "run" ]; then
                echo "validate-blog-urls.................Failed"
                echo "- hook id: validate-blog-urls"
                echo "- exit code: 1"
                echo ""
                echo "Private repo link detected: https://github.com/ErikBjare/bob/..."
                exit 1
            fi
            exit 2
            """
        )
    )
    fake_prek.chmod(0o755)

    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md", ".pre-commit-config.yaml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    init_result = subprocess.run(
        [str(SAFE_COMMIT), "README.md", ".pre-commit-config.yaml", "-m", "init"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert init_result.returncode != 0, "Expected commit to fail when hook rejects"
    combined = init_result.stdout + init_result.stderr

    # Real hook output must be visible
    assert "validate-blog-urls" in combined
    assert "Private repo link detected" in combined

    # New, accurate failure message must appear
    assert "no auto-fixable changes" in combined
    assert "prek run --files" in combined

    # Old misleading header must NOT appear when no files were modified
    assert "checking for auto-staging" not in combined


def _staged_paths(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in out.stdout.splitlines() if line}


def test_dirty_guard_abort_unstages_newly_staged_file(git_repo: Path):
    """Unstage-on-abort: when the dirty-worktree guard refuses after
    stage_explicit_pathspecs ran, the paths THIS run staged must not stay
    staged (a staged-but-uncommitted file in a shared worktree can be swept by
    a sibling's branch switch/reset — 2026-09-01 incident). The file itself
    must remain on disk, back to untracked."""
    # Unstaged tracked modification outside the pathspec → guard blocks
    (git_repo / "README.md").write_text("modified outside scope")
    new_file = git_repo / "new-journal.md"
    new_file.write_text("precious content")

    result = subprocess.run(
        [str(SAFE_COMMIT), "new-journal.md", "-m", "test: should abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Refusing to run pre-commit in a dirty worktree" in result.stderr
    assert "unstaged 1 path(s) staged by this aborted run" in result.stderr
    assert "new-journal.md" not in _staged_paths(
        git_repo
    ), "aborted run left its file staged — sweep-class ammunition"
    assert new_file.exists() and new_file.read_text() == "precious content"


def test_failing_hook_abort_unstages_newly_staged_file(git_repo: Path):
    """Same guarantee when the pre-commit hook itself fails after staging."""
    hooks = git_repo / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    new_file = git_repo / "new-file.md"
    new_file.write_text("content")

    result = subprocess.run(
        [str(SAFE_COMMIT), "new-file.md", "-m", "test: hook fails"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "new-file.md" not in _staged_paths(git_repo)
    assert new_file.exists()


def test_abort_preserves_preexisting_staged_content(git_repo: Path):
    """Only paths newly staged by the aborted run are unstaged — content the
    caller (or a sibling) staged beforehand is left untouched."""
    pre_staged = git_repo / "already-staged.md"
    pre_staged.write_text("caller staged this")
    subprocess.run(
        ["git", "add", "already-staged.md"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    # Trigger the dirty guard via an unstaged tracked modification
    (git_repo / "README.md").write_text("dirty")
    new_file = git_repo / "fresh.md"
    new_file.write_text("fresh")

    result = subprocess.run(
        [str(SAFE_COMMIT), "already-staged.md", "fresh.md", "-m", "test: abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    staged = _staged_paths(git_repo)
    assert "already-staged.md" in staged, "pre-existing staged content was dropped"
    assert "fresh.md" not in staged
    assert new_file.exists()


def test_abort_restores_replaced_staged_blob(git_repo: Path):
    """When the explicit path already had staged content and the worktree held
    different content, our `git add` replaces the staged blob. An abort must
    put the caller's prior staged blob back, not leave this run's content
    staged (Greptile finding on #1579)."""
    doc = git_repo / "doc.md"
    doc.write_text("version A")
    subprocess.run(
        ["git", "add", "doc.md"], cwd=git_repo, check=True, capture_output=True
    )
    doc.write_text("version B")
    # Trigger the dirty guard via an unstaged tracked modification
    (git_repo / "README.md").write_text("dirty")

    result = subprocess.run(
        [str(SAFE_COMMIT), "doc.md", "-m", "test: abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    staged_blob = subprocess.run(
        ["git", "show", ":doc.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged_blob == "version A", "caller's prior staged blob was lost"
    assert doc.read_text() == "version B", "worktree content must be untouched"


def test_abort_restores_staged_deletion(git_repo: Path):
    """If our add staged a deletion, abort must put the prior index entry
    back (path absent from the index needs `update-index --add --cacheinfo`)."""
    doomed = git_repo / "doomed.md"
    doomed.write_text("keep me in index")
    subprocess.run(
        ["git", "add", "doomed.md"], cwd=git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "add doomed"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    doomed.unlink()
    (git_repo / "README.md").write_text("dirty")

    result = subprocess.run(
        [str(SAFE_COMMIT), "doomed.md", "-m", "test: abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "doomed.md" not in _staged_paths(git_repo)
    ls = subprocess.run(
        ["git", "ls-files", "--stage", "--", "doomed.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ls.stdout.strip(), "pre-run index entry was not restored"
    assert not doomed.exists(), "worktree deletion must be left alone"


def test_abort_skips_path_restaged_by_sibling(git_repo: Path):
    """If another process re-stages one of our paths after our `git add` but
    before the abort, the trap must leave that newer index entry alone
    (Greptile finding on #1579). Simulated via a failing pre-commit hook that
    plays the sibling."""
    # Hooks dir lives OUTSIDE the repo so the dirty guard (untracked-path
    # check) does not abort before the hook gets a chance to run.
    hooks = git_repo.parent / "sibling-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"cd '{git_repo}'\n"
        'printf "sibling content" > raced.md\n'
        "git add raced.md\n"
        "exit 1\n"
    )
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (git_repo / "raced.md").write_text("our content")

    result = subprocess.run(
        [str(SAFE_COMMIT), "raced.md", "-m", "test: hook fails"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "left 1 path(s) alone" in result.stderr
    staged_blob = subprocess.run(
        ["git", "show", ":raced.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged_blob == "sibling content", "sibling's newer staging was clobbered"


def test_partial_git_add_failure_unstages_succeeded_paths(git_repo: Path):
    """A mixed pathspec list stages the valid paths then fails. The abort
    trap must still unstage whatever DID get added, or those files remain
    sweep-class ammunition (in-band review P1 on #1579)."""
    good = git_repo / "good.md"
    good.write_text("keep me on disk")

    result = subprocess.run(
        [str(SAFE_COMMIT), "good.md", "missing.md", "-m", "test: partial add"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "good.md" not in _staged_paths(git_repo)
    assert good.exists() and good.read_text() == "keep me on disk"


def test_abort_unstages_identical_noop_sibling_add(git_repo: Path):
    """A sibling `git add` of the same already-staged bytes is a no-op
    (git does not rewrite the index entry). That is still this run's staging
    and must be restored on abort — there is no independent sibling entry to
    preserve. Worktree content is untouched so the sibling can re-add
    (Greptile identical-restage finding on #1579)."""
    hooks = git_repo.parent / "noop-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\n" f"cd '{git_repo}'\n" "git add raced.md\n" "exit 1\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    raced = git_repo / "raced.md"
    raced.write_text("our content")

    result = subprocess.run(
        [str(SAFE_COMMIT), "raced.md", "-m", "test: hook fails"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "raced.md" not in _staged_paths(git_repo)
    assert raced.exists() and raced.read_text() == "our content"


def test_abort_restores_unmerged_conflict_stages(git_repo: Path):
    """When an explicit path has unresolved stage-1/2/3 entries and this run
    `git add`s a worktree resolution, abort must restore the original unmerged
    dump instead of leaving a fabricated stage-0 entry (Greptile finding on
    #1579). The worktree resolution stays on disk."""
    conflict = git_repo / "conflict.md"
    conflict.write_text("base")
    subprocess.run(
        ["git", "add", "conflict.md"], cwd=git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "-b", "side"], cwd=git_repo, check=True, capture_output=True
    )
    conflict.write_text("side version")
    subprocess.run(
        ["git", "commit", "-am", "side"], cwd=git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "test-branch"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    conflict.write_text("main version")
    subprocess.run(
        ["git", "commit", "-am", "main"], cwd=git_repo, check=True, capture_output=True
    )
    merge = subprocess.run(
        ["git", "merge", "side"], cwd=git_repo, capture_output=True, text=True
    )
    assert merge.returncode != 0, "expected a merge conflict"
    before = subprocess.run(
        ["git", "ls-files", "--stage", "--", "conflict.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert before.count("\t") >= 2, "expected unmerged stages, got:\n" + before

    conflict.write_text("resolved by caller")
    # Trigger the dirty guard via an unstaged tracked modification
    (git_repo / "README.md").write_text("dirty")

    result = subprocess.run(
        [str(SAFE_COMMIT), "conflict.md", "-m", "test: abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    after = subprocess.run(
        ["git", "ls-files", "--stage", "--", "conflict.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before, (
        "abort collapsed or mutated conflict stages:\n"
        f"before:\n{before}after:\n{after}\nstderr:\n{result.stderr}"
    )
    assert conflict.read_text() == "resolved by caller"


def test_abort_skips_newline_pathname(git_repo: Path):
    """A pathname containing a newline cannot ride the newline-delimited
    dump/restore feed (`update-index --index-info`), so such paths are
    excluded from abort tracking (Greptile finding on #1579): they keep
    whatever state exists at abort instead of being restored wrongly."""
    evil = git_repo / "evil\nname.md"
    evil.write_text("version A")
    subprocess.run(
        ["git", "add", "--", "evil\nname.md"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    evil.write_text("version B")
    # Trigger the dirty guard via an unstaged tracked modification
    (git_repo / "README.md").write_text("dirty")

    result = subprocess.run(
        [str(SAFE_COMMIT), "evil\nname.md", "-m", "test: abort"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "could not restore" not in result.stderr
    ls = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", "evil\nname.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [e for e in ls.split("\0") if e]
    assert len(entries) == 1, f"index corrupted for newline path: {entries!r}"
    # Out-of-scope path keeps this run's staging (documented pre-trap
    # behavior) rather than getting a corrupt partial restore.
    staged_blob = subprocess.run(
        ["git", "show", ":evil\nname.md"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged_blob == "version B"


def test_successful_commit_keeps_trap_inert(git_repo: Path):
    """The success path must not unstage anything or emit the abort note."""
    f = git_repo / "ok.md"
    f.write_text("fine")

    result = subprocess.run(
        [str(SAFE_COMMIT), "ok.md", "-m", "test: success"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "unstaged" not in result.stderr
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "test: success" in log.stdout


def _fresh_repo(tmp_path: Path) -> Path:
    """`git init` with identity, no commits, no .git/index."""
    subprocess.run(
        ["git", "init", "-b", "test-branch", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_safe_commit_initial_commit_without_index(tmp_path: Path):
    """Fresh `git init` has no .git/index. Snapshotting via cp -p used to
    fail with 'could not snapshot the index' and block the first commit
    (AI-review P1 on #1579). git add used to create the index lazily."""
    repo = _fresh_repo(tmp_path)
    assert not (repo / ".git" / "index").exists()
    (repo / "README.md").write_text("first\n")

    result = subprocess.run(
        [str(SAFE_COMMIT), "README.md", "-m", "feat: first commit"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "could not snapshot" not in result.stderr
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "feat: first commit" in log.stdout
    assert (repo / ".git" / "index").exists()


def test_abort_unstages_newly_staged_file_on_fresh_repo(tmp_path: Path):
    """First-commit abort still unstages: the file returns to untracked
    instead of remaining staged-but-uncommitted (or failing the snapshot)."""
    repo = _fresh_repo(tmp_path)
    hooks = repo / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    new_file = repo / "new-file.md"
    new_file.write_text("content")

    result = subprocess.run(
        [str(SAFE_COMMIT), "new-file.md", "-m", "test: hook fails"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "could not snapshot" not in result.stderr
    assert "new-file.md" not in _staged_paths(repo)
    assert new_file.exists() and new_file.read_text() == "content"


@pytest.fixture
def autoformat_env(git_repo: Path) -> dict[str, str]:
    """A pinned hook double that rewrites once, then passes on retry."""
    fake_bin = git_repo / ".git" / "fake-bin"
    fake_bin.mkdir()
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            [ "$1 $2 $3" = "run ruff-format --files" ] || exit 90
            shift 3
            changed=0
            for f in "$@"; do
                if ! grep -q '# formatted' "$f"; then
                    printf '# formatted\\n' >> "$f"
                    changed=1
                fi
            done
            exit "$changed"
            """
        )
    )
    fake_prek.chmod(0o755)
    env = os.environ.copy()
    env.pop("GIT_SAFE_COMMIT_AUTO_FORMAT", None)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return env


@pytest.mark.parametrize(
    "opt_in,no_verify", [(False, False), (True, False), (True, True)]
)
def test_autoformat_requires_opt_in_and_hooks_enabled(
    git_repo: Path, autoformat_env: dict[str, str], opt_in: bool, no_verify: bool
):
    foo = git_repo / "foo.py"
    foo.write_text("x = 1\n")
    (git_repo / "unrelated.py").write_text("leave = 1\n")
    if opt_in:
        autoformat_env["GIT_SAFE_COMMIT_AUTO_FORMAT"] = "1"
    args = [str(SAFE_COMMIT), "foo.py", "-m", "test: opt-in formatter"]
    if no_verify:
        args.append("--no-verify")
    result = subprocess.run(
        args, cwd=git_repo, env=autoformat_env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    committed = subprocess.run(
        ["git", "show", "HEAD:foo.py"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected = "x = 1\n# formatted\n" if opt_in and not no_verify else "x = 1\n"
    assert committed == foo.read_text() == expected
    assert (git_repo / "unrelated.py").read_text() == "leave = 1\n"
    assert (
        subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


@pytest.mark.parametrize("pre_staged", [False, True])
def test_autoformat_abort_restores_original_index(
    git_repo: Path, autoformat_env: dict[str, str], pre_staged: bool
):
    foo = git_repo / "foo.py"
    if pre_staged:
        foo.write_text("staged = 1\n")
        subprocess.run(["git", "add", "foo.py"], cwd=git_repo, check=True)
    original = subprocess.run(
        ["git", "ls-files", "--stage"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    foo.write_text("worktree = 2\n")
    hooks = git_repo / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho abort-after-formatting >&2\nexit 1\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)], cwd=git_repo, check=True
    )
    autoformat_env["GIT_SAFE_COMMIT_AUTO_FORMAT"] = "1"
    result = subprocess.run(
        [str(SAFE_COMMIT), "foo.py", "-m", "test: abort after formatting"],
        cwd=git_repo,
        env=autoformat_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "abort-after-formatting" in result.stderr
    assert foo.read_text() == "worktree = 2\n# formatted\n"
    assert (
        subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == original
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == head_before
    )


@pytest.mark.parametrize("absolute_path", [False, True])
def test_autoformat_runs_from_repository_root(
    git_repo: Path, autoformat_env: dict[str, str], absolute_path: bool
):
    nested = git_repo / "src"
    nested.mkdir()
    foo = nested / "file with spaces.py"
    foo.write_text("x = 1\n")
    autoformat_env["GIT_SAFE_COMMIT_AUTO_FORMAT"] = "1"
    result = subprocess.run(
        [
            str(SAFE_COMMIT),
            str(foo) if absolute_path else foo.name,
            "-m",
            "test: subdirectory formatting",
        ],
        cwd=nested,
        env=autoformat_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (
        subprocess.run(
            ["git", "show", "HEAD:src/file with spaces.py"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "x = 1\n# formatted\n"
    )


def test_autoformat_failure_aborts_before_staging(
    git_repo: Path, autoformat_env: dict[str, str]
):
    fake_prek = git_repo / ".git" / "fake-bin" / "prek"
    fake_prek.write_text("#!/bin/sh\nexit 2\n")
    foo = git_repo / "foo.py"
    foo.write_text("x = 1\n")
    autoformat_env["GIT_SAFE_COMMIT_AUTO_FORMAT"] = "1"
    result = subprocess.run(
        [str(SAFE_COMMIT), "foo.py", "-m", "test: formatter failure"],
        cwd=git_repo,
        env=autoformat_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        subprocess.run(
            ["git", "ls-files", "--", "foo.py"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    assert foo.read_text() == "x = 1\n"


def test_autoformat_holds_index_lock_during_hook(
    git_repo: Path, autoformat_env: dict[str, str]
):
    fake_prek = git_repo / ".git" / "fake-bin" / "prek"
    fake_prek.write_text(
        "#!/bin/sh\n"
        "env -u GIT_INDEX_FILE git add -- sibling.txt 2> .git/lock-error && exit 90\n"
        "grep -q index.lock .git/lock-error || exit 91\n"
    )
    (git_repo / "foo.py").write_text("x = 1\n")
    (git_repo / "sibling.txt").write_text("sibling\n")
    autoformat_env["GIT_SAFE_COMMIT_AUTO_FORMAT"] = "1"
    result = subprocess.run(
        [str(SAFE_COMMIT), "foo.py", "-m", "test: formatter holds lock"],
        cwd=git_repo,
        env=autoformat_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (
        subprocess.run(
            ["git", "ls-files", "--", "sibling.txt"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


# ---------------------------------------------------------------------------
# Submodule / gitlink regression tests  (issue #1776 + #1772)
# ---------------------------------------------------------------------------


def _make_sub_repo(path: Path, commit_msg: str = "init sub") -> str:
    """Create a bare-minimum git repo at *path* and return its HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
        # Allow pushes to the checked-out branch (this repo acts as a fake remote)
        ["git", "config", "receive.denyCurrentBranch", "ignore"],
    ):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)
    (path / "file.txt").write_text("sub content")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _superproject_with_submodule(tmp_path: Path) -> tuple[Path, Path, str]:
    """
    Return (superproject, submodule_path, sub_head_sha).

    Layout:
        tmp_path/super/          — superproject
        tmp_path/super/vendor/sub — submodule tracked in superproject's HEAD
    The submodule is registered at its initial HEAD SHA.
    """
    sub_dir = tmp_path / "sub_origin"
    sub_sha = _make_sub_repo(sub_dir)

    super_dir = tmp_path / "super"
    super_dir.mkdir()
    for cmd in (
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(cmd, cwd=super_dir, check=True, capture_output=True)

    (super_dir / "README.md").write_text("root")
    subprocess.run(
        ["git", "add", "README.md"], cwd=super_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=super_dir, check=True, capture_output=True
    )

    # Add vendor/sub as a gitlink via update-index (avoids .gitmodules overhead)
    vendor_dir = super_dir / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "readme.txt").write_text("vendor readme")
    subprocess.run(
        ["git", "add", "vendor/readme.txt"],
        cwd=super_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_sha},vendor/sub"],
        cwd=super_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add vendor/sub gitlink"],
        cwd=super_dir,
        check=True,
        capture_output=True,
    )

    # Clone the sub_origin into vendor/sub so submodule queries work
    sub_path = vendor_dir / "sub"
    subprocess.run(
        ["git", "clone", str(sub_dir), str(sub_path)],
        check=True,
        capture_output=True,
    )
    # Disable global hooks in the cloned sub so test pushes aren't blocked
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )
    # Ensure the submodule worktree HEAD matches the committed SHA
    subprocess.run(
        ["git", "checkout", sub_sha], cwd=sub_path, check=True, capture_output=True
    )

    return super_dir, sub_path, sub_sha


def test_direct_gitlink_refuses_unreachable_worktree_head(tmp_path: Path):
    """Direct gitlink pathspec vendor/sub is refused when worktree HEAD is
    not reachable from origin/master (regression for #1772)."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Advance submodule worktree to a new commit that origin doesn't have
    (sub_path / "new.txt").write_text("feature work")
    subprocess.run(
        ["git", "add", "new.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "feature"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor/sub", "-m", "bump sub", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "should refuse staging unreachable submodule SHA"
    assert "refusing" in result.stderr.lower() or "error" in result.stderr.lower()


def test_dir_pathspec_refuses_unreachable_submodule(tmp_path: Path):
    """Directory pathspec `vendor` containing a gitlink is refused when the
    submodule worktree HEAD is not reachable from origin/master (gap in #1772)."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Add a normal file so vendor/ has something to commit besides the gitlink
    (super_dir / "vendor" / "new.txt").write_text("regular file")

    # Advance submodule worktree to a new commit not on origin
    (sub_path / "new.txt").write_text("feature work")
    subprocess.run(
        ["git", "add", "new.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "feature"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit vendor should refuse when vendor/sub worktree is on "
        "an unreachable feature branch (issue #1776)"
    )
    assert "refusing" in result.stderr.lower() or "error" in result.stderr.lower()


def test_dot_pathspec_refuses_unreachable_submodule(tmp_path: Path):
    """`.` pathspec refuses when any submodule worktree HEAD is unreachable
    from origin/master (dot-pathspec variant of #1776)."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    (super_dir / "top.txt").write_text("top-level change")

    # Advance submodule worktree
    (sub_path / "new.txt").write_text("feature work")
    subprocess.run(
        ["git", "add", "new.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "feature"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), ".", "-m", "all changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit . should refuse when a nested submodule worktree is "
        "on an unreachable feature branch"
    )
    assert "refusing" in result.stderr.lower() or "error" in result.stderr.lower()


def test_dir_pathspec_allows_submodule_at_head_sha(tmp_path: Path):
    """Directory pathspec is allowed when submodule worktree HEAD matches
    the committed SHA (no-op gitlink case — should commit the other files)."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Add a regular file to commit alongside the (unchanged) gitlink
    new_file = super_dir / "vendor" / "new.txt"
    new_file.write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "add vendor/new.txt", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"should allow dir pathspec when submodule is at HEAD SHA\n"
        f"stderr: {result.stderr}"
    )
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "add vendor/new.txt" in log.stdout


def test_dir_pathspec_stages_submodule_when_reachable(tmp_path: Path):
    """Directory pathspec advances the gitlink when the worktree HEAD is
    reachable from origin/master (legitimate submodule bump via directory)."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Add a commit to the submodule and push it so it's on origin/master
    (sub_path / "v2.txt").write_text("v2")
    subprocess.run(
        ["git", "add", "v2.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "v2"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:master"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )

    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sub_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "bump vendor/sub to v2", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"should allow dir pathspec when submodule worktree is on origin/master\n"
        f"stderr: {result.stderr}"
    )

    # Verify the committed gitlink SHA is the new one
    committed_sha = subprocess.run(
        ["git", "ls-tree", "HEAD", "vendor/sub"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[2]
    assert (
        committed_sha == new_sha
    ), f"gitlink should be bumped to {new_sha[:12]}, got {committed_sha[:12]}"


def test_dir_pathspec_refuses_unreachable_gitlink_removed_from_index(tmp_path: Path):
    """A gitlink still in HEAD but dropped from the index (`git rm --cached`)
    is still detected by the directory scan and refused when the worktree HEAD
    is unreachable.

    The tracked scan in _gsc_find_gitlinks_under is index-only, but removing a
    path from the index makes `git ls-files --others` treat it as untracked, so
    the nested-repo scan still catches it. Without this the plain `git add`
    would re-add it at the (possibly unpublished) worktree HEAD, bypassing the
    reachability guard.
    """
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    (super_dir / "vendor" / "new.txt").write_text("regular file")

    # Advance submodule worktree to a commit origin does not have
    (sub_path / "new.txt").write_text("feature work")
    subprocess.run(
        ["git", "add", "new.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "feature"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )
    wt_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sub_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Drop the gitlink from the index while it remains in HEAD
    subprocess.run(
        ["git", "rm", "--cached", "vendor/sub"],
        cwd=super_dir,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit vendor must refuse an unreachable gitlink even when "
        "it was removed from the index but is still in HEAD"
    )
    # The unpublished worktree HEAD must not have been recorded
    staged = subprocess.run(
        ["git", "ls-files", "-s", "--", "vendor/sub"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert wt_head not in staged, "unreachable SHA leaked into the index"
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "vendor changes" not in log, "no commit should have been created"


def test_dir_pathspec_stages_reachable_gitlink_removed_from_index(tmp_path: Path):
    """A gitlink dropped from the index (`git rm --cached`) whose worktree HEAD
    is reachable is re-staged at that SHA — the path-missing-from-index branch
    of _gsc_stage_gitlink_path."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Advance the submodule and push so its HEAD is on origin/master
    (sub_path / "v2.txt").write_text("v2")
    subprocess.run(
        ["git", "add", "v2.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "v2"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:master"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sub_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    subprocess.run(
        ["git", "rm", "--cached", "vendor/sub"],
        cwd=super_dir,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "restore vendor/sub", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"should re-stage a reachable gitlink missing from the index\n"
        f"stderr: {result.stderr}"
    )
    committed_sha = subprocess.run(
        ["git", "ls-tree", "HEAD", "vendor/sub"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[2]
    assert (
        committed_sha == new_sha
    ), f"gitlink should be restored at {new_sha[:12]}, got {committed_sha[:12]}"


def test_dir_pathspec_refuses_ignored_untracked_nested_repo(tmp_path: Path):
    """Directory pathspec must detect an ignored untracked nested repo whose
    worktree HEAD is *unreachable* from its origin/master.

    `git ls-files --others --exclude-standard` hides gitignore'd paths, so a
    nested repo whose path is ignored would be invisible to the directory
    scan and `git add <dir>` could record its worktree HEAD as a gitlink
    (P1 in the #1772 review of the directory-pathspec scan).  The nested repo
    here has a *real* origin remote seeded on master, then is advanced to an
    unpushed feature commit — so the refusal must come from the reachability
    check, not merely from a missing remote (the previous version of this
    test created the nested repo with no remote, so it passed for the wrong
    reason)."""
    super_dir, _sub_path, _sha = _superproject_with_submodule(tmp_path)

    # A real origin remote for the nested repo, so reachability is checkable.
    nested_origin = tmp_path / "sub2_origin"
    _make_sub_repo(nested_origin, commit_msg="nested init")

    nested = super_dir / "vendor" / "sub2"
    subprocess.run(
        ["git", "clone", str(nested_origin), str(nested)],
        check=True,
        capture_output=True,
    )
    for cmd in (
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        # Bypass the global identity-guard hooks, as other tests do
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)

    # Unpushed feature commit → not reachable from origin/master
    (nested / "feature.txt").write_text("nested feature")
    subprocess.run(
        ["git", "add", "feature.txt"], cwd=nested, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "nested feature"],
        cwd=nested,
        check=True,
        capture_output=True,
    )

    (super_dir / "vendor" / ".gitignore").write_text("sub2\n")
    (super_dir / "vendor" / "new.txt").write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit vendor should refuse when an ignored untracked "
        "nested repo has an unreachable worktree HEAD"
    )
    assert "not reachable" in result.stderr.lower(), (
        "refusal must be the reachability guard, not a missing-remote error; "
        f"got: {result.stderr!r}"
    )
    # The nested repo must not have been recorded as a gitlink
    ls = subprocess.run(
        ["git", "ls-files", "-s", "--", "vendor/sub2"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert "160000" not in ls.stdout, "ignored nested repo was staged as a gitlink"


def test_dir_pathspec_refuses_repo_nested_under_untracked_dir(tmp_path: Path):
    """A directory pathspec must detect a nested repo that lives *inside* an
    as-yet-untracked intermediate directory (`vendor/newdir/sub2`).

    `git ls-files --others --directory` collapses the untracked `newdir` to a
    single trailing-slash entry, so the deeper repo is never probed and
    `git add vendor` records its worktree HEAD as a gitlink — the exact leak
    #1776 describes.  The nested repo has a real origin remote and is advanced
    to an unpushed feature commit, so the refusal must come from the
    reachability guard, not from a missing remote."""
    super_dir, _sub_path, _sha = _superproject_with_submodule(tmp_path)

    nested_origin = tmp_path / "sub3_origin"
    _make_sub_repo(nested_origin, commit_msg="nested init")

    newdir = super_dir / "vendor" / "newdir"
    newdir.mkdir()
    nested = newdir / "sub2"
    subprocess.run(
        ["git", "clone", str(nested_origin), str(nested)],
        check=True,
        capture_output=True,
    )
    for cmd in (
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)

    # Unpushed feature commit → not reachable from origin/master
    (nested / "feature.txt").write_text("nested feature")
    subprocess.run(
        ["git", "add", "feature.txt"], cwd=nested, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "nested feature"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (newdir / "plain.txt").write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit vendor should refuse when a repo nested under an "
        "untracked directory has an unreachable worktree HEAD"
    )
    assert "not reachable" in result.stderr.lower(), (
        "refusal must be the reachability guard, not a missing-remote error; "
        f"got: {result.stderr!r}"
    )
    ls = subprocess.run(
        ["git", "ls-files", "-s", "--", "vendor/newdir/sub2"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert (
        "160000" not in ls.stdout
    ), "nested repo under untracked dir was staged as a gitlink"


def test_untracked_dir_pathspec_refuses_repo_nested_inside_it(tmp_path: Path):
    """A pathspec that *is* an untracked intermediate directory must still be
    scanned for repos nested inside it.

    With `git-safe-commit vendor/newdir` where `vendor/newdir` is untracked and
    `vendor/newdir/sub2` is a repo, `git ls-files --others --directory` collapses
    the whole untracked subtree to the single entry `vendor/newdir/` — which is
    the pathspec itself. The pre-fix scan skipped that entry (`[ "$path" = "$ps" ]`
    -> continue) before the subtree re-list, so the nested repo was never probed
    and `git add vendor/newdir` recorded its worktree HEAD as a gitlink, bypassing
    the reachability guard. The nested repo has a real origin remote and an
    unpushed feature commit, so the refusal must come from the reachability guard
    rather than a missing-remote error."""
    super_dir, _sub_path, _sha = _superproject_with_submodule(tmp_path)

    nested_origin = tmp_path / "sub4_origin"
    _make_sub_repo(nested_origin, commit_msg="nested init")

    newdir = super_dir / "vendor" / "newdir"
    newdir.mkdir()
    nested = newdir / "sub2"
    subprocess.run(
        ["git", "clone", str(nested_origin), str(nested)],
        check=True,
        capture_output=True,
    )
    for cmd in (
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)

    # Unpushed feature commit → not reachable from origin/master
    (nested / "feature.txt").write_text("nested feature")
    subprocess.run(
        ["git", "add", "feature.txt"], cwd=nested, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "nested feature"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    # A regular file too, so the pathspec is a non-empty directory and not a gitlink.
    (newdir / "plain.txt").write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor/newdir", "-m", "newdir changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "git-safe-commit vendor/newdir should refuse when a repo nested inside "
        "the untracked pathspec has an unreachable worktree HEAD"
    )
    assert "not reachable" in result.stderr.lower(), (
        "refusal must be the reachability guard, not a missing-remote error; "
        f"got: {result.stderr!r}"
    )
    ls = subprocess.run(
        ["git", "ls-files", "-s", "--", "vendor/newdir/sub2"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert (
        "160000" not in ls.stdout
    ), "repo nested inside the untracked pathspec was staged as a gitlink"


def test_new_gitlink_without_remote_refused_with_specific_message(tmp_path: Path):
    """A nested repo with no origin remote cannot have reachability verified.

    Refusing is deliberate (see `_gsc_stage_gitlink_path`): a remote-less
    nested repo's worktree HEAD could be a sibling's in-progress branch just
    as easily as a remote-backed one, and there is no public SHA to fall back
    to.  The error must say so specifically rather than reuse the
    "not reachable from origin/master" wording, so callers understand the
    remedy (`git update-index --cacheinfo 160000 <sha> <path>`)."""
    super_dir, _sub_path, _sha = _superproject_with_submodule(tmp_path)

    nested = super_dir / "vendor" / "sub2"
    nested.mkdir(parents=True)
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)
    (nested / "f.txt").write_text("nested work")
    subprocess.run(["git", "add", "f.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested work"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (super_dir / "vendor" / "new.txt").write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "remote-less nested repo should be refused"
    assert "no origin remote" in result.stderr.lower(), (
        "refusal should call out the missing remote explicitly; "
        f"got: {result.stderr!r}"
    )
    ls = subprocess.run(
        ["git", "ls-files", "-s", "--", "vendor/sub2"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert "160000" not in ls.stdout, "remote-less nested repo was staged as a gitlink"


def test_dot_pathspec_stages_when_submodule_reachable(tmp_path: Path):
    """`. ` must be treated as a directory pathspec, not a gitlink.

    Regression: `_gsc_is_gitlink "."` matched on the first `ls-files -s`
    entry and/or the root's own `.git`, so `git-safe-commit .` was routed
    through the single-gitlink staging path and refused *any* repo that
    contained a submodule — even a reachable one."""
    super_dir, sub_path, _sha = _superproject_with_submodule(tmp_path)

    # Advance the submodule and push so its worktree HEAD is reachable
    (sub_path / "v2.txt").write_text("v2")
    subprocess.run(
        ["git", "add", "v2.txt"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "v2"], cwd=sub_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:master"],
        cwd=sub_path,
        check=True,
        capture_output=True,
    )
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sub_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(SAFE_COMMIT), ".", "-m", "bump via dot", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`.` pathspec must not refuse a repo with a reachable submodule\n"
        f"stderr: {result.stderr}"
    )
    committed_sha = subprocess.run(
        ["git", "ls-tree", "HEAD", "vendor/sub"],
        cwd=super_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[2]
    assert (
        committed_sha == new_sha
    ), f"`.` should bump the gitlink to {new_sha[:12]}, got {committed_sha[:12]}"


def test_new_gitlink_with_unresolvable_default_branch_names_the_real_gap(
    tmp_path: Path,
):
    """origin exists, but its default branch is neither master nor main and
    origin/HEAD is unset: refuse (fail closed), but don't claim the remote is
    missing."""
    super_dir, _sub_path, _sha = _superproject_with_submodule(tmp_path)

    upstream = tmp_path / "upstream-trunk.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "trunk", str(upstream)],
        check=True,
        capture_output=True,
    )
    nested = super_dir / "vendor" / "sub2"
    nested.mkdir(parents=True)
    for cmd in (
        ["git", "init", "-b", "trunk"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "core.hooksPath", "/dev/null"],
        ["git", "remote", "add", "origin", str(upstream)],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)
    (nested / "f.txt").write_text("nested work")
    for cmd in (
        ["git", "add", "f.txt"],
        ["git", "commit", "-m", "nested work"],
        ["git", "push", "origin", "trunk"],
        ["git", "fetch", "origin"],
    ):
        subprocess.run(cmd, cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "-d"],
        cwd=nested,
        capture_output=True,
    )
    (super_dir / "vendor" / "new.txt").write_text("regular file")

    result = subprocess.run(
        [str(SAFE_COMMIT), "vendor", "-m", "vendor changes", "--no-verify"],
        cwd=super_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "unresolvable default branch should be refused"
    stderr = result.stderr.lower()
    assert "no origin remote" not in stderr, f"misleading refusal: {result.stderr!r}"
    assert "default branch" in stderr, f"got: {result.stderr!r}"
