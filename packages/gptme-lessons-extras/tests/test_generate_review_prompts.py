"""generate-review-prompts.py resolves the workspace from the cwd."""

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "gptme_lessons_extras"
    / "generate-review-prompts.py"
)


def test_writes_report_into_the_cwd_workspace(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "knowledge" / "meta" / "lesson-review-prompts.md").exists()
