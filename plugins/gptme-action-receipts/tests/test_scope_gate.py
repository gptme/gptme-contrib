"""Tests for the Phase 2 scope-check gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from gptme_action_receipts.hooks.scope_gate import (
    _extract_git_push_remote,
    _is_authorized,
    _load_scope_config,
    _parse_repo_from_url,
    check_scope,
    check_scope_decision,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def scope_yaml(tmp_path: Path):
    """Write a scope.yaml to tmp_path and point GPTME_SCOPE_MANIFEST at it."""
    path = tmp_path / "scope.yaml"

    def _write(content: dict) -> Path:
        path.write_text(yaml.dump(content), encoding="utf-8")
        return path

    return _write


@pytest.fixture()
def authed_merge_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """scope.yaml that authorizes ErikBjare/bob merges only."""
    path = tmp_path / "scope.yaml"
    path.write_text(
        yaml.dump(
            {
                "version": 1,
                "violation_action": "warn",
                "scopes": {"merge_repos": ["ErikBjare/bob"]},
            }
        )
    )
    monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))
    return path


# --------------------------------------------------------------------------- #
# _is_authorized                                                               #
# --------------------------------------------------------------------------- #


class TestIsAuthorized:
    def test_exact_match(self):
        assert _is_authorized("ErikBjare/bob", ["ErikBjare/bob"])

    def test_glob_org_wildcard(self):
        assert _is_authorized("ErikBjare/bob", ["ErikBjare/*"])

    def test_not_in_list(self):
        assert not _is_authorized("gptme/gptme-contrib", ["ErikBjare/bob"])

    def test_empty_list_denies(self):
        assert not _is_authorized("anything/repo", [])

    def test_partial_match_not_enough(self):
        assert not _is_authorized("gptme/gptme", ["ErikBjare/*"])


# --------------------------------------------------------------------------- #
# _parse_repo_from_url                                                         #
# --------------------------------------------------------------------------- #


class TestParseRepoFromUrl:
    def test_ssh_url(self):
        assert (
            _parse_repo_from_url("git@github.com:ErikBjare/bob.git") == "ErikBjare/bob"
        )

    def test_https_url(self):
        assert (
            _parse_repo_from_url("https://github.com/ErikBjare/bob.git")
            == "ErikBjare/bob"
        )

    def test_https_no_dot_git(self):
        assert (
            _parse_repo_from_url("https://github.com/gptme/gptme-contrib")
            == "gptme/gptme-contrib"
        )

    def test_returns_none_on_garbage(self):
        assert _parse_repo_from_url("not-a-url") is None


# --------------------------------------------------------------------------- #
# check_scope — authorized actions                                             #
# --------------------------------------------------------------------------- #


class TestCheckScopeAuthorized:
    def test_authorized_merge_allowed(self, authed_merge_yaml: Path):
        violation = check_scope(
            "shell",
            "gh pr merge 42 --squash --repo ErikBjare/bob",
            None,
        )
        assert violation is None

    def test_non_sensitive_command_passes_through(self, authed_merge_yaml: Path):
        for cmd in ("ls -la", "echo hello", "uv run pytest", "git status"):
            assert check_scope("shell", cmd, None) is None

    def test_non_shell_tool_skipped(self, authed_merge_yaml: Path):
        # save/patch tools are never scope-checked
        assert (
            check_scope(
                "save", "gh pr merge 1175 --squash --repo gptme/gptme-contrib", None
            )
            is None
        )

    def test_glob_org_authorizes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "scope.yaml"
        path.write_text(yaml.dump({"scopes": {"merge_repos": ["ErikBjare/*"]}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))
        assert check_scope("shell", "gh pr merge 7 --repo ErikBjare/bob", None) is None


# --------------------------------------------------------------------------- #
# check_scope — unauthorized actions                                           #
# --------------------------------------------------------------------------- #


class TestCheckScopeUnauthorized:
    def test_unauthorized_merge_flagged(self, authed_merge_yaml: Path):
        violation = check_scope(
            "shell",
            "gh pr merge 1175 --squash --repo gptme/gptme-contrib",
            None,
        )
        assert violation is not None
        assert "gptme/gptme-contrib" in violation

    def test_force_push_flagged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "scope.yaml"
        path.write_text(yaml.dump({"scopes": {"force_push_repos": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))
        # git push --force resolves the repo via workspace remote
        fake_url = "https://github.com/gptme/gptme-contrib.git"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_url, stderr=""
            )
            violation = check_scope(
                "shell",
                "git push origin HEAD:refs/heads/main --force",
                tmp_path,
            )
        assert violation is not None
        assert "gptme-contrib" in violation

    @pytest.mark.parametrize(
        ("command", "expected_remote"),
        [
            ("git push -f origin HEAD", "origin"),
            ("git push origin HEAD -f", "origin"),
            ("git push --force origin HEAD", "origin"),
            ("git push --force-with-lease origin HEAD", "origin"),
        ],
    )
    def test_force_push_flag_variants_flagged(
        self,
        command: str,
        expected_remote: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        path = tmp_path / "scope.yaml"
        path.write_text(yaml.dump({"scopes": {"force_push_repos": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))
        fake_url = "https://github.com/gptme/gptme-contrib.git"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_url, stderr=""
            )
            violation = check_scope("shell", command, tmp_path)

        assert violation is not None
        assert "gptme-contrib" in violation
        assert mock_run.call_args.args[0] == [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "get-url",
            expected_remote,
        ]

    def test_repo_delete_flagged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "scope.yaml"
        path.write_text(yaml.dump({"scopes": {"repo_delete": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))
        violation = check_scope("shell", "gh repo delete gptme/old-repo --yes", None)
        assert violation is not None
        assert "old-repo" in violation


# --------------------------------------------------------------------------- #
# check_scope — workspace remote fallback                                      #
# --------------------------------------------------------------------------- #


class TestCheckScopeWorkspaceFallback:
    def test_uses_workspace_remote_when_no_repo_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Without --repo flag, extraction falls back to workspace git remote."""
        scope = tmp_path / "scope.yaml"
        scope.write_text(yaml.dump({"scopes": {"merge_repos": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(scope))

        # Fake git remote returning gptme-contrib URL
        fake_remote = "https://github.com/gptme/gptme-contrib.git"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_remote, stderr=""
            )
            violation = check_scope("shell", "gh pr merge 42 --squash", tmp_path)

        assert violation is not None
        assert "gptme-contrib" in violation

    def test_no_workspace_skips_check_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Without workspace and no --repo flag, no violation (can't extract)."""
        scope = tmp_path / "scope.yaml"
        scope.write_text(yaml.dump({"scopes": {"merge_repos": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(scope))

        # No --repo flag, no workspace → extraction returns None → skip check
        violation = check_scope("shell", "gh pr merge 42 --squash", None)
        assert violation is None


# --------------------------------------------------------------------------- #
# check_scope — failure resilience                                             #
# --------------------------------------------------------------------------- #


class TestCheckScopeResilience:
    def test_bad_manifest_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bad = tmp_path / "scope.yaml"
        bad.write_text(": this is : not : valid yaml :\n  - [unclosed")
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(bad))
        # Falls back to defaults (warn, all empty) — no crash, returns None
        result = check_scope("shell", "gh pr merge 1 --repo ErikBjare/bob", None)
        # Default allows no merges (empty list) → violation expected
        assert isinstance(result, str | type(None))  # no exception

    def test_missing_manifest_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(tmp_path / "nonexistent.yaml"))
        # Default config → empty merge_repos → violation
        violation = check_scope(
            "shell", "gh pr merge 1175 --squash --repo gptme/gptme-contrib", None
        )
        assert violation is not None  # defaults deny

    def test_default_config_scopes_are_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(tmp_path / "nonexistent.yaml"))

        cfg = _load_scope_config()
        cfg["scopes"]["merge_repos"].append("mutated/repo")

        assert _load_scope_config()["scopes"]["merge_repos"] == []

    def test_scope_decision_reuses_detection_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "scope.yaml"
        path.write_text(
            yaml.dump(
                {
                    "violation_action": "block",
                    "scopes": {"merge_repos": ["ErikBjare/bob"]},
                }
            )
        )
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(path))

        decision = check_scope_decision(
            "shell",
            "gh pr merge 1175 --squash --repo gptme/gptme-contrib",
            None,
        )

        assert decision.violation is not None
        assert decision.action == "block"

    def test_subprocess_error_during_extraction_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        scope = tmp_path / "scope.yaml"
        scope.write_text(yaml.dump({"scopes": {"merge_repos": []}}))
        monkeypatch.setenv("GPTME_SCOPE_MANIFEST", str(scope))

        with patch("subprocess.run", side_effect=OSError("no git")):
            # Should not crash — falls back gracefully
            result = check_scope("shell", "gh pr merge 42 --squash", tmp_path)
        # No --repo + subprocess failure → repo extraction fails → no violation
        assert result is None


class TestExtractGitPushRemote:
    @pytest.mark.parametrize(
        ("command", "remote"),
        [
            ("git push origin HEAD --force", "origin"),
            ("git push --force origin HEAD", "origin"),
            ("git push -f origin HEAD", "origin"),
            ("git push --force-with-lease=main origin HEAD", "origin"),
            ("git push -o ci.skip origin HEAD -f", "origin"),
        ],
    )
    def test_skips_flags_before_remote(self, command: str, remote: str):
        assert _extract_git_push_remote(command) == remote
