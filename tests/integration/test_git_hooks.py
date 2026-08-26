#!/usr/bin/env python3
"""
Integration tests for git hooks.

Tests the hooks in dotfiles/.config/git/hooks/ to ensure they:
1. Properly handle stdin (git provides ref info via stdin)
2. Block pushes to master/main for non-allowed repos
3. Allow pushes to master for allowed repos (agent workspaces)
4. Pass worktree tracking validation

Run with: pytest tests/integration/test_git_hooks.py -v
Or: python tests/integration/test_git_hooks.py (standalone)

Requirements:
- Git installed
- Write access to /tmp for test repos
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Path to hooks (relative to gptme-contrib root)
HOOKS_DIR = (
    Path(__file__).parent.parent.parent / "dotfiles" / ".config" / "git" / "hooks"
)


def _clean_git_env() -> dict:
    """Return a copy of the environment with all GIT_* variables stripped.

    When tests run inside an autonomous session, GIT_DIR may point at the host
    repo's .git directory.  Any ``git config`` call that inherits that env will
    write into the *host* repo instead of the freshly-initialised test repo,
    silently leaking test identity into production git config.

    We strip *all* GIT_* variables (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE,
    GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, …) for maximum
    isolation — any inherited git-env var can cause subtle misdirection.
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    return env


@pytest.fixture
def temp_repo():
    """Create a temporary git repository for testing."""
    temp_dir = tempfile.mkdtemp(prefix="git_hook_test_")
    repo_path = Path(temp_dir) / "test-repo"
    repo_path.mkdir()

    clean_env = _clean_git_env()

    # Initialize git repo
    subprocess.run(
        ["git", "init"], cwd=repo_path, check=True, capture_output=True, env=clean_env
    )
    # Use --local so config writes stay inside the temp repo even if GIT_DIR
    # leaks in from an outer context.  The renamed identity makes any accidental
    # leak immediately obvious in ``git log`` output.
    subprocess.run(
        ["git", "config", "--local", "user.email", "test-automation@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "--local", "user.name", "Test Automation"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )
    # Disable global hooks so the host machine's pre-commit setup doesn't
    # interfere with the test repo's commit operations.
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", "/dev/null"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Create initial commit (--no-verify to skip pre-commit hooks during test setup)
    (repo_path / "README.md").write_text("# Test Repo")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )

    yield repo_path

    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def hook_env(temp_repo):
    """Set up environment for running hooks."""
    # Copy hooks to test repo
    hooks_target = temp_repo / ".git" / "hooks"
    hooks_target.mkdir(exist_ok=True)

    # Copy pre-push and its dependencies
    for hook_file in HOOKS_DIR.iterdir():
        if hook_file.is_file():
            dest = hooks_target / hook_file.name
            shutil.copy(hook_file, dest)
            dest.chmod(0o755)

    # Copy allowed-repos.conf if it exists
    allowed_conf = HOOKS_DIR.parent / "allowed-repos.conf"
    if allowed_conf.exists():
        shutil.copy(allowed_conf, hooks_target.parent / "allowed-repos.conf")

    return temp_repo


def run_pre_push_hook(
    repo_path: Path,
    remote_url: str = "https://github.com/test/repo",
    ref_info: str = "refs/heads/feature abc123 refs/heads/feature def456",
) -> subprocess.CompletedProcess:
    """Run the pre-push hook with given stdin (ref info)."""
    hook_path = repo_path / ".git" / "hooks" / "pre-push"

    if not hook_path.exists():
        pytest.skip("pre-push hook not found")

    clean_env = _clean_git_env()

    # Set up fake remote (ignore error if already added from a previous call)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=repo_path,
        capture_output=True,
        env=clean_env,
    )

    # Run hook with stdin — env must be clean so the hook's own git calls
    # don't accidentally operate on the host repo via an inherited GIT_DIR.
    result = subprocess.run(
        [str(hook_path), "origin", remote_url],
        cwd=repo_path,
        input=ref_info,
        text=True,
        capture_output=True,
        env=clean_env,
    )

    return result


class TestPrePushStdinHandling:
    """Test that pre-push hook properly handles stdin."""

    def test_stdin_preserved_for_feature_branch(self, hook_env):
        """Stdin should be read and passed to worktree validation."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/test/repo",
            ref_info="refs/heads/feature abc123 refs/heads/feature def456",
        )
        # Feature branch push should succeed (not blocked)
        # Even if worktree validation warns, it shouldn't fail
        # Exit 0 = success, Exit 1 = blocked
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

    def test_stdin_available_for_master_check(self, hook_env):
        """Stdin should be readable for master/main detection."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/test/repo",  # Non-allowed repo
            ref_info="refs/heads/feature abc123 refs/heads/master def456",
        )
        # Should be blocked (trying to push to master)
        assert result.returncode == 1
        assert "master" in result.stdout.lower() or "blocked" in result.stdout.lower()


class TestMasterMainProtection:
    """Test that pushes to master/main are blocked for non-allowed repos."""

    def test_blocks_push_to_master(self, hook_env):
        """Push to refs/heads/master should be blocked."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/random/repo",
            ref_info="refs/heads/feature abc123 refs/heads/master def456",
        )
        assert result.returncode == 1
        assert "blocked" in result.stdout.lower() or "error" in result.stdout.lower()

    def test_blocks_push_to_main(self, hook_env):
        """Push to refs/heads/main should be blocked."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/random/repo",
            ref_info="refs/heads/feature abc123 refs/heads/main def456",
        )
        assert result.returncode == 1
        assert "blocked" in result.stdout.lower() or "error" in result.stdout.lower()

    def test_allows_push_to_feature_branch(self, hook_env):
        """Push to feature branches should be allowed."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/random/repo",
            ref_info="refs/heads/feature abc123 refs/heads/feature def456",
        )
        # Should succeed (exit 0)
        assert result.returncode == 0


class TestAllowedReposBypass:
    """Test that allowed repos can push to master."""

    def test_gptme_agent_template_allowed(self, hook_env):
        """gptme-agent-template should be allowed to push to master."""
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/gptme/gptme-agent-template",
            ref_info="refs/heads/feature abc123 refs/heads/master def456",
        )
        # Should succeed (allowed repo)
        assert result.returncode == 0

    def test_agent_workspace_auto_allowed(self, hook_env):
        """Repos with gptme.toml [agent] section should be auto-allowed."""
        # Create gptme.toml with [agent] section
        gptme_toml = hook_env / "gptme.toml"
        gptme_toml.write_text('[agent]\nname = "test-agent"\n')

        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/test/my-agent",
            ref_info="refs/heads/feature abc123 refs/heads/master def456",
        )
        # Should succeed (auto-detected agent workspace)
        assert result.returncode == 0


class TestWorktreeValidation:
    """Test worktree tracking validation integration."""

    def test_validation_script_receives_stdin(self, hook_env):
        """validate-worktree-tracking.sh should receive stdin from pre-push."""
        # This test verifies the stdin piping fix (PR #111)
        # The validation script needs stdin to detect new branch pushes
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/test/repo",
            ref_info="refs/heads/feature abc123 refs/heads/feature 0000000000000000000000000000000000000000",
        )
        # New branch (remote sha is zeros) should trigger validation
        # Even if it warns, it should not fail (exit 0)
        # The key is that it receives the stdin and processes it
        assert result.returncode == 0

    def test_detached_head_skips_validation(self, hook_env):
        """Detached HEAD (submodules, rebases) should skip worktree validation."""
        clean_env = _clean_git_env()

        # Detach HEAD by checking out a specific commit
        subprocess.run(
            ["git", "checkout", "--detach", "HEAD"],
            cwd=hook_env,
            check=True,
            capture_output=True,
            env=clean_env,
        )

        # Push from detached HEAD should succeed (validation skipped)
        result = run_pre_push_hook(
            hook_env,
            remote_url="https://github.com/test/repo",
            ref_info=(
                "(delete) 0000000000000000000000000000000000000000"
                " refs/heads/old-branch abc123"
            ),
        )
        assert result.returncode == 0, f"Hook failed on detached HEAD: {result.stderr}"


def run_validate_worktree_tracking(
    repo_path: Path,
    ref_info: str,
) -> subprocess.CompletedProcess:
    """Invoke validate-worktree-tracking.sh directly with the given stdin.

    Calling the validator directly (rather than through pre-push) isolates the
    tracking rules from the master/main-protection and mass-delete stanzas, which
    would otherwise mask which check actually fired.
    """
    script = repo_path / ".git" / "hooks" / "validate-worktree-tracking.sh"
    if not script.exists():
        pytest.skip("validate-worktree-tracking.sh not found")

    # A remote must exist for `git branch --set-upstream-to=origin/...` in the
    # upstream tests; harmless for the rest. Ignore failure if already added.
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo"],
        cwd=repo_path,
        capture_output=True,
        env=_clean_git_env(),
    )

    return subprocess.run(
        ["bash", str(script), "origin", "https://github.com/test/repo"],
        cwd=repo_path,
        input=ref_info,
        text=True,
        capture_output=True,
        env=_clean_git_env(),
    )


def _checkout_branch(repo_path: Path, name: str) -> None:
    subprocess.run(
        ["git", "checkout", "-q", "-b", name],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )


class TestNoUpstreamTracking:
    """Rules applied when the current branch has no upstream.

    Regression coverage for the false block that hit every PR worker checking a
    PR out under a local name differing from the PR's remote branch (gptme#3468).
    The guard's real purpose is preventing a push from landing on master/main
    unintentionally, which is a property of the *destination* — git resolves
    refspecs before the hook runs, so the destination is always known here.
    """

    def test_allows_explicit_refspec_to_differently_named_feature_branch(
        self, hook_env
    ):
        """The #3468 shape: local `pr-3468` -> remote `fix-3440-server-e2e`.

        Destination is explicit and is not master/main, so it must be allowed.
        """
        _checkout_branch(hook_env, "pr-3468")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/pr-3468 abc123 refs/heads/fix-3440-server-e2e "
            "0000000000000000000000000000000000000000",
        )
        assert result.returncode == 0, (
            f"Explicit refspec to a feature branch was blocked: "
            f"{result.stdout}{result.stderr}"
        )
        # Must not be a silent downgrade — the allowed destination is named.
        assert "fix-3440-server-e2e" in result.stdout

    def test_blocks_push_to_master_with_no_upstream(self, hook_env):
        """No upstream + destination master is the real hazard — still blocked."""
        _checkout_branch(hook_env, "some-feature")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/some-feature abc123 refs/heads/master def456",
        )
        assert result.returncode == 1
        assert "master" in result.stdout.lower()

    def test_blocks_push_to_main_with_no_upstream(self, hook_env):
        """Same for `main`-default repos."""
        _checkout_branch(hook_env, "some-feature")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/some-feature abc123 refs/heads/main def456",
        )
        assert result.returncode == 1

    def test_allows_same_name_new_branch_push(self, hook_env):
        """The pre-existing allowance (same-named new branch) must keep working."""
        _checkout_branch(hook_env, "my-feature")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/my-feature abc123 refs/heads/my-feature "
            "0000000000000000000000000000000000000000",
        )
        assert result.returncode == 0

    def test_allows_branch_deletion(self, hook_env):
        """Deletions carry a zero local sha and touch nothing we protect."""
        _checkout_branch(hook_env, "my-feature")
        result = run_validate_worktree_tracking(
            hook_env,
            "(delete) 0000000000000000000000000000000000000000 "
            "refs/heads/old-branch abc123",
        )
        assert result.returncode == 0

    def test_allows_empty_stdin(self, hook_env):
        """An up-to-date push invokes the hook with nothing to validate."""
        _checkout_branch(hook_env, "my-feature")
        result = run_validate_worktree_tracking(hook_env, "")
        assert result.returncode == 0

    def test_allows_blank_line_stdin(self, hook_env):
        """A forwarded single empty line must not read as a real ref."""
        _checkout_branch(hook_env, "my-feature")
        result = run_validate_worktree_tracking(hook_env, "\n")
        assert result.returncode == 0

    def test_mixed_push_with_master_destination_is_blocked(self, hook_env):
        """A feature destination must not launder a master destination beside it."""
        _checkout_branch(hook_env, "pr-999")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/pr-999 abc123 refs/heads/some-feature def456\n"
            "refs/heads/pr-999 abc123 refs/heads/master def456",
        )
        assert result.returncode == 1


class TestUpstreamTracksDefaultBranch:
    """The `worktree add -b feat origin/master` trap: upstream IS origin/master."""

    def _set_upstream_to_master(self, repo_path: Path, branch: str) -> None:
        clean_env = _clean_git_env()
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo"],
            cwd=repo_path,
            capture_output=True,
            env=clean_env,
        )
        # Fabricate a remote-tracking ref so `@{u}` resolves without a network.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/master", head],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "branch", "--set-upstream-to=origin/master", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=clean_env,
        )

    def test_blocks_when_upstream_is_master_and_destination_is_master(self, hook_env):
        """The original hazard — must stay blocked."""
        _checkout_branch(hook_env, "trap-branch")
        self._set_upstream_to_master(hook_env, "trap-branch")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/trap-branch abc123 refs/heads/master def456",
        )
        assert result.returncode == 1
        assert "trap-branch" in result.stdout

    def test_allows_when_upstream_is_master_but_destination_is_feature(self, hook_env):
        """Explicit feature-branch push is safe even with a bad upstream."""
        _checkout_branch(hook_env, "trap-branch")
        self._set_upstream_to_master(hook_env, "trap-branch")
        result = run_validate_worktree_tracking(
            hook_env,
            "refs/heads/trap-branch abc123 refs/heads/trap-branch def456",
        )
        assert result.returncode == 0


def test_hooks_exist():
    """Verify that required hook files exist."""
    assert HOOKS_DIR.exists(), f"Hooks directory not found: {HOOKS_DIR}"
    assert (HOOKS_DIR / "pre-push").exists(), "pre-push hook not found"
    assert (HOOKS_DIR / "validate-worktree-tracking.sh").exists(), (
        "validate-worktree-tracking.sh not found"
    )
    assert (HOOKS_DIR / "pre-commit").exists(), "pre-commit hook not found"


def run_pre_commit_hook(repo_path: Path, email: str) -> subprocess.CompletedProcess:
    """Stage a change and invoke the pre-commit hook directly with a given
    committing identity. Runs the hook script directly (the fixture disables
    core.hooksPath) so we exercise the hook logic, not git's dispatch."""
    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    if not hook_path.exists():
        pytest.skip("pre-commit hook not found")

    clean_env = _clean_git_env()
    # Put the repo on a feature branch so the master-commit guard is not the
    # thing that aborts (we want to isolate the identity check).
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature-identity-test"],
        cwd=repo_path,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "--local", "user.email", email],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )
    (repo_path / "change.txt").write_text("content")
    subprocess.run(
        ["git", "add", "change.txt"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        env=clean_env,
    )
    return subprocess.run(
        ["bash", str(hook_path)],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=clean_env,
    )


class TestGitIdentityValidation:
    """Part 0.5 of the pre-commit hook: block non-allowlisted commit identities."""

    def test_disallowed_email_is_blocked(self, hook_env):
        """A typo-blend / wrong identity (the historical corruption) must abort."""
        result = run_pre_commit_hook(hook_env, "timetolearnbob@gmail.com")
        assert result.returncode != 0
        assert "NOT an allowed identity" in (result.stdout + result.stderr)

    def test_allowed_email_passes_identity_check(self, hook_env):
        """A canonical Bob identity must NOT trip the identity guard."""
        result = run_pre_commit_hook(hook_env, "bob@superuserlabs.org")
        # The hook may exit non-zero for unrelated later parts (e.g. no prek),
        # but the identity guard specifically must not fire.
        assert "NOT an allowed identity" not in (result.stdout + result.stderr)
        assert "user.email is not set" not in (result.stdout + result.stderr)

    def test_bypass_env_skips_identity_check(self, hook_env):
        """ALLOW_GIT_IDENTITY=1 skips the guard for deliberate exceptions."""
        hook_path = hook_env / ".git" / "hooks" / "pre-commit"
        clean_env = _clean_git_env()
        clean_env["ALLOW_GIT_IDENTITY"] = "1"
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature-bypass-test"],
            cwd=hook_env,
            capture_output=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "config", "--local", "user.email", "timetolearnbob@gmail.com"],
            cwd=hook_env,
            check=True,
            capture_output=True,
            env=clean_env,
        )
        (hook_env / "b.txt").write_text("x")
        subprocess.run(
            ["git", "add", "b.txt"],
            cwd=hook_env,
            check=True,
            capture_output=True,
            env=clean_env,
        )
        result = subprocess.run(
            ["bash", str(hook_path)],
            cwd=hook_env,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        assert "NOT an allowed identity" not in (result.stdout + result.stderr)


if __name__ == "__main__":
    # Allow running as standalone script
    pytest.main([__file__, "-v"])
