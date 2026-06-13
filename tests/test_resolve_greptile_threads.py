from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "resolve-greptile-threads.py"
)
spec = importlib.util.spec_from_file_location("resolve_greptile_threads", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
resolve_greptile_threads = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = resolve_greptile_threads
spec.loader.exec_module(resolve_greptile_threads)

parse_pr = resolve_greptile_threads.parse_pr


def test_parse_pr_hash_form() -> None:
    assert parse_pr("gptme/gptme-contrib#123", None) == ("gptme", "gptme-contrib", 123)


def test_parse_pr_two_arg_form() -> None:
    assert parse_pr("gptme/gptme-contrib", "456") == ("gptme", "gptme-contrib", 456)


def test_parse_pr_url_form() -> None:
    url = "https://github.com/gptme/gptme/pull/789"
    assert parse_pr(url, None) == ("gptme", "gptme", 789)


def test_parse_pr_url_takes_precedence_over_second_arg() -> None:
    # A URL should be parsed as-is even when a stray second arg is present.
    url = "https://github.com/owner/repo/pull/12"
    assert parse_pr(url, "99") == ("owner", "repo", 12)


def test_parse_pr_invalid_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        parse_pr("not-a-pr-spec", None)
    assert exc.value.code == 2


def test_targeting_excludes_resolved() -> None:
    # Mirrors main()'s target filter: skip resolved; honor --outdated-only.
    threads = [
        {"id": "a", "isResolved": True, "isOutdated": True, "path": "x"},
        {"id": "b", "isResolved": False, "isOutdated": False, "path": "y"},
        {"id": "c", "isResolved": False, "isOutdated": True, "path": "z"},
    ]
    all_targets = [
        t for t in threads if not t["isResolved"] and (t["isOutdated"] or not False)
    ]
    assert {t["id"] for t in all_targets} == {"b", "c"}

    outdated_targets = [
        t for t in threads if not t["isResolved"] and (t["isOutdated"] or not True)
    ]
    assert {t["id"] for t in outdated_targets} == {"c"}
