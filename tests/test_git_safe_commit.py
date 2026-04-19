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


def test_safe_commit_refuses_dirty_worktree_without_no_verify(git_repo: Path):
    """Dirty worktrees are blocked before prek can stash unrelated files."""
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


def test_safe_commit_rechecks_dirty_worktree_after_waiting_for_lock(git_repo: Path):
    """The dirty-worktree check must happen after lock acquisition, not before."""
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
        (git_repo / "dirty.txt").write_text("dirty\n")
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
    assert proc_a.returncode == 0 or proc_b.returncode == 0

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
