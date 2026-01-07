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

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Path to hooks (relative to gptme-contrib root)
HOOKS_DIR = (
    Path(__file__).parent.parent.parent / "dotfiles" / ".config" / "git" / "hooks"
)


@pytest.fixture
def temp_repo():
    """Create a temporary git repository for testing."""
    temp_dir = tempfile.mkdtemp(prefix="git_hook_test_")
    repo_path = Path(temp_dir) / "test-repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit (--no-verify to skip pre-commit hooks during test setup)
    (repo_path / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
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

    # Set up fake remote
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=repo_path,
        capture_output=True,
    )

    # Run hook with stdin
    result = subprocess.run(
        [str(hook_path), "origin", remote_url],
        cwd=repo_path,
        input=ref_info,
        text=True,
        capture_output=True,
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


def test_hooks_exist():
    """Verify that required hook files exist."""
    assert HOOKS_DIR.exists(), f"Hooks directory not found: {HOOKS_DIR}"
    assert (HOOKS_DIR / "pre-push").exists(), "pre-push hook not found"
    assert (
        HOOKS_DIR / "validate-worktree-tracking.sh"
    ).exists(), "validate-worktree-tracking.sh not found"
    assert (HOOKS_DIR / "pre-commit").exists(), "pre-commit hook not found"


if __name__ == "__main__":
    # Allow running as standalone script
    pytest.main([__file__, "-v"])
