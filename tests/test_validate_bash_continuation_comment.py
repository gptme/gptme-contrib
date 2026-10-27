"""Tests for scripts/precommit/validators/validate_bash_continuation_comment.py

Regression coverage for ErikBjare/bob#1409: a comment line between
backslash continuations ended the `env` command, which then printed the
whole environment.
"""

import subprocess
import sys
from pathlib import Path

VALIDATOR = (
    Path(__file__).resolve().parents[1]
    / "scripts/precommit/validators/validate_bash_continuation_comment.py"
)
sys.path.insert(0, str(VALIDATOR.parent))

from validate_bash_continuation_comment import find_violations  # noqa: E402


def _check(src: str) -> list[tuple[int, int]]:
    return find_violations(src.splitlines())


def test_1409_shape_is_flagged():
    src = """\
OUT=$(env -u FOO \\
    BAR=1 \\
    # this comment ends the env command
    timeout 900 bash run.sh 2>&1)
"""
    assert _check(src) == [(3, 2)]


def test_comment_above_command_is_clean():
    src = """\
# explain the command up here
OUT=$(env -u FOO \\
    BAR=1 \\
    timeout 900 bash run.sh 2>&1)
"""
    assert _check(src) == []


def test_backslash_inside_trailing_comment_does_not_continue():
    src = """\
echo hi  # a path like C:\\
# next comment
"""
    assert _check(src) == []


def test_escaped_backslash_does_not_continue():
    src = """\
printf '%s\\n' a\\\\
# separate comment
"""
    assert _check(src) == []


def test_hash_inside_string_is_not_a_comment():
    src = """\
echo "issue #1409" \\
    done
"""
    assert _check(src) == []


def test_hash_line_inside_multiline_string_is_content():
    src = """\
PROMPT="first line \\
# not a comment, string content
"
"""
    assert _check(src) == []


def test_heredoc_body_is_skipped():
    src = """\
cat <<'EOF'
some text \\
# markdown heading
EOF
cat <<-EOF
\tline \\
\t# heading
\tEOF
echo after \\
    # real violation after the heredocs
"""
    assert _check(src) == [(10, 9)]


def test_here_string_is_not_a_heredoc():
    src = """\
grep x <<< EOF
cmd \\
    # flagged because no heredoc was opened
"""
    assert _check(src) == [(3, 2)]


def test_quoted_or_arithmetic_shift_does_not_open_heredoc():
    src = """\
echo "use <<EOF in docs"
x=$(( 1 << N ))
cmd \\
    # still flagged: no heredoc swallowed the rest of the file
"""
    assert _check(src) == [(4, 3)]


def test_cli_exit_codes(tmp_path: Path):
    bad = tmp_path / "bad.sh"
    bad.write_text("env -u X \\\n    # oops\n    true\n")
    good = tmp_path / "good.sh"
    good.write_text("env -u X \\\n    true\n")

    res = subprocess.run(
        [sys.executable, str(VALIDATOR), str(good), str(bad)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert f"{bad}:2:" in res.stdout
    assert str(good) not in res.stdout

    res = subprocess.run(
        [sys.executable, str(VALIDATOR), str(good)], capture_output=True, text=True
    )
    assert res.returncode == 0
