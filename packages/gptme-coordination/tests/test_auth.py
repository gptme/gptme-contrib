"""Tests for HMAC auth and secret resolution."""

from pathlib import Path

from gptme_coordination.auth import resolve_secret, resolve_secrets_dir


def test_resolve_secret_prefers_env_var(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "agent-a.secret").write_text("file-secret\n")

    secret = resolve_secret(
        "agent-a",
        secrets_dir=secrets_dir,
        env={"COORDINATION_SECRET_AGENT_A": "env-secret"},
    )

    assert secret == b"env-secret"


def test_resolve_secret_uses_coordination_secrets_dir_env(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "custom-secrets"
    secrets_dir.mkdir()
    (secrets_dir / "agent-a.secret").write_text("file-secret\n")

    outside_repo = tmp_path / "outside"
    outside_repo.mkdir()

    secret = resolve_secret(
        "agent-a",
        cwd=outside_repo,
        env={"COORDINATION_SECRETS_DIR": str(secrets_dir)},
    )

    assert secret == b"file-secret"


def test_resolve_secrets_dir_uses_nearest_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "packages" / "agent"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert resolve_secrets_dir(cwd=nested, env={}) == repo / "secrets" / "coordination"
