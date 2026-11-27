"""Tests for scripts/print-agent-changelog.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "print-agent-changelog.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("print_agent_changelog", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_mod()

SAMPLE = """# Agent changelog

Prose, not an entry.

## Entry format

## YYYY-MM-DD - short title

- **Action:** recommended
- **Do:** this is the format example, not a real entry

## Entries

## 2026-11-28 - required migration

- **Action:** required
- **Do:** Move HOOKS_PATH to the new location.

## 2026-11-27 - optional plugin

- **Action:** optional
- **Do:** Enable gptme-example if you want the new capability.

## 2026-11-27 - Agent changelog convention

- **Action:** recommended
- **Do:** Run the printer after each pin bump.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def commit(repo: Path, path: str, content: str, message: str = "test") -> str:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)
    git(repo, "add", "--", path)
    git(
        repo,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "contrib"
    root.mkdir()
    git(root, "init", "-qb", "master")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "core.hooksPath", "/dev/null")
    return root


def test_checked_in_changelog_parses():
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    titles = [entry.title for entry in mod.parse_entries(text)]
    assert "Agent changelog convention" in titles


def test_parse_skips_prose_and_placeholder_heading():
    entries = mod.parse_entries(SAMPLE)
    assert [e.title for e in entries] == [
        "required migration",
        "optional plugin",
        "Agent changelog convention",
    ]
    assert entries[0].action == "required"
    assert "HOOKS_PATH" in entries[0].do


def test_filter_defaults_to_recommended():
    entries = mod.parse_entries(SAMPLE)
    kept = mod.filter_action(entries, "recommended")
    assert [e.action for e in kept] == ["required", "recommended"]


def test_normalize_action_strips_backticks_and_trailing_notes():
    assert mod.normalize_action("`required`") == "required"
    assert mod.normalize_action("recommended (breaking)") == "recommended"


def test_backticked_action_parses():
    text = (
        "## 2026-11-27 - hook\n\n"
        "- **Action:** `required`\n"
        "- **Do:** Install the guard.\n"
    )
    entries = mod.parse_entries(text)
    assert entries[0].action == "required"


def test_unparseable_action_is_an_error():
    text = (
        "## 2026-11-27 - hook\n\n"
        "- **Action:** requried\n"
        "- **Do:** Install the guard.\n"
    )
    with pytest.raises(RuntimeError, match="unparseable Action"):
        mod.parse_entries(text)


def test_missing_do_is_an_error():
    text = "## 2026-11-27 - hook\n\n- **Action:** required\n"
    with pytest.raises(RuntimeError, match="missing Do"):
        mod.parse_entries(text)


def test_missing_action_is_an_error():
    text = "## 2026-11-27 - hook\n\n- **Do:** Install the guard.\n"
    with pytest.raises(RuntimeError, match="missing Action"):
        mod.parse_entries(text)


def test_entries_between_old_and_new(repo: Path):
    old = commit(repo, "CHANGELOG.md", SAMPLE)
    updated = SAMPLE.replace(
        "## Entries\n",
        "## Entries\n\n## 2026-11-29 - new hook\n\n"
        "- **Action:** recommended\n"
        "- **Do:** Install the shared-checkout guard.\n\n",
    )
    new = commit(repo, "CHANGELOG.md", updated)
    added = mod.entries_between(repo, old, new)
    assert [e.title for e in added] == ["new hook"]


def test_missing_changelog_at_old_treats_all_new_entries_as_added(repo: Path):
    old = commit(repo, "README.md", "start\n")
    new = commit(repo, "CHANGELOG.md", SAMPLE)
    added = mod.filter_action(mod.entries_between(repo, old, new), "recommended")
    assert {e.action for e in added} == {"required", "recommended"}


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        ("HEAD", "no-such-rev", "CHANGELOG.md"),
        ("no-such-rev", "HEAD", "CHANGELOG.md"),
        ("HEAD", "HEAD", "TYPO.md"),
    ],
)
def test_failed_lookup_is_an_error_not_an_empty_result(
    repo: Path, old: str, new: str, path: str
):
    commit(repo, "CHANGELOG.md", SAMPLE)
    assert mod.main([old, new, "--repo", str(repo), "--path", path]) == 2


def test_malformed_new_changelog_is_an_error_not_an_empty_result(repo: Path):
    old = commit(repo, "CHANGELOG.md", SAMPLE)
    bad = SAMPLE + (
        "\n## 2026-11-30 - broken\n\n- **Action:** requried\n- **Do:** x.\n"
    )
    new = commit(repo, "CHANGELOG.md", bad)
    assert mod.main([old, new, "--repo", str(repo)]) == 2


def test_malformed_old_changelog_is_skipped(repo: Path):
    old_text = (
        "## 2026-11-26 - typo\n\n"
        "- **Action:** requried\n"
        "- **Do:** Install the guard.\n\n"
        "## 2026-11-26 - keep\n\n"
        "- **Action:** recommended\n"
        "- **Do:** Keep this.\n"
    )
    new_text = (
        "## 2026-11-27 - fixed\n\n"
        "- **Action:** required\n"
        "- **Do:** Install the guard.\n\n"
        "## 2026-11-26 - keep\n\n"
        "- **Action:** recommended\n"
        "- **Do:** Keep this.\n"
    )
    old = commit(repo, "CHANGELOG.md", old_text)
    new = commit(repo, "CHANGELOG.md", new_text)
    added = mod.entries_between(repo, old, new)
    assert [e.title for e in added] == ["fixed"]
    assert mod.main([old, new, "--repo", str(repo)]) == 0


def test_resolve_repo_defaults_to_script_checkout():
    assert mod.resolve_repo(None) == REPO_ROOT


def test_cli_json_and_text(repo: Path):
    old = commit(repo, "CHANGELOG.md", SAMPLE)
    updated = SAMPLE.replace(
        "## Entries\n",
        "## Entries\n\n## 2026-11-29 - new hook\n\n"
        "- **Action:** recommended\n"
        "- **Do:** Install the shared-checkout guard.\n\n",
    )
    new = commit(repo, "CHANGELOG.md", updated)
    json_out = subprocess.check_output(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--json", old, new],
        text=True,
    )
    rows = json.loads(json_out)
    assert rows == [
        {
            "date": "2026-11-29",
            "title": "new hook",
            "action": "recommended",
            "do": "Install the shared-checkout guard.",
        }
    ]
    text = subprocess.check_output(
        [sys.executable, str(SCRIPT), "--repo", str(repo), old, new],
        text=True,
    )
    assert "recommended  2026-11-29  new hook" in text
    empty = subprocess.check_output(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--json", new, new],
        text=True,
    )
    assert json.loads(empty) == []
