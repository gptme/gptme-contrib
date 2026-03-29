"""Tests for token_storage module — atomic batch saves."""

from pathlib import Path

from gptmail.communication_utils.auth.token_storage import (
    save_token_to_env,
    save_tokens_to_env,
)


def test_save_tokens_to_env_creates_file(tmp_path: Path) -> None:
    """save_tokens_to_env creates .env with all tokens in one write."""
    env_path = tmp_path / ".env"
    result = save_tokens_to_env(
        {"TOKEN_A": "val_a", "TOKEN_B": "val_b"},
        env_path=env_path,
        comment="test tokens",
    )
    assert result is True
    content = env_path.read_text()
    assert "TOKEN_A=val_a" in content
    assert "TOKEN_B=val_b" in content


def test_save_tokens_to_env_updates_existing(tmp_path: Path) -> None:
    """save_tokens_to_env updates existing keys in-place."""
    env_path = tmp_path / ".env"
    env_path.write_text("TOKEN_A=old_a\nTOKEN_B=old_b\n")
    result = save_tokens_to_env(
        {"TOKEN_A": "new_a", "TOKEN_B": "new_b"},
        env_path=env_path,
    )
    assert result is True
    content = env_path.read_text()
    assert "TOKEN_A=new_a" in content
    assert "TOKEN_B=new_b" in content
    assert "old_a" not in content
    assert "old_b" not in content


def test_save_tokens_to_env_preserves_other_lines(tmp_path: Path) -> None:
    """save_tokens_to_env doesn't clobber unrelated env vars."""
    env_path = tmp_path / ".env"
    env_path.write_text("UNRELATED=keep_me\nTOKEN_A=old\n")
    save_tokens_to_env({"TOKEN_A": "new"}, env_path=env_path)
    content = env_path.read_text()
    assert "UNRELATED=keep_me" in content
    assert "TOKEN_A=new" in content


def test_save_tokens_to_env_mixed_update_and_append(tmp_path: Path) -> None:
    """Some tokens exist (updated), some are new (appended)."""
    env_path = tmp_path / ".env"
    env_path.write_text("TOKEN_A=old_a\n")
    save_tokens_to_env(
        {"TOKEN_A": "new_a", "TOKEN_B": "new_b"},
        env_path=env_path,
    )
    content = env_path.read_text()
    assert "TOKEN_A=new_a" in content
    assert "TOKEN_B=new_b" in content


def test_save_tokens_to_env_atomic_vs_separate(tmp_path: Path) -> None:
    """Atomic batch save produces same result as separate saves."""
    # Batch save
    env_batch = tmp_path / "batch.env"
    env_batch.write_text("X=1\n")
    save_tokens_to_env({"A": "1", "B": "2", "C": "3"}, env_path=env_batch)

    # Separate saves
    env_sep = tmp_path / "separate.env"
    env_sep.write_text("X=1\n")
    save_token_to_env("A", "1", env_path=env_sep)
    save_token_to_env("B", "2", env_path=env_sep)
    save_token_to_env("C", "3", env_path=env_sep)

    # Both should have the same keys (order may differ)
    batch_keys = {
        line.split("=")[0]
        for line in env_batch.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    sep_keys = {
        line.split("=")[0]
        for line in env_sep.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert batch_keys == sep_keys


def test_save_tokens_empty_dict(tmp_path: Path) -> None:
    """Empty dict is a no-op (file unchanged)."""
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=me\n")
    save_tokens_to_env({}, env_path=env_path)
    assert env_path.read_text() == "KEEP=me\n"
