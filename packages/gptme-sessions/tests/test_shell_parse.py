"""Tests for shell_parse — tree-sitter-bash heredoc and commit detection."""

from gptme_sessions.shell_parse import (
    bash_heredoc_write_paths,
    has_git_commit_command,
    has_git_push_command,
)

# ---------------------------------------------------------------------------
# bash_heredoc_write_paths — heredoc write detection
# ---------------------------------------------------------------------------


class TestBashHeredocWritePaths:
    """Parametrised cases for bash_heredoc_write_paths."""

    def test_cat_redirect_then_heredoc(self):
        """cat > file <<EOF — basic case."""
        cmd = "cat > /journal/2026-09-29/session.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/2026-09-29/session.md"]

    def test_cat_append_then_heredoc(self):
        """cat >> file <<EOF — append variant."""
        cmd = "cat >> /journal/2026-09-29/session.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/2026-09-29/session.md"]

    def test_cat_heredoc_then_redirect(self):
        """cat <<'EOF' > file — heredoc comes before redirect."""
        cmd = "cat <<'EOF' > path/to/file.md\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["path/to/file.md"]

    def test_cat_heredoc_quoted_eof(self):
        """cat <<'EOF' > file with single-quoted marker."""
        cmd = "cat <<'EOF' >> /journal/notes.md\ncontent\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/notes.md"]

    def test_cat_heredoc_double_quoted_marker(self):
        """cat <<\"EOF\" > file."""
        cmd = 'cat > out.txt <<"EOF"\ncontent\nEOF'
        assert bash_heredoc_write_paths(cmd) == ["out.txt"]

    def test_cat_indented_heredoc(self):
        """cat > file <<-EOF — dash-indented heredoc."""
        cmd = "cat > /tmp/worktrees/my-feature/result.md <<-EOF\nhello\n\tEOF"
        assert bash_heredoc_write_paths(cmd) == ["/tmp/worktrees/my-feature/result.md"]

    def test_tee_heredoc(self):
        """tee file <<EOF — tee feeds heredoc to a file."""
        cmd = "tee /tmp/worktrees/feature/notes.txt <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/tmp/worktrees/feature/notes.txt"]

    def test_tee_append_heredoc(self):
        """tee -a file <<EOF — tee with -a flag."""
        cmd = "tee -a /journal/2026/notes.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/2026/notes.md"]

    def test_quoted_path(self):
        """cat > 'path/file.md' <<EOF — single-quoted path."""
        cmd = "cat >> '/journal/2026-09-29/session.md' <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/2026-09-29/session.md"]

    def test_path_with_spaces(self):
        """cat > \"path with spaces/file.md\" <<EOF."""
        cmd = 'cat > "path with spaces/file.md" <<EOF\nhello\nEOF'
        assert bash_heredoc_write_paths(cmd) == ["path with spaces/file.md"]

    def test_no_heredoc_plain_redirect_excluded(self):
        """Plain redirect without heredoc is NOT a heredoc write."""
        cmd = "echo hello > /tmp/output.txt"
        # Not a heredoc-fed write, so not detected by this function
        assert bash_heredoc_write_paths(cmd) == []

    def test_dev_null_excluded(self):
        """/dev/null is excluded."""
        cmd = "cat > /dev/null <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == []

    def test_proc_excluded(self):
        """/proc/ paths are excluded."""
        cmd = "cat > /proc/self/fd/1 <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == []

    def test_tmp_paths_included(self):
        """/tmp/ paths are returned; callers decide whether to keep them."""
        cmd = "cat > /tmp/scratch.txt <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/tmp/scratch.txt"]

    def test_tmp_worktrees_included(self):
        """/tmp/worktrees/ paths are included (real feature work)."""
        cmd = "cat > /tmp/worktrees/my-feature/README.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/tmp/worktrees/my-feature/README.md"]

    def test_journal_path_included(self):
        """/journal/ paths are included; callers route them separately."""
        cmd = "cat > /home/bob/bob/journal/2026-09-29/notes.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/home/bob/bob/journal/2026-09-29/notes.md"]

    def test_variable_expansion_in_path_preserved(self):
        """${VAR} in a heredoc path is preserved so the caller can glob it.

        Regression: the tree-sitter concatenation handler used to keep only
        word/raw_string/command_substitution children, dropping ``expansion``
        (``${VAR}``) and mangling "/journal/${DATE}/x.md" into "/journal//x.md".
        The caller keys its glob branch on a literal "${", so the mangled path
        was silently dropped.
        """
        cmd = "cat > /journal/${DATE}/session.md <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["/journal/${DATE}/session.md"]

    def test_bare_variable_expansion_path_preserved(self):
        """A path that is exactly ${FILE} (no concatenation) is preserved."""
        cmd = "cat > ${FILE} <<EOF\nhello\nEOF"
        assert bash_heredoc_write_paths(cmd) == ["${FILE}"]

    def test_multiple_heredocs_in_pipeline(self):
        """Multiple heredoc writes in the same command string."""
        cmd = "cat > /journal/a.md <<EOF\nhello\nEOF\ncat > /journal/b.md <<EOF2\nworld\nEOF2"
        paths = bash_heredoc_write_paths(cmd)
        assert "/journal/a.md" in paths
        assert "/journal/b.md" in paths

    def test_no_heredoc_no_output(self):
        """Commands with no heredoc produce no paths."""
        assert bash_heredoc_write_paths("ls -la") == []
        assert bash_heredoc_write_paths("git status") == []
        assert bash_heredoc_write_paths("echo hello") == []

    def test_cat_plain_pipe_not_detected(self):
        """cat piped output (no heredoc) is not a heredoc write."""
        cmd = "cat /source/file.txt > /tmp/output.txt"
        # This is a plain redirect, not heredoc-fed.
        assert bash_heredoc_write_paths(cmd) == []

    def test_commit_cmd_in_heredoc_body_not_detected(self):
        """Heredoc body content with > chars doesn't produce false paths."""
        cmd = "cat > /journal/notes.md <<'EOF'\nif x > 0: print(x)\nEOF"
        # The `> 0` in the heredoc body should not be detected as a write
        assert bash_heredoc_write_paths(cmd) == ["/journal/notes.md"]


# ---------------------------------------------------------------------------
# has_git_commit_command
# ---------------------------------------------------------------------------


class TestHasGitCommitCommand:
    def test_git_commit(self):
        assert has_git_commit_command("git commit -m 'feat: add foo'") is True

    def test_git_commit_with_flags(self):
        assert has_git_commit_command("git commit -am 'fix: stuff'") is True

    def test_git_c_commit(self):
        assert has_git_commit_command("git -C /some/path commit -m 'msg'") is True

    def test_git_safe_commit(self):
        assert has_git_commit_command("git-safe-commit path1 path2 -m 'fix'") is True

    def test_git_commit_in_chain(self):
        assert has_git_commit_command("git add . && git commit -m 'chore: update'") is True

    def test_git_commit_in_if(self):
        assert has_git_commit_command("if true; then git commit -m 'x'; fi") is True

    def test_no_commit(self):
        assert has_git_commit_command("git push origin master") is False

    def test_git_log_not_commit(self):
        assert has_git_commit_command("git log --oneline -5") is False

    def test_commit_as_operand_not_detected(self):
        """`commit` as a path/grep operand is not a commit invocation.

        Regression: the tree-sitter path scanned all word args for "commit",
        so `git log commit` / `git log --grep commit` matched. The regex
        fallback anchored on the subcommand and did not.
        """
        assert has_git_commit_command("git log commit") is False
        assert has_git_commit_command("git log --grep commit") is False
        assert has_git_commit_command("git show commit") is False

    def test_git_c_value_option_commit(self):
        """`git -c key=val commit` is a commit (value-taking option before it)."""
        assert has_git_commit_command("git -c user.name=x commit -m 'm'") is True

    def test_git_diff_not_commit(self):
        assert has_git_commit_command("git diff --stat HEAD~1") is False

    def test_empty(self):
        assert has_git_commit_command("") is False

    def test_unrelated_command(self):
        assert has_git_commit_command("ls -la") is False


# ---------------------------------------------------------------------------
# has_git_push_command
# ---------------------------------------------------------------------------


class TestHasGitPushCommand:
    def test_git_push(self):
        assert has_git_push_command("git push origin master") is True

    def test_git_push_no_args(self):
        assert has_git_push_command("git push") is True

    def test_git_push_in_chain(self):
        assert has_git_push_command("git commit -m 'fix' && git push") is True

    def test_git_c_push(self):
        assert has_git_push_command("git -C /some/path push origin main") is True

    def test_no_push(self):
        assert has_git_push_command("git commit -m 'fix'") is False

    def test_git_pull_not_push(self):
        assert has_git_push_command("git pull --rebase") is False

    def test_push_as_operand_not_detected(self):
        """`push` as a grep/operand word is not a push invocation."""
        assert has_git_push_command("git log --grep push") is False

    def test_git_c_value_option_push(self):
        """`git -c key=val push` is a push."""
        assert has_git_push_command("git -c user.name=x push origin main") is True

    def test_empty(self):
        assert has_git_push_command("") is False
