"""Tests for the shared-checkout git guard (Claude Code PreToolUse hook)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).parent.parent
    / "scripts"
    / "claude-code-hooks"
    / "shared-checkout-git-guard.py"
)
_spec = importlib.util.spec_from_file_location("shared_checkout_git_guard", _PATH)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

SHARED = Path("/repo/shared")
OTHER = Path("/repo/other")


def detect(command: str, cwd: Path = SHARED, *, worktrees: bool = False):
    return guard.detect(
        command,
        cwd,
        shared_checkout=lambda p: SHARED in (p, *p.parents),
        stash_stack_shared=lambda p: worktrees,
        autostash=lambda p, sub: False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git stash",
        "git stash -u",
        "git stash push -m wip",
        "git stash pop",
        "git reset --hard",
        "git reset --hard origin/master",
        "git reset",
        "git reset HEAD",
        "git reset -- .",
        "git reset origin/master",
        "git reset 1a2b3c4d",
        "git reset -q HEAD~2",
        "git reset file.py",  # ambiguous: could be a commit; use `reset -- file.py`
        "git pull --rebase --autostash",
        "git rebase origin/master --autostash",
        "git clean -fd",
        "git clean -df",
        "git clean -f",
        "git clean -fx",
        "git clean -fX",
        "git clean --force",
        "git add -u",
        "git add --update",
        "git add -- .",
        "git add -Av",
        "git add -uv",
        "git add -vA",
        'git add "."',
        "git add --chmod=+x -A",
        "git add -v --ignore-errors .",
        "git checkout -- .",
        "git checkout .",
        "git checkout HEAD -- .",
        "git checkout origin/master -- .",
        'git restore -- "."',
        "git checkout -- '.'",
        'git reset -- "."',
        "git stash; git status",
        "git stash&&git status",
        "git stash|cat",
        "git status # inspect first\ngit reset --hard",
        "git merge --autostash feature",
        "git reset HEAD --",
        "git reset -q HEAD~1 --",
        "if true; then git reset --hard; fi",
        "while true; do git stash; done",
        "! git add -A",
        "{ git clean -fd; }",
        "git -c rebase.autoStash=true pull",
        "git -c merge.autostash pull --rebase",
        "git add -A >/dev/null 2>&1",
        "cat <<'EOF' > note.txt\nharmless\nEOF\ngit reset --hard",
        "git checkout main .",
        "git reset --keep origin/master",
        "git reset --merge HEAD~1",
        "git checkout -f",
        "git checkout -f main",
        "git checkout --force other-branch",
        "git switch --discard-changes main",
        "git restore .",
        "git restore --staged .",
        "git add -A",
        "git add --all",
        "git add .",
        "git commit -a -m wip",
        "git commit -am wip",
        "git status && git stash",
        "echo hi; git add -A && git commit -m x",
        # Global options before the subcommand must not hide it.
        "git -c core.pager=cat stash",
        "git --no-pager reset --hard",
        "git -c color.ui=never add -A",
        "git --git-dir=.git --work-tree=. clean -fd",
        "git --no-pager -c x=y -p restore .",
        "git restore -- .",
        "git restore --staged -- .",
        "git restore --source=HEAD~1 --worktree -- .",
        "git restore -s HEAD .",
    ],
)
def test_shared_checkout_denies_tree_wide_operations(command):
    reason = detect(command)
    assert reason is not None and "shared checkout" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git stash list",
        "git stash show -p",
        "git add path/to/file.py",
        "git add ./file.py docs/x.md",
        "git commit -m 'fix: thing' -- path/to/file.py",
        "git commit --amend --no-edit",
        "git reset -- path/to/file.py",
        "git reset HEAD path/to/file.py",
        "git reset origin/master -- path/to/file.py",
        "git reset --soft HEAD~1",
        "git pull --ff-only",
        "git restore path/to/file.py",
        "git restore -- path/to/file.py",
        "git restore --staged -- ./file.py",
        "git checkout -- path/to/file.py",
        "git checkout feature-branch",
        "git checkout -b new-branch",
        "git switch main",
        "git checkout ./file.py",
        "git clean -n",
        "git stash apply 1a2b3c4d",
        'git stash apply "$sha"',
        "sha=$(git stash create)",
        "git clean -nd",
        "git add -u path/to/file.py",
        "git add --chmod=+x script.sh",
        "git add -- path/to/file.py",
        "git add -v path/to/file.py",
        "git add -uv path/to/file.py",
        "git add -p",
        'git commit -m "document -a behavior" -- docs/usage.md',
        "git commit -m 'fix: -a and --all in docs' -- README.md",
        "git commit -F msg.txt -- a.py",
        # Text that only mentions destructive commands is not a command.
        "cat > notes.md <<'EOF'\nnever run git stash or git reset --hard here\nEOF",
        "git commit -F - -- a.py <<'EOF'\nfix: stop using git add -A\nEOF",
        'git commit -m "first line\n\nnot git add -A" -- a.py',
        "# git reset --hard would be bad\ngit status",
        "git merge --no-autostash feature",
        "git -c rebase.autoStash=false pull --rebase",
        "git log > out.txt 2>&1",
        "git reset HEAD -- path/to/file.py",
        "if git diff --quiet; then git status; fi",
        "git log --oneline -5",
        "git --no-pager log -1",
        "git -c core.pager=cat diff",
    ],
)
def test_shared_checkout_allows_scoped_operations(command):
    assert detect(command) is None


def test_unmarked_repo_is_not_guarded():
    assert detect("git reset --hard", cwd=OTHER) is None
    assert detect("git add -A", cwd=OTHER) is None


def test_git_dash_c_targets_the_named_repo():
    assert detect(f"git -C {SHARED} reset --hard", cwd=OTHER) is not None
    assert detect(f"git -C {OTHER} reset --hard", cwd=SHARED) is None
    assert detect(f"git --no-pager -C {SHARED} stash", cwd=OTHER) is not None
    # Repeated -C chain, as in git: /repo then shared.
    assert detect("git -C /repo -C shared add -A", cwd=OTHER) is not None


def test_quoted_option_paths_with_spaces(tmp_path):
    spaced = (tmp_path / "My Repos" / "shared").resolve()

    def check(command: str):
        return guard.detect(
            command,
            OTHER,
            shared_checkout=lambda p: spaced in (p, *p.parents),
            stash_stack_shared=lambda p: False,
        )

    assert check(f'git -C "{spaced}" reset --hard') is not None
    assert check(f"git -C '{spaced}' clean -fd") is not None
    assert check(f'git --work-tree="{spaced}" add -A') is not None
    assert check(f'git -C "{spaced}" status') is None
    assert detect(f'git -C "{SHARED}" reset --hard', cwd=OTHER) is not None


def test_git_dir_option_targets_that_repo():
    assert detect(f"git --git-dir={SHARED}/.git stash drop", cwd=OTHER) is not None
    assert detect(f"git --git-dir {SHARED}/.git reset --hard", cwd=OTHER) is not None
    assert detect(f"git --git-dir={OTHER}/.git reset --hard", cwd=SHARED) is None


def test_failed_cd_then_semicolon_checks_cwd_too():
    # If the cd fails, `git status` is skipped but the reset still runs in cwd.
    assert (
        detect("cd /does-not-exist && git status; git reset --hard", cwd=SHARED)
        is not None
    )
    assert detect(f"cd {OTHER} && git status && git reset --hard", cwd=SHARED) is None


def test_git_dir_environment_and_wrappers_are_followed():
    for command in (
        f"GIT_DIR={SHARED}/.git GIT_WORK_TREE={SHARED} git reset --hard",
        f"env GIT_DIR={SHARED}/.git git reset --hard",
        f"export GIT_DIR={SHARED}/.git; git stash",
        f"GIT_WORK_TREE={SHARED} git clean -fd",
    ):
        assert detect(command, cwd=OTHER) is not None, command
    for command in (
        "timeout 10 git reset --hard",
        "nice git stash",
        "nice -n 5 git add -A",
        "stdbuf -oL git add -A",
        "sudo -u me git clean -fd",
        "env -u PAGER git reset --hard",
    ):
        assert detect(command, cwd=SHARED) is not None, command
    assert detect("timeout 10 git status", cwd=SHARED) is None
    assert detect(f"GIT_DIR={OTHER}/.git git reset --hard", cwd=OTHER) is None


def test_work_tree_option_targets_that_tree():
    assert detect(f"git --work-tree={SHARED} clean -fd", cwd=OTHER) is not None
    assert detect(f"git --work-tree {SHARED} reset --hard", cwd=OTHER) is not None
    # --work-tree does not change the repository: from inside the shared
    # checkout its index is still the one reset.
    assert detect(f"git --work-tree={OTHER} reset HEAD", cwd=SHARED) is not None
    assert detect("git --work-tree=/tmp stash drop", cwd=SHARED) is not None
    assert (
        detect(f"git --git-dir={OTHER}/.git --work-tree={OTHER} reset", cwd=SHARED)
        is None
    )


def test_chained_relative_cds_resolve_in_order(tmp_path):
    srv = tmp_path / "srv"
    (srv / "shared").mkdir(parents=True)
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    marked = (srv / "shared").resolve()

    def check(command: str):
        return guard.detect(
            command,
            unmarked,
            shared_checkout=lambda p: marked in (p, *p.parents),
            stash_stack_shared=lambda p: False,
        )

    assert check(f"cd {srv} && cd shared && git reset --hard") is not None
    assert check(f"pushd {srv} && cd shared && git add -A") is not None
    assert check(f"cd {srv} && cd .. && git reset --hard") is None


def test_untokenizable_destructive_command_fails_closed_where_guarded():
    for command in ("git merge --autostash feature 'oops", 'git reset --hard "x'):
        reason = detect(command, cwd=SHARED)
        assert reason is not None and "could not be tokenized" in reason
        assert detect(command, cwd=OTHER, worktrees=True) is not None
        # An unmarked repo without linked worktrees keeps its contract.
        assert detect(command, cwd=OTHER) is None
    assert detect("echo 'unbalanced", cwd=SHARED) is None


def test_autostash_enabled_in_config_is_caught():
    def check(command: str, enabled: bool):
        return guard.detect(
            command,
            SHARED,
            shared_checkout=lambda p: True,
            stash_stack_shared=lambda p: False,
            autostash=lambda p, sub: enabled,
        )

    assert check("git pull --rebase", True) is not None
    assert check("git merge feature", True) is not None
    assert check("git pull --rebase", False) is None
    assert check("git pull --no-autostash", True) is None


def test_predicates_fail_closed_when_git_is_unavailable(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def boom(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(guard.subprocess, "run", boom)
    assert guard.is_shared_checkout(repo) is True
    assert guard.autostash_enabled(repo, "pull") is True
    assert guard.is_shared_checkout(tmp_path / "not-a-repo-x") is False


def test_cd_inside_a_subshell_does_not_leak():
    # The cd happens in a subshell; `git add -A` runs in the shared cwd.
    assert detect(f"(cd {OTHER} && make) && git add -A", cwd=SHARED) is not None
    assert detect(f"(cd {OTHER}) && git reset --hard", cwd=SHARED) is not None
    assert detect(f"x=$(cd {OTHER} && pwd) && git stash", cwd=SHARED) is not None
    # Inside the subshell the cd applies.
    assert detect(f"(cd {SHARED} && git reset --hard)", cwd=OTHER) is not None
    assert detect(f"(cd {OTHER} && git reset --hard)", cwd=SHARED) is None


def test_several_cds_check_every_target():
    # cwd unmarked; a later cd lands in the shared checkout.
    assert detect(f"cd /tmp && cd {SHARED} && git reset --hard", cwd=OTHER) is not None
    assert detect(f"cd {OTHER} && cd {SHARED}; git add -A", cwd=OTHER) is not None
    assert detect(f"cd /tmp && cd {OTHER} && git reset --hard", cwd=OTHER) is None


def test_cd_inside_a_quoted_argument_is_not_a_directory_change():
    assert detect(f'git stash push -m "cd {SHARED}"', cwd=OTHER) is None
    assert detect(f"git commit -am 'cd {SHARED}'", cwd=OTHER) is None
    # A quoted cd target still counts.
    assert detect(f'cd "{SHARED}" && git add -A', cwd=OTHER) is not None


def test_unresolvable_directories_fail_closed_where_guarded():
    for command in (
        'cd "$WORKTREE" && git reset --hard',
        'cd /tmp && cd "$SHARED" && git reset --hard',
        'git -C "$REPO" clean -fd',
        "cd `pwd` && git add -A",
    ):
        reason = detect(command, cwd=SHARED)
        assert reason is not None and "literal path" in reason, command
        # An unmarked repo keeps its contract: common `cd "$X"` idioms work.
        assert detect(command, cwd=OTHER) is None, command
    # Harmless commands in an unknown directory stay allowed.
    assert detect('cd "$WORKTREE" && git status', cwd=SHARED) is None
    assert detect('git -C "$REPO" log -1', cwd=SHARED) is None


def test_pushd_popd_return_to_the_previous_directory():
    assert (
        detect(f"pushd {OTHER} && make && popd && git add -A", cwd=SHARED) is not None
    )
    assert detect(f"pushd {SHARED} && popd && git add -A", cwd=OTHER) is None
    assert (
        detect("pushd build && make && popd && git commit -am wip", cwd=OTHER) is None
    )


def test_commit_dash_s_does_not_swallow_the_next_option():
    assert detect("git commit -S -a -m wip", cwd=SHARED) is not None
    assert detect("git commit -S --all -m x", cwd=SHARED) is not None
    assert detect("git commit -Skey123 -m x -- a.py", cwd=SHARED) is None
    assert detect("git commit -uall -m x -- a.py", cwd=SHARED) is None


def test_paths_resolving_to_the_repo_root_are_whole_tree(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()

    def check(command: str, cwd: Path):
        return guard.detect(
            command,
            cwd,
            shared_checkout=lambda p: True,
            stash_stack_shared=lambda p: False,
            autostash=lambda p, sub: False,
        )

    for command in (
        "git checkout -- ..",
        "git restore ..",
        "git add ..",
        "git reset -- ..",
    ):
        assert check(command, root / "src") is not None, command
    assert check(f"git restore {root}", root / "src") is not None
    assert check("git restore ./.", root) is not None
    assert check("git restore ../src/a.py", root / "src") is None


def test_cd_with_semicolon_checks_both_directories():
    # `cd X; cmd` runs cmd in X, or in cwd if the cd fails: check both.
    assert detect(f"cd {SHARED}; git reset --hard", cwd=OTHER) is not None
    assert detect(f"cd {OTHER}; git reset --hard", cwd=SHARED) is not None
    assert detect(f"cd {SHARED}\ngit add -A", cwd=OTHER) is not None
    assert detect(f"cd {OTHER}; git reset --hard", cwd=OTHER) is None


def test_single_leading_cd_moves_the_target():
    assert detect(f"cd {OTHER} && git reset --hard", cwd=SHARED) is None
    assert detect(f"cd {SHARED} && git add -A", cwd=OTHER) is not None
    # Two cds could hop back into the shared tree: keep the original cwd.
    assert (
        detect(f"cd {OTHER} && cd {SHARED} && git reset --hard", cwd=SHARED) is not None
    )


def test_stash_stack_rule_in_repos_with_linked_worktrees():
    reason = detect("git stash push -m wip", cwd=OTHER, worktrees=True)
    assert reason is not None and "worktrees share one stash stack" in reason
    for allowed in (
        "git stash list",
        "git stash show",
        "sha=$(git stash create)",
        "git stash apply $sha",
        "git stash apply 1a2b3c4d",
    ):
        assert detect(allowed, cwd=OTHER, worktrees=True) is None, allowed
    assert detect("git stash push", cwd=OTHER, worktrees=False) is None


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_real_repo_predicates(tmp_path, monkeypatch):
    # The guard reads real git config: keep the developer's global/system out.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=main,
    )
    assert guard.is_shared_checkout(main) is False
    assert guard.shares_stash_stack(main) is False
    _git("config", "safety.sharedCheckout", "true", cwd=main)
    assert guard.is_shared_checkout(main) is True
    _git("worktree", "add", "-q", str(tmp_path / "wt"), cwd=main)
    assert guard.shares_stash_stack(main) is True
    assert guard.shares_stash_stack(tmp_path / "wt") is True
    assert guard.detect("git add -A", main) is not None
    _git("config", "rebase.autoStash", "true", cwd=main)
    assert guard.detect("git pull --rebase", main) is not None
    _git("config", "rebase.autoStash", "false", cwd=main)
    assert guard.detect("git pull --rebase", main) is None


def test_hook_output_and_non_bash_passthrough(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "safety.sharedCheckout", "true", cwd=main)

    def run(payload: dict) -> str:
        return subprocess.run(
            [sys.executable, str(_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    denied = json.loads(
        run(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git stash -u"},
                "cwd": str(main),
            }
        )
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        run(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
                "cwd": str(main),
            }
        )
        == ""
    )
    assert run({"tool_name": "Read", "tool_input": {"file_path": "x"}}) == ""
    # Garbage in must never break the Bash tool.
    assert (
        subprocess.run(
            [sys.executable, str(_PATH)],
            input="not json",
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
