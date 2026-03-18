"""Tests for user memory extraction logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_user_memories.extractor import (
    _get_anthropic_api_key,
    get_cc_user_messages,
    get_user_messages,
    is_autonomous_session,
    is_cc_autonomous_session,
    load_existing_memories,
    merge_facts,
    run_batch,
    save_memories,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, messages: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")


def _make_gptme_conv(tmp_path: Path, messages: list[dict]) -> Path:
    log_dir = tmp_path / "conv1"
    log_dir.mkdir()
    conv_file = log_dir / "conversation.jsonl"
    _write_jsonl(conv_file, messages)
    return conv_file


# ---------------------------------------------------------------------------
# is_autonomous_session
# ---------------------------------------------------------------------------


class TestIsAutonomousSession:
    def test_personal_session_returns_false(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "user", "content": "How do I use Python decorators?"}],
        )
        assert not is_autonomous_session(conv)

    def test_autonomous_pattern_returns_true(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [
                {
                    "role": "system",
                    "content": "You are starting an autonomous work session.",
                }
            ],
        )
        assert is_autonomous_session(conv)

    def test_explicit_bob_pattern(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "system", "content": "You are Bob, an autonomous agent."}],
        )
        assert is_autonomous_session(conv)

    def test_autonomous_keyword_case_insensitive(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "system", "content": "Running in AUTONOMOUS mode."}],
        )
        assert is_autonomous_session(conv)

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert not is_autonomous_session(tmp_path / "nonexistent.jsonl")

    def test_malformed_json_is_skipped(self, tmp_path: Path) -> None:
        conv_file = tmp_path / "bad.jsonl"
        conv_file.write_text("not valid json\n")
        assert not is_autonomous_session(conv_file)


# ---------------------------------------------------------------------------
# get_user_messages
# ---------------------------------------------------------------------------


class TestGetUserMessages:
    def test_extracts_user_messages(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [
                {"role": "user", "content": "Hello world, this is a user message."},
                {"role": "assistant", "content": "I am the assistant."},
                {"role": "user", "content": "Second user message here."},
            ],
        )
        result = get_user_messages(conv)
        assert "Hello world" in result
        assert "Second user message" in result
        assert "assistant" not in result

    def test_skips_short_messages(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "user", "content": "ok"}],
        )
        result = get_user_messages(conv)
        assert result == ""

    def test_structured_content_extracted(self, tmp_path: Path) -> None:
        conv = _make_gptme_conv(
            tmp_path,
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "A structured user message."}],
                }
            ],
        )
        result = get_user_messages(conv)
        assert "structured user message" in result

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = get_user_messages(tmp_path / "nonexistent.jsonl")
        assert result == ""

    def test_truncates_long_messages(self, tmp_path: Path) -> None:
        long_content = "x" * 5000
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "user", "content": long_content}],
        )
        result = get_user_messages(conv)
        assert len(result) <= 8000  # MAX_CONV_CHARS


# ---------------------------------------------------------------------------
# get_cc_user_messages
# ---------------------------------------------------------------------------


def _make_cc_jsonl(tmp_path: Path, messages: list[dict]) -> Path:
    cc_file = tmp_path / "session.jsonl"
    _write_jsonl(cc_file, messages)
    return cc_file


class TestGetCCUserMessages:
    def test_extracts_cc_user_messages(self, tmp_path: Path) -> None:
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "This is a CC user message.",
                    },
                }
            ],
        )
        result = get_cc_user_messages(cc_file)
        assert "CC user message" in result

    def test_skips_tool_results(self, tmp_path: Path) -> None:
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "ok",
                            }
                        ],
                    },
                }
            ],
        )
        result = get_cc_user_messages(cc_file)
        assert result == ""

    def test_mixed_content_extracts_text(self, tmp_path: Path) -> None:
        """Mixed tool_result + text content: text parts should be extracted."""
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "ok",
                            },
                            {
                                "type": "text",
                                "text": "Thanks, that worked great!",
                            },
                        ],
                    },
                }
            ],
        )
        result = get_cc_user_messages(cc_file)
        assert "Thanks, that worked great!" in result

    def test_skips_non_user_type(self, tmp_path: Path) -> None:
        cc_file = _make_cc_jsonl(
            tmp_path,
            [{"type": "assistant", "message": {"role": "assistant", "content": "Hi!"}}],
        )
        result = get_cc_user_messages(cc_file)
        assert result == ""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = get_cc_user_messages(tmp_path / "nonexistent.jsonl")
        assert result == ""


# ---------------------------------------------------------------------------
# is_cc_autonomous_session
# ---------------------------------------------------------------------------


class TestIsCCAutonomousSession:
    def test_personal_session_returns_false(self, tmp_path: Path) -> None:
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "How do I use async/await?"},
                }
            ],
        )
        assert not is_cc_autonomous_session(cc_file)

    def test_autonomous_pattern_returns_true(self, tmp_path: Path) -> None:
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "You are starting an autonomous work session.",
                    },
                }
            ],
        )
        assert is_cc_autonomous_session(cc_file)


# ---------------------------------------------------------------------------
# merge_facts
# ---------------------------------------------------------------------------


class TestMergeFacts:
    def test_deduplicates_by_normalized_form(self) -> None:
        existing = ["Uses Python for scripting"]
        new_facts = ["uses python for scripting"]
        result = merge_facts(existing, new_facts)
        assert result == existing

    def test_appends_new_facts(self) -> None:
        existing = ["Uses Vim editor"]
        new_facts = ["Prefers dark mode themes"]
        result = merge_facts(existing, new_facts)
        assert len(result) == 2
        assert "Prefers dark mode themes" in result

    def test_empty_inputs(self) -> None:
        assert merge_facts([], []) == []
        assert merge_facts([], ["fact one"]) == ["fact one"]
        assert merge_facts(["fact one"], []) == ["fact one"]


# ---------------------------------------------------------------------------
# load_existing_memories / save_memories
# ---------------------------------------------------------------------------


class TestMemoriesFile:
    def test_load_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        result = load_existing_memories(tmp_path / "memories.md")
        assert result == []

    def test_save_and_reload(self, tmp_path: Path) -> None:
        memories_file = tmp_path / "memories.md"
        facts = ["Uses Python", "Works at Superuser Labs"]
        save_memories(memories_file, facts)

        loaded = load_existing_memories(memories_file)
        assert set(loaded) == set(facts)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        memories_file = tmp_path / "nested" / "deep" / "memories.md"
        save_memories(memories_file, ["Some fact here"])
        assert memories_file.exists()

    def test_save_sorted_output(self, tmp_path: Path) -> None:
        memories_file = tmp_path / "memories.md"
        save_memories(memories_file, ["Zebra fact", "Apple fact"])
        text = memories_file.read_text()
        apple_pos = text.index("Apple fact")
        zebra_pos = text.index("Zebra fact")
        assert apple_pos < zebra_pos


# ---------------------------------------------------------------------------
# _get_anthropic_api_key
# ---------------------------------------------------------------------------


class TestGetAnthropicApiKey:
    def test_env_var_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-123")
        result = _get_anthropic_api_key()
        assert result == "env-key-123"

    def test_reads_from_toml_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config_path = tmp_path / ".config" / "gptme" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text('[env]\nANTHROPIC_API_KEY = "toml-key-456"\n')

        with patch("gptme_user_memories.extractor.Path.home", return_value=tmp_path):
            result = _get_anthropic_api_key()
        assert result == "toml-key-456"

    def test_missing_config_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("gptme_user_memories.extractor.Path.home", return_value=tmp_path):
            result = _get_anthropic_api_key()
        assert result is None

    def test_invalid_toml_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config_path = tmp_path / ".config" / "gptme" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("not valid toml ][[[")

        with patch("gptme_user_memories.extractor.Path.home", return_value=tmp_path):
            result = _get_anthropic_api_key()
        assert result is None


# ---------------------------------------------------------------------------
# run_batch — regression tests
# ---------------------------------------------------------------------------


class TestRunBatch:
    def test_non_dir_entry_in_cc_logs_does_not_halt_iteration(
        self, tmp_path: Path
    ) -> None:
        """Regression: break instead of continue for non-dir entries caused
        all directories *after* a .DS_Store-like file to be silently skipped."""
        cc_dir = tmp_path / ".claude" / "projects"
        cc_dir.mkdir(parents=True)

        # Place a non-directory file before valid project dirs (sorted order matters)
        (cc_dir / ".DS_Store").write_bytes(b"bogus")

        # Two valid project directories, each with a JSONL conversation
        long_content = (
            "I work as a software engineer at Acme Corp. "
            "I have been programming for over ten years using Python and Go."
        )
        for proj_name in ("project-a", "project-b"):
            proj = cc_dir / proj_name
            proj.mkdir()
            conv = proj / "conv.jsonl"
            conv.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": long_content,
                        },
                    }
                )
                + "\n"
            )

        facts_extracted: list[str] = []

        def fake_extract(text: str, model: str = "") -> list[str]:
            facts_extracted.append(text)
            return ["Works at Acme Corp"]

        with (
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", cc_dir),
            patch("gptme_user_memories.extractor.LOGS_DIR", tmp_path / "no-gptme-logs"),
            patch("gptme_user_memories.extractor.extract_facts", fake_extract),
        ):
            run_batch(days=9999, limit=10, dry_run=True)

        # Both project dirs must have been visited; without the fix only 0 would be
        assert len(facts_extracted) == 2, (
            f"Expected 2 dirs processed, got {len(facts_extracted)}. "
            "The break-vs-continue bug may have been reintroduced."
        )
