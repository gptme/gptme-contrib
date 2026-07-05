"""Tests for the submodule commit validation hook."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "validate_submodule_commits",
    Path(__file__).parent.parent
    / "scripts"
    / "precommit"
    / "validate_submodule_commits.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

check_commit_exists_upstream = _MODULE.check_commit_exists_upstream
get_submodules = _MODULE.get_submodules
main = _MODULE.main


def test_get_submodules_parses_paths_and_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gitmodules = tmp_path / ".gitmodules"
    gitmodules.write_text(
        """
[submodule "gptme-contrib"]
    path = gptme-contrib
    url = https://github.com/gptme/gptme-contrib.git
[submodule "missing-url"]
    path = no-url
"""
    )

    monkeypatch.chdir(tmp_path)

    assert get_submodules() == {
        "gptme-contrib": "https://github.com/gptme/gptme-contrib.git"
    }


def test_check_commit_exists_upstream_fetches_configured_url(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_MODULE.subprocess, "run", fake_run)

    assert check_commit_exists_upstream(
        "gptme-contrib",
        "abc123",
        "https://github.com/gptme/gptme-contrib.git",
    )
    assert calls == [
        [
            "git",
            "-C",
            "gptme-contrib",
            "fetch",
            "--depth=1",
            "https://github.com/gptme/gptme-contrib.git",
            "abc123",
        ]
    ]


def test_check_commit_exists_upstream_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(cmd: list[str], **kwargs):
        raise _MODULE.subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(_MODULE.subprocess, "run", fake_run)

    assert not check_commit_exists_upstream(
        "gptme-contrib", "abc123", "https://example.com/repo.git"
    )


def test_main_reports_missing_commit_with_upstream_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        _MODULE,
        "get_submodules",
        lambda: {"submodule-a": "https://example.com/repo.git"},
    )
    monkeypatch.setattr(
        _MODULE, "get_staged_submodule_sha", lambda _path: "1234567890abcdef"
    )
    monkeypatch.setattr(_MODULE, "check_commit_exists_upstream", lambda *_args: False)

    assert main() == 1
    captured = capsys.readouterr()
    assert "https://example.com/repo.git" in captured.out
    assert "1234567890ab" in captured.out
