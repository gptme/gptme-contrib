"""Tests for PR Review Guide — difficulty estimator and prioritization."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

spec = importlib.util.spec_from_file_location(
    "pr_review_guide",
    Path(__file__).parent.parent / "scripts" / "github" / "pr-review-guide.py",
)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["pr_review_guide"] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)

classify_files = mod.classify_files
compute_loc_excluding_lockfiles = mod.compute_loc_excluding_lockfiles
estimate_review = mod.estimate_review
format_context = mod.format_context
format_estimate = mod.format_estimate
get_tracked_repos = mod.get_tracked_repos
ReviewEstimate = mod.ReviewEstimate


# --- get_tracked_repos ---


def test_tracked_repos_default():
    """Default repos are gptme org repos."""
    import os

    env = os.environ.pop("GPTME_TRACKED_REPOS", None)
    try:
        repos = get_tracked_repos()
        assert len(repos) >= 1
        assert all(r.startswith("gptme/") for r in repos)
    finally:
        if env is not None:
            os.environ["GPTME_TRACKED_REPOS"] = env


def test_tracked_repos_from_env(monkeypatch):
    """GPTME_TRACKED_REPOS env var overrides defaults."""
    monkeypatch.setenv("GPTME_TRACKED_REPOS", "foo/bar,baz/qux")
    repos = get_tracked_repos()
    assert repos == ["foo/bar", "baz/qux"]


def test_tracked_repos_env_strips_whitespace(monkeypatch):
    """Whitespace around repo names is stripped."""
    monkeypatch.setenv("GPTME_TRACKED_REPOS", " foo/bar , baz/qux ")
    repos = get_tracked_repos()
    assert repos == ["foo/bar", "baz/qux"]


def test_tracked_repos_env_empty_entries(monkeypatch):
    """Empty entries from trailing commas are filtered."""
    monkeypatch.setenv("GPTME_TRACKED_REPOS", "foo/bar,,baz/qux,")
    repos = get_tracked_repos()
    assert repos == ["foo/bar", "baz/qux"]


# --- classify_files ---


def test_classify_test_files():
    files = [
        {"path": "tests/test_foo.py"},
        {"path": "src/test_bar.py"},
        {"path": "tests/conftest.py"},
    ]
    result = classify_files(files)
    assert result["test"] == 3
    assert result["logic"] == 0


def test_classify_doc_files():
    files = [
        {"path": "README.md"},
        {"path": "docs/guide.rst"},
        {"path": "CHANGELOG.md"},
    ]
    result = classify_files(files)
    assert result["docs"] == 3
    assert result["logic"] == 0


def test_classify_config_files():
    files = [
        {"path": "pyproject.toml"},
        {"path": ".github/workflows/ci.yml"},
        {"path": "Makefile"},
    ]
    result = classify_files(files)
    assert result["config"] == 3


def test_classify_lockfiles_excluded():
    files = [
        {"path": "uv.lock"},
        {"path": "package-lock.json"},
        {"path": "src/main.py"},
    ]
    result = classify_files(files)
    assert sum(result.values()) == 1
    assert result["logic"] == 1


def test_classify_mixed_files():
    files = [
        {"path": "src/core.py"},
        {"path": "tests/test_core.py"},
        {"path": "README.md"},
        {"path": "pyproject.toml"},
    ]
    result = classify_files(files)
    assert result["logic"] == 1
    assert result["test"] == 1
    assert result["docs"] == 1
    assert result["config"] == 1


# --- compute_loc_excluding_lockfiles ---


def test_loc_excludes_lockfiles():
    pr = {
        "files": [
            {"path": "src/main.py", "additions": 50, "deletions": 10},
            {"path": "uv.lock", "additions": 500, "deletions": 200},
        ],
        "additions": 550,
        "deletions": 210,
    }
    assert compute_loc_excluding_lockfiles(pr) == 60


def test_loc_fallback_when_no_files():
    pr = {"files": [], "additions": 100, "deletions": 30}
    assert compute_loc_excluding_lockfiles(pr) == 130


def test_loc_all_lockfiles():
    pr = {
        "files": [
            {"path": "uv.lock", "additions": 500, "deletions": 200},
            {"path": "Cargo.lock", "additions": 300, "deletions": 100},
        ],
        "additions": 800,
        "deletions": 300,
    }
    assert compute_loc_excluding_lockfiles(pr) == 0


# --- estimate_review ---


def _make_pr(
    *,
    loc: int = 50,
    files: list | None = None,
    ci_green: bool = True,
    body: str = "A good description of the changes made.",
    age_days: int = 1,
):
    """Create a mock PR dict for testing."""
    from datetime import datetime, timedelta, timezone

    created = datetime.now(timezone.utc) - timedelta(days=age_days)

    if files is None:
        files = [{"path": "src/main.py", "additions": loc, "deletions": 0}]

    checks = []
    if not ci_green:
        checks = [{"name": "tests", "conclusion": "FAILURE"}]

    return {
        "repo": "gptme/gptme",
        "number": 1234,
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme/pull/1234",
        "additions": sum(f.get("additions", 0) for f in files),
        "deletions": sum(f.get("deletions", 0) for f in files),
        "changedFiles": len(files),
        "files": files,
        "createdAt": created.isoformat(),
        "body": body,
        "statusCheckRollup": checks,
        "commits": [],
    }


def test_tiny_pr_is_quick():
    pr = _make_pr(loc=10)
    est = estimate_review(pr, fetch_greptile=False)
    assert est.category == "quick"
    assert est.estimated_minutes == 2
    assert est.difficulty_score <= 18


def test_small_docs_pr_is_quick():
    pr = _make_pr(
        loc=80,
        files=[
            {"path": "README.md", "additions": 50, "deletions": 0},
            {"path": "docs/guide.md", "additions": 30, "deletions": 0},
        ],
    )
    est = estimate_review(pr, fetch_greptile=False)
    assert est.category == "quick"
    assert "docs-only" in est.positives


def test_test_heavy_pr_is_easier():
    pr = _make_pr(
        loc=200,
        files=[
            {"path": "src/feature.py", "additions": 50, "deletions": 0},
            {"path": "tests/test_feature.py", "additions": 150, "deletions": 0},
        ],
    )
    est = estimate_review(pr, fetch_greptile=False)
    assert est.category in ("quick", "normal")
    assert "mostly tests" in est.positives


def test_large_logic_heavy_pr_is_hard():
    pr = _make_pr(
        loc=500,
        files=[
            {"path": "src/core.py", "additions": 200, "deletions": 0},
            {"path": "src/handler.py", "additions": 150, "deletions": 0},
            {"path": "src/utils.py", "additions": 100, "deletions": 0},
            {"path": "src/models.py", "additions": 50, "deletions": 0},
        ],
    )
    est = estimate_review(pr, fetch_greptile=False)
    assert est.category in ("deep", "heavy")
    assert est.estimated_minutes >= 15


def test_very_large_pr_is_heavy():
    pr = _make_pr(loc=1000)
    est = estimate_review(pr, fetch_greptile=False)
    assert est.category == "heavy"
    assert est.estimated_minutes >= 25


def test_ci_failing_increases_difficulty():
    pr_green = _make_pr(loc=100, ci_green=True)
    pr_red = _make_pr(loc=100, ci_green=False)
    est_green = estimate_review(pr_green, fetch_greptile=False)
    est_red = estimate_review(pr_red, fetch_greptile=False)
    assert est_red.difficulty_score > est_green.difficulty_score
    assert not est_red.ci_green
    assert "CI failing" in est_red.factors


def test_sparse_description_increases_difficulty():
    pr_good = _make_pr(
        loc=100,
        body="A comprehensive description of all the changes made in this PR. " * 4,
    )
    pr_sparse = _make_pr(loc=100, body="fix")
    est_good = estimate_review(pr_good, fetch_greptile=False)
    est_sparse = estimate_review(pr_sparse, fetch_greptile=False)
    assert est_sparse.difficulty_score > est_good.difficulty_score


def test_old_pr_increases_difficulty():
    pr_new = _make_pr(loc=100, age_days=1)
    pr_old = _make_pr(loc=100, age_days=20)
    est_new = estimate_review(pr_new, fetch_greptile=False)
    est_old = estimate_review(pr_old, fetch_greptile=False)
    assert est_old.difficulty_score > est_new.difficulty_score
    assert est_old.age_days == 20


def test_many_files_increases_difficulty():
    single_file = _make_pr(
        loc=100,
        files=[{"path": "src/main.py", "additions": 100, "deletions": 0}],
    )
    many_files = _make_pr(
        loc=100,
        files=[
            {"path": f"src/file{i}.py", "additions": 10, "deletions": 0}
            for i in range(12)
        ],
    )
    est_single = estimate_review(single_file, fetch_greptile=False)
    est_many = estimate_review(many_files, fetch_greptile=False)
    assert est_many.difficulty_score > est_single.difficulty_score


def test_merge_conflicts_increase_difficulty():
    pr_clean = _make_pr(loc=100)
    pr_conflict = _make_pr(loc=100)
    pr_conflict["mergeable"] = "CONFLICTING"
    est_clean = estimate_review(pr_clean, fetch_greptile=False)
    est_conflict = estimate_review(pr_conflict, fetch_greptile=False)
    assert est_conflict.difficulty_score > est_clean.difficulty_score
    assert est_conflict.has_conflicts
    assert "merge conflicts" in est_conflict.factors


def test_has_tests_reduces_difficulty():
    pr_no_tests = _make_pr(
        loc=200,
        files=[
            {"path": "src/feature.py", "additions": 200, "deletions": 0},
        ],
    )
    pr_with_tests = _make_pr(
        loc=200,
        files=[
            {"path": "src/feature.py", "additions": 100, "deletions": 0},
            {"path": "tests/test_feature.py", "additions": 100, "deletions": 0},
        ],
    )
    est_no = estimate_review(pr_no_tests, fetch_greptile=False)
    est_yes = estimate_review(pr_with_tests, fetch_greptile=False)
    assert est_yes.has_tests
    assert not est_no.has_tests
    assert est_yes.difficulty_score < est_no.difficulty_score


def test_no_tests_for_logic_adds_factor():
    pr = _make_pr(
        loc=150,
        files=[
            {"path": "src/core.py", "additions": 150, "deletions": 0},
        ],
    )
    est = estimate_review(pr, fetch_greptile=False)
    assert "no tests for logic changes" in est.factors


def test_difficulty_score_clamped():
    """Difficulty score should be between 0 and 100."""
    pr = _make_pr(loc=1)
    est = estimate_review(pr, fetch_greptile=False)
    assert 0 <= est.difficulty_score <= 100

    pr = _make_pr(loc=5000, ci_green=False, body="x", age_days=30)
    est = estimate_review(pr, fetch_greptile=False)
    assert 0 <= est.difficulty_score <= 100


# --- format_context ---


def test_format_context_output():
    estimates = [
        ReviewEstimate(
            repo="gptme/gptme",
            number=123,
            title="Test PR",
            url="",
            difficulty_score=10,
            estimated_minutes=2,
            category="quick",
            loc_changed=20,
        ),
    ]
    output = format_context(estimates)
    assert "PR Review Guide" in output
    assert "gptme/gptme#123" in output
    assert "~2min" in output
    assert "1 quick reviews" in output


def test_conflict_shown_in_context_format():
    estimates = [
        ReviewEstimate(
            repo="gptme/gptme",
            number=456,
            title="Conflicting PR",
            url="",
            difficulty_score=50,
            estimated_minutes=15,
            category="deep",
            loc_changed=200,
            has_conflicts=True,
        ),
    ]
    output = format_context(estimates)
    assert "CONFLICT" in output
    assert "gptme/gptme#456" in output


# --- format_estimate ---


def _make_estimate(**kwargs: Any) -> Any:
    """Create a ReviewEstimate with sensible defaults."""
    defaults = dict(
        repo="gptme/gptme",
        number=99,
        title="My PR title",
        url="https://github.com/gptme/gptme/pull/99",
        difficulty_score=10,
        estimated_minutes=2,
        category="quick",
        loc_changed=20,
        files_changed=1,
        age_days=1,
    )
    defaults.update(kwargs)
    return ReviewEstimate(**defaults)


def test_format_estimate_quick_uses_lightning_icon():
    est = _make_estimate(category="quick", estimated_minutes=2)
    output = format_estimate(est, rank=1)
    assert "⚡" in output
    assert "[QUICK]" in output
    assert "~2min" in output


def test_format_estimate_heavy_uses_heavy_icon():
    est = _make_estimate(category="heavy", estimated_minutes=25)
    output = format_estimate(est, rank=3)
    assert "🏋️" in output
    assert "[HEAVY]" in output


def test_format_estimate_rank_appears_in_output():
    est = _make_estimate()
    assert "7." in format_estimate(est, rank=7)
    assert "42." in format_estimate(est, rank=42)


def test_format_estimate_ci_failure_shown():
    est = _make_estimate(ci_green=False)
    output = format_estimate(est, rank=1)
    assert "❌" in output


def test_format_estimate_conflicts_shown():
    est = _make_estimate(has_conflicts=True)
    output = format_estimate(est, rank=1)
    assert "conflicts" in output


def test_format_estimate_factors_listed():
    est = _make_estimate(factors=["CI failing", "no tests for logic changes"])
    output = format_estimate(est, rank=1)
    assert "CI failing" in output
    assert "no tests for logic changes" in output


def test_format_estimate_positives_listed():
    est = _make_estimate(positives=["docs-only", "mostly tests"])
    output = format_estimate(est, rank=1)
    assert "docs-only" in output
    assert "mostly tests" in output


def test_format_estimate_greptile_reviewed_clean():
    est = _make_estimate(has_greptile=True, greptile_clean=True)
    output = format_estimate(est, rank=1)
    assert output.count("✅") >= 2


def test_format_estimate_no_greptile_shows_empty():
    est = _make_estimate(has_greptile=False)
    output = format_estimate(est, rank=1)
    assert "⬜" in output


def test_format_estimate_contains_repo_and_number():
    est = _make_estimate(repo="ActivityWatch/aw-qt", number=42)
    output = format_estimate(est, rank=1)
    assert "ActivityWatch/aw-qt#42" in output


def test_format_estimate_empty_factors_and_positives():
    est = _make_estimate(factors=[], positives=[])
    lines = format_estimate(est, rank=1).splitlines()
    assert len(lines) == 3
