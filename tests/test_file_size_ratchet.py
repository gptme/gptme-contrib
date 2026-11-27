"""Tests for the file-size ratchet pre-commit hook."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit" / "validators"))

import file_size_ratchet as ratchet  # noqa: E402

THRESHOLD = 10


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def write(path: str, lines: int) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("x\n" * lines)
    return path


def baseline(files: dict[str, int], **extra) -> dict:
    data = {"threshold": THRESHOLD, "exclude": [], "files": files, **extra}
    Path(ratchet.DEFAULT_BASELINE).write_text(json.dumps(data))
    return data


def read_baseline() -> dict:
    data: dict = json.loads(Path(ratchet.DEFAULT_BASELINE).read_text())
    return data


def test_new_file_under_limit_passes(repo):
    baseline({})
    assert ratchet.main([write("small.py", THRESHOLD - 1)]) == 0


def test_new_file_over_limit_fails(repo, capsys):
    baseline({})
    assert ratchet.main([write("big.py", THRESHOLD)]) == 1
    assert "big.py: 10 lines, at or over the 10-line limit" in capsys.readouterr().err


def test_listed_file_growing_fails(repo, capsys):
    baseline({"big.py": 15})
    assert ratchet.main([write("big.py", 16)]) == 1
    assert "grew from 15 to 16" in capsys.readouterr().err
    assert read_baseline()["files"] == {"big.py": 15}


def test_listed_file_at_size_passes_unchanged(repo):
    baseline({"big.py": 15})
    assert ratchet.main([write("big.py", 15)]) == 0
    assert read_baseline()["files"] == {"big.py": 15}


def test_shrinking_lowers_baseline_and_never_raises_it(repo):
    baseline({"big.py": 15})
    assert ratchet.main([write("big.py", 12)]) == 0
    assert read_baseline()["files"] == {"big.py": 12}
    # Growing back to the old size now fails: the ratchet only turns one way.
    assert ratchet.main([write("big.py", 15)]) == 1
    assert read_baseline()["files"] == {"big.py": 12}


def test_shrinking_under_limit_drops_entry(repo):
    baseline({"big.py": 15})
    assert ratchet.main([write("big.py", THRESHOLD - 1)]) == 0
    assert read_baseline()["files"] == {}


def test_deleted_file_entry_is_dropped(repo):
    baseline({"gone.py": 15, "big.py": 15})
    assert ratchet.main([write("big.py", 15)]) == 0
    assert read_baseline()["files"] == {"big.py": 15}


def test_non_code_and_excluded_files_are_ignored(repo):
    baseline({}, exclude=["gen/*.py"])
    paths = [
        write("notes.md", 50),
        write("data.json", 50),
        write("gen/schema.py", 50),
        write("web/vendor/lib.js", 50),
        write("app.min.js", 50),
    ]
    assert ratchet.main(paths) == 0


def test_tests_are_ratcheted_like_code(repo):
    baseline({})
    assert ratchet.main([write("tests/test_huge.py", THRESHOLD)]) == 1


def test_no_baseline_uses_default_threshold(repo):
    assert ratchet.main(["--threshold", "10", write("big.py", 10)]) == 1
    assert ratchet.main(["--threshold", "10", write("ok.py", 9)]) == 0
    assert not Path(ratchet.DEFAULT_BASELINE).exists()


def test_init_records_tracked_oversized_files(repo):
    write("big.py", 12)
    write("small.py", 3)
    write("notes.md", 50)
    write("untracked.py", 50)
    subprocess.run(["git", "add", "big.py", "small.py", "notes.md"], check=True)
    assert ratchet.main(["--init", "--threshold", "10"]) == 0
    data = read_baseline()
    assert data["threshold"] == 10
    assert data["files"] == {"big.py": 12}
    # A second --init must not silently reset ceilings.
    assert ratchet.main(["--init"]) == 1


def test_malformed_baseline_fails(repo, capsys):
    Path(ratchet.DEFAULT_BASELINE).write_text('{"files": []}')
    assert ratchet.main([write("big.py", 1)]) == 1
    assert "expected an object" in capsys.readouterr().err


def test_line_count_matches_codesize_rule(repo):
    Path("a.py").write_text("one\ntwo")  # no trailing newline still counts
    Path("b.py").write_bytes(b"\x00binary\n")
    assert ratchet.count_lines(Path("a.py")) == 2
    assert ratchet.count_lines(Path("b.py")) == 0
