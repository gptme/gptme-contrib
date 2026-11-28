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
    # A GitHub pull-request run sets GITHUB_BASE_REF; the fixture repo has no
    # such remote branch, so the inherited value would hide the budget base.
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("FILE_SIZE_RATCHET_BASE", raising=False)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def write(path: str, lines: int, *, tracked: bool = True) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("x\n" * lines)
    if tracked:
        subprocess.run(["git", "add", path], check=True)
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


def test_growth_warn_reports_without_failing(repo, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    baseline({"big.py": 15})
    assert ratchet.main(["--growth=warn", write("big.py", 16)]) == 0
    out = capsys.readouterr().out
    assert "warning: big.py: grew from 15 to 16 lines" in out
    assert "::warning" not in out
    # Warning about growth does not raise the ceiling.
    assert read_baseline()["files"] == {"big.py": 15}


def test_growth_warn_annotates_on_github_actions(repo, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    baseline({"a,b%.py": 15})
    assert ratchet.main(["--growth=warn", write("a,b%.py", 16)]) == 0
    out = capsys.readouterr().out
    assert "::warning file=a%2Cb%25.py,title=File-size ratchet::grew from 15" in out


def test_growth_warn_still_fails_new_oversized_files(repo, capsys):
    baseline({"big.py": 15})
    paths = [write("big.py", 16), write("new.py", THRESHOLD)]
    assert ratchet.main(["--growth=warn", *paths]) == 1
    err = capsys.readouterr().err
    assert "new.py: 10 lines, at or over the 10-line limit" in err
    assert "big.py" not in err


def test_growth_warn_still_requires_staging_a_lowered_baseline(repo):
    baseline({"big.py": 15})
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    path = write("big.py", 12)
    assert ratchet.main(["--growth=warn", path]) == 1
    assert read_baseline()["files"] == {"big.py": 12}
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    assert ratchet.main(["--growth=warn", path]) == 0


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


@pytest.mark.parametrize("shrunk", [12, THRESHOLD - 1])
def test_shrink_fails_until_the_lowered_baseline_is_staged(repo, capsys, shrunk):
    baseline({"big.py": 15})
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    path = write("big.py", shrunk)
    assert ratchet.main([path]) == 1
    # A retry leaves the baseline file alone, but it is still not in the commit.
    assert ratchet.main([path]) == 1
    assert "staged copy is not lowered for big.py" in capsys.readouterr().err
    # Commits that do not touch the shrunk file are unaffected.
    assert ratchet.main([write("other.py", 1)]) == 0
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    assert ratchet.main([path]) == 0


@pytest.mark.parametrize("keep_file", [False, True])
def test_removal_fails_until_the_lowered_baseline_is_staged(repo, capsys, keep_file):
    path = write("gone.py", 15)
    baseline({path: 15})
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    cached = ["--cached"] if keep_file else []
    subprocess.run(["git", "rm", "-qf", *cached, path], check=True)
    # pre-commit does not pass deleted files, and a retry rewrites nothing.
    assert ratchet.main([]) == 1
    assert ratchet.main([]) == 1
    assert "staged copy is not lowered for gone.py" in capsys.readouterr().err
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    assert ratchet.main([]) == 0


def test_shrinking_under_limit_drops_entry(repo):
    baseline({"big.py": 15})
    assert ratchet.main([write("big.py", THRESHOLD - 1)]) == 0
    assert read_baseline()["files"] == {}


def test_deleted_file_entry_is_dropped(repo):
    baseline({"gone.py": 15, "big.py": 15})
    assert ratchet.main([write("big.py", 15)]) == 0
    assert read_baseline()["files"] == {"big.py": 15}


def test_sparse_checkout_missing_tracked_file_keeps_ceiling(repo):
    baseline({"big.py": 15})
    write("big.py", 15)
    subprocess.run(["git", "add", "big.py"], check=True)
    subprocess.run(["git", "update-index", "--skip-worktree", "big.py"], check=True)
    Path("big.py").unlink()
    assert ratchet.main([]) == 0
    assert read_baseline()["files"] == {"big.py": 15}


def test_index_deletion_drops_ceiling_even_with_working_copy(repo):
    baseline({"big.py": 15})
    write("big.py", 15)
    subprocess.run(["git", "rm", "--cached", "-f", "big.py"], check=True)
    assert Path("big.py").exists()
    assert ratchet.main([]) == 0
    assert read_baseline()["files"] == {}
    assert ratchet.main([write("big.py", 14)]) == 1


def test_unreadable_file_does_not_lower_ceiling(repo, monkeypatch, capsys):
    baseline({"big.py": 15})
    write("big.py", 15)
    original = Path.read_bytes

    def read_bytes(path):
        if path.name == "big.py":
            raise PermissionError("cannot read big.py")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert ratchet.main(["big.py"]) == 1
    assert read_baseline()["files"] == {"big.py": 15}
    assert "cannot read big.py" in capsys.readouterr().err


def test_hook_registrations_serialize_and_run_deletion_cleanup():
    import yaml

    shared = yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text())
    local = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [*shared, *(h for repo in local["repos"] for h in repo["hooks"])]
    ratchets = [h for h in hooks if h["id"] == "file-size-ratchet"]
    assert len(ratchets) == 2
    for hook in ratchets:
        assert hook.get("require_serial") is True
        assert hook.get("always_run") is True


def test_non_code_and_excluded_files_are_ignored(repo):
    baseline({}, exclude=["gen/*.py"])
    paths = [
        write("notes.md", 50),
        write("data.json", 50),
        write("gen/schema.py", 50),
        write("web/vendor/lib.js", 50),
        write("app.min.js", 50),
        write("generated/schema.py", 50),
        write("src/generated/schema.py", 50),
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
    write("untracked.py", 50, tracked=False)
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


def commit(message: str = "c") -> None:
    # The fixture repo must not run the developer's global hooks or signing.
    config = ["user.name=t", "user.email=t@t", "core.hooksPath=/dev/null"]
    config.append("commit.gpgsign=false")
    flags = [arg for item in config for arg in ("-c", item)]
    subprocess.run(["git", *flags, "commit", "-qm", message], check=True)


BUDGET = ["--growth=budget", "--budget=3", "--hard-stop=5", "--hard-stop-size=30"]


def test_budget_growth_within_budget_warns(repo, capsys):
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    assert ratchet.main([*BUDGET, write("big.py", 18)]) == 0
    assert "grew by 3 lines (from 15 to 18)" in capsys.readouterr().out
    assert read_baseline()["files"] == {"big.py": 15}


def test_budget_growth_over_budget_fails(repo, capsys):
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    assert ratchet.main([*BUDGET, write("big.py", 19)]) == 1
    assert "over the 3-line budget" in capsys.readouterr().err


def test_budget_allows_more_growth_when_a_new_module_is_added(repo):
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    write("helpers.py", 6)  # at least the 4 lines big.py grows by
    assert ratchet.main([*BUDGET, write("big.py", 19)]) == 0


def test_budget_rejects_growth_larger_than_the_new_module(repo, capsys):
    # A token new file must not exempt an arbitrarily large growth.
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    write("helpers.py", 2)
    assert ratchet.main([*BUDGET, write("big.py", 19)]) == 1
    assert "new files in this change add only 2 lines" in capsys.readouterr().err


def test_budget_hard_stop_ignores_the_new_module_exception(repo, capsys):
    baseline({"huge.py": 30})
    write("huge.py", 30)
    commit()
    write("helpers.py", 2)
    assert ratchet.main([*BUDGET, write("huge.py", 36)]) == 1
    assert "may grow by at most 5 lines" in capsys.readouterr().err


def test_budget_measures_growth_from_the_base_not_the_baseline(repo, capsys):
    # An earlier change grew big.py to 20 without raising the baseline.
    baseline({"big.py": 15})
    write("big.py", 20)
    commit()
    assert ratchet.main([*BUDGET, write("big.py", 21)]) == 0
    assert "grew by 1 lines (from 20 to 21)" in capsys.readouterr().out
    assert ratchet.main([*BUDGET, write("big.py", 20)]) == 0
    assert "grew" not in capsys.readouterr().out


def test_budget_uses_the_merge_base_of_base_ref(repo):
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    subprocess.run(["git", "branch", "-q", "trunk"], check=True)
    # Two commits on the branch, each within budget, add up to more than it.
    write("big.py", 17)
    commit()
    path = write("big.py", 19)
    assert ratchet.main([*BUDGET, path]) == 0  # vs HEAD: +2
    assert ratchet.main([*BUDGET, "--base=trunk", path]) == 1  # vs merge base: +4


def test_budget_reads_github_base_ref(repo, monkeypatch):
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/trunk", "HEAD"], check=True
    )
    write("big.py", 17)
    commit()
    path = write("big.py", 19)
    monkeypatch.setenv("GITHUB_BASE_REF", "trunk")
    assert ratchet.main([*BUDGET, path]) == 1


def test_budget_without_a_resolvable_base_only_warns(repo, capsys):
    baseline({"big.py": 15})  # no commits: HEAD does not resolve
    assert ratchet.main([*BUDGET, write("big.py", 40)]) == 0
    assert "cannot resolve the growth base" in capsys.readouterr().out


def test_budget_still_fails_new_oversized_files(repo, capsys):
    baseline({})
    commit_path = write("seed.py", 1)
    commit()
    assert ratchet.main([*BUDGET, commit_path, write("new.py", THRESHOLD)]) == 1
    assert "new.py: 10 lines" in capsys.readouterr().err


def test_growth_off_keeps_only_the_new_file_rule(repo, capsys):
    baseline({"big.py": 15})
    subprocess.run(["git", "add", ratchet.DEFAULT_BASELINE], check=True)
    # Growth passes, and a shrink needs no baseline edit.
    assert ratchet.main(["--growth=off", write("big.py", 40)]) == 0
    assert ratchet.main(["--growth=off", write("big.py", 12)]) == 0
    assert ratchet.main(["--growth=off", write("big.py", 5)]) == 0
    assert read_baseline()["files"] == {"big.py": 15}
    assert ratchet.main(["--growth=off", write("new.py", THRESHOLD)]) == 1
    assert "new.py: 10 lines" in capsys.readouterr().err


def test_budget_base_env_measures_a_pr_merge_commit(repo, monkeypatch):
    # CI checks out the PR merge commit; its first parent is the base tip.
    baseline({"big.py": 15})
    write("big.py", 15)
    commit()
    write("big.py", 19)
    commit()
    path = write("big.py", 19)
    assert ratchet.main([*BUDGET, path]) == 0  # vs HEAD: no growth
    monkeypatch.setenv("FILE_SIZE_RATCHET_BASE", "HEAD^1")
    assert ratchet.main([*BUDGET, path]) == 1  # vs the base tip: +4
