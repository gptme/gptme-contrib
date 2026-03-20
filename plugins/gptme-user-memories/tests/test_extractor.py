"""Tests for user memory extraction logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_user_memories.extractor import (
    SENTINEL_FILENAME,
    _get_anthropic_api_key,
    get_cc_user_messages,
    get_user_messages,
    is_autonomous_session,
    is_cc_autonomous_session,
    load_existing_memories,
    merge_facts,
    process_cc_logfile,
    process_logdir,
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

    def test_bare_autonomous_word_not_false_positive(self, tmp_path: Path) -> None:
        """Regression: bare 'autonomous' must not filter personal conversations.

        A user asking "how do I build autonomous agents?" should not be mistaken
        for an agent session — only specific multi-word patterns qualify.
        """
        conv = _make_gptme_conv(
            tmp_path,
            [{"role": "user", "content": "How do I build autonomous agents?"}],
        )
        assert not is_autonomous_session(conv)

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert not is_autonomous_session(tmp_path / "nonexistent.jsonl")

    def test_malformed_json_is_skipped(self, tmp_path: Path) -> None:
        conv_file = tmp_path / "bad.jsonl"
        conv_file.write_text("not valid json\n")
        assert not is_autonomous_session(conv_file)

    def test_autonomous_pattern_in_list_content_returns_true(
        self, tmp_path: Path
    ) -> None:
        """Regression: str() on list content could miss patterns; use text extraction."""
        conv = _make_gptme_conv(
            tmp_path,
            [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "You are Bob, an autonomous agent."}
                    ],
                }
            ],
        )
        assert is_autonomous_session(conv)

    def test_pure_tool_result_list_content_skipped(self, tmp_path: Path) -> None:
        """Pure tool-result messages with no text blocks should not cause false positives."""
        conv = _make_gptme_conv(
            tmp_path,
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "You are Bob, an autonomous agent.",
                        }
                    ],
                }
            ],
        )
        assert not is_autonomous_session(conv)

    def test_autonomous_pattern_in_assistant_message_no_false_positive(
        self, tmp_path: Path
    ) -> None:
        """Regression: assistant messages explaining autonomous mode must not trigger
        classification as autonomous — only system/user messages should be checked."""
        conv = _make_gptme_conv(
            tmp_path,
            [
                {
                    "role": "user",
                    "content": "How does gptme autonomous mode work?",
                },
                {
                    "role": "assistant",
                    # Assistant explanation contains the trigger pattern
                    "content": "gptme-prompt- is used when running in autonomous mode...",
                },
            ],
        )
        assert not is_autonomous_session(conv)


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

    def test_autonomous_pattern_in_mixed_content_returns_true(
        self, tmp_path: Path
    ) -> None:
        """Autonomous pattern in the text part of a mixed-content message must be detected."""
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
                                "tool_use_id": "x",
                                "content": "some output",
                            },
                            {
                                "type": "text",
                                "text": "You are starting an autonomous work session.",
                            },
                        ],
                    },
                }
            ],
        )
        assert is_cc_autonomous_session(cc_file)

    def test_pure_tool_result_skipped(self, tmp_path: Path) -> None:
        """A message with only tool_result content (no text) is skipped, not mis-classified."""
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
                                "tool_use_id": "x",
                                "content": "You are starting an autonomous work session.",
                            }
                        ],
                    },
                }
            ],
        )
        assert not is_cc_autonomous_session(cc_file)

    def test_bare_autonomous_word_not_false_positive(self, tmp_path: Path) -> None:
        """Regression: bare 'autonomous' must not filter CC personal conversations."""
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "How do I build autonomous agents?",
                    },
                }
            ],
        )
        assert not is_cc_autonomous_session(cc_file)

    def test_autonomous_pattern_in_later_message_detected(self, tmp_path: Path) -> None:
        """Regression: autonomous pattern in 2nd+ message must not be missed.

        Previously is_cc_autonomous_session returned False after the first
        substantial non-autonomous message, so patterns in later messages were
        silently skipped and the session was mis-classified as personal.
        """
        cc_file = _make_cc_jsonl(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Good morning, let's get started.",
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "You are starting an autonomous work session.",
                    },
                },
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

    def test_save_case_insensitive_sort(self, tmp_path: Path) -> None:
        """Facts are sorted case-insensitively, matching merge_facts deduplication."""
        memories_file = tmp_path / "memories.md"
        save_memories(
            memories_file,
            ["banana preference", "Apple preference", "cherry preference"],
        )
        text = memories_file.read_text()
        apple_pos = text.index("Apple preference")
        banana_pos = text.index("banana preference")
        cherry_pos = text.index("cherry preference")
        # Case-insensitive: Apple < banana < cherry (not ASCII: banana < Apple)
        assert apple_pos < banana_pos < cherry_pos

    def test_save_no_tmp_file_left_behind(self, tmp_path: Path) -> None:
        """Atomic write via temp-then-rename should leave no .tmp file."""
        memories_file = tmp_path / "memories.md"
        save_memories(memories_file, ["Some fact"])
        assert memories_file.exists()
        # save_memories creates memories.<pid>.tmp — check no pid-suffixed tmp file remains
        assert not list(tmp_path.glob("memories.*.tmp"))

    def test_save_cleans_tmp_on_replace_failure(self, tmp_path: Path) -> None:
        """Regression: .tmp file must be cleaned up even when replace() raises.

        If replace() fails (e.g., cross-device link, permissions), the finally
        block must unlink the stale temp file so it doesn't accumulate on disk.
        """
        memories_file = tmp_path / "memories.md"
        with patch("pathlib.Path.replace", side_effect=OSError("cross-device link")):
            with pytest.raises(OSError):
                save_memories(memories_file, ["Some fact"])
        # The .tmp file must be cleaned up despite the replace() failure
        assert not list(tmp_path.glob("memories.*.tmp"))


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

    def test_env_var_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var with surrounding whitespace should be stripped like the TOML path."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "  env-key-123  ")
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
# extract_facts — NO_NEW_FACTS handling (regression)
# ---------------------------------------------------------------------------


class TestExtractFactsNoNewFacts:
    """Regression: NO_NEW_FACTS must be an exact-match check, not a substring check."""

    def _mock_response(self, text: str):
        from unittest.mock import MagicMock

        block = MagicMock()
        block.text = text
        response = MagicMock()
        response.content = [block]
        return response

    def test_exact_no_new_facts_returns_empty(self) -> None:
        """Model returns exactly NO_NEW_FACTS → no facts extracted."""
        with (
            patch(
                "gptme_user_memories.extractor._get_anthropic_api_key",
                return_value="key",
            ),
            patch("anthropic.Anthropic") as mock_cls,
        ):
            mock_cls.return_value.messages.create.return_value = self._mock_response(
                "NO_NEW_FACTS"
            )
            from gptme_user_memories.extractor import extract_facts

            assert extract_facts("some conversation text " * 10) == []

    def test_fact_containing_no_new_facts_substring_is_not_dropped(self) -> None:
        """A fact that mentions 'NO_NEW_FACTS' as part of a sentence must NOT be discarded."""
        response_text = (
            "- Concerned about the NO_NEW_FACTS sentinel in the memory plugin"
        )
        with (
            patch(
                "gptme_user_memories.extractor._get_anthropic_api_key",
                return_value="key",
            ),
            patch("anthropic.Anthropic") as mock_cls,
        ):
            mock_cls.return_value.messages.create.return_value = self._mock_response(
                response_text
            )
            from gptme_user_memories.extractor import extract_facts

            facts = extract_facts("some conversation text " * 10)
        assert any(
            "NO_NEW_FACTS" in f for f in facts
        ), f"Expected fact containing NO_NEW_FACTS to be kept, got: {facts}"


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

        # Place .DS_Store AFTER the project dirs so it has a newer mtime and
        # appears first in newest-first sorted order. This makes the break-vs-continue
        # bug observable: with break, iteration stops immediately on .DS_Store and
        # neither project dir is visited (0 extracted); with continue, .DS_Store is
        # skipped and both project dirs are processed (2 extracted).
        (cc_dir / ".DS_Store").write_bytes(b"bogus")

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

        # Both project dirs must have been visited; without the fix (break instead of
        # continue) iteration stops on .DS_Store and 0 dirs are processed.
        assert len(facts_extracted) == 2, (
            f"Expected 2 dirs processed, got {len(facts_extracted)}. "
            "The break-vs-continue bug may have been reintroduced."
        )

    def test_autonomous_sessions_do_not_consume_limit(self, tmp_path: Path) -> None:
        """Regression: autonomous sessions were counted toward the limit even
        though no API call was made, exhausting limit=1 before personal sessions."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        long_personal = (
            "I work as a software engineer at Acme Corp. "
            "I have been programming for over ten years using Python and Go."
        )
        # Create one autonomous session and one personal session
        for name, content, is_auto in [
            (
                "auto-session",
                "You are starting an autonomous work session.",
                True,
            ),
            ("personal-session", long_personal, False),
        ]:
            d = logs_dir / name
            d.mkdir()
            (d / "conversation.jsonl").write_text(
                json.dumps({"role": "user", "content": content}) + "\n"
            )

        api_calls: list[str] = []

        def fake_extract(text: str, model: str = "") -> list[str]:
            api_calls.append(text)
            return ["Works at Acme Corp"]

        with (
            patch("gptme_user_memories.extractor.LOGS_DIR", logs_dir),
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", tmp_path / "no-cc-logs"),
            patch("gptme_user_memories.extractor.extract_facts", fake_extract),
        ):
            # limit=1 — should be consumed by the personal session, not the auto one
            run_batch(days=9999, limit=1, dry_run=True)

        assert len(api_calls) == 1, (
            f"Expected 1 API call (personal session only), got {len(api_calls)}. "
            "Autonomous sessions must not consume the limit."
        )

    def test_limit_1_not_exceeded_across_both_sources(self, tmp_path: Path) -> None:
        """Regression: --limit 1 allowed up to 2 sessions (1 per source) because
        per_source_limit = max(1, 1//2) = 1, letting each source process independently.
        Total sessions processed must respect the hard limit."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        cc_logs_dir = tmp_path / "cc-logs"
        cc_logs_dir.mkdir()

        long_content = (
            "I work as a software engineer at Acme Corp. "
            "I have been programming for over ten years using Python and Go."
        )

        # One gptme session
        d = logs_dir / "gptme-session"
        d.mkdir()
        (d / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": long_content}) + "\n"
        )

        # One CC session
        proj = cc_logs_dir / "my-project"
        proj.mkdir()
        (proj / "conv.jsonl").write_text(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": long_content}}
            )
            + "\n"
        )

        sessions_processed: list[str] = []

        def fake_process_logdir(log_dir: Path, **kwargs: object) -> list[str] | None:
            sessions_processed.append(f"gptme:{log_dir.name}")
            return ["gptme fact"]

        def fake_process_cc(jsonl_file: Path, **kwargs: object) -> list[str] | None:
            sessions_processed.append(f"cc:{jsonl_file.name}")
            return ["cc fact"]

        with (
            patch("gptme_user_memories.extractor.LOGS_DIR", logs_dir),
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", cc_logs_dir),
            patch(
                "gptme_user_memories.extractor.process_logdir",
                side_effect=fake_process_logdir,
            ),
            patch(
                "gptme_user_memories.extractor.process_cc_logfile",
                side_effect=fake_process_cc,
            ),
        ):
            run_batch(days=9999, limit=1, dry_run=True)

        assert len(sessions_processed) <= 1, (
            f"Expected at most 1 session processed with --limit 1, "
            f"got {len(sessions_processed)}: {sessions_processed}. "
            "per_source_limit must not allow each source to independently reach 1."
        )

    def test_broken_symlink_in_logs_dir_does_not_crash(self, tmp_path: Path) -> None:
        """Regression: p.stat().st_mtime in sorted() key raises OSError for broken
        symlinks, causing the entire batch scan to abort before processing any logs."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        long_content = (
            "I work as a software engineer at Acme Corp. "
            "I have been programming for over ten years using Python and Go."
        )
        valid_session = logs_dir / "valid-session"
        valid_session.mkdir()
        (valid_session / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": long_content}) + "\n"
        )

        # Broken symlink — stat() raises OSError
        broken = logs_dir / "broken-link"
        broken.symlink_to(logs_dir / "nonexistent-target")

        api_calls: list[str] = []

        def fake_extract(text: str, model: str = "") -> list[str]:
            api_calls.append(text)
            return ["Works at Acme Corp"]

        with (
            patch("gptme_user_memories.extractor.LOGS_DIR", logs_dir),
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", tmp_path / "no-cc-logs"),
            patch("gptme_user_memories.extractor.extract_facts", fake_extract),
        ):
            # Must not raise OSError; valid session must still be processed
            run_batch(days=9999, limit=10, dry_run=True)

        assert len(api_calls) == 1, (
            f"Expected 1 API call (valid session), got {len(api_calls)}. "
            "A broken symlink in the logs dir must not abort the batch scan."
        )

    def test_broken_symlink_jsonl_in_cc_logs_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        """Regression: jsonl_file.stat().st_mtime in CC inner loop raises OSError for
        broken .jsonl symlinks, crashing the entire batch scan mid-iteration."""
        cc_logs_dir = tmp_path / "cc-logs"
        cc_logs_dir.mkdir()

        proj_dir = cc_logs_dir / "some-project"
        proj_dir.mkdir()

        long_content = (
            "I work as a data scientist at Widgets Inc. "
            "I primarily use Python and SQL for my work."
        )
        valid_jsonl = proj_dir / "valid-session.jsonl"
        valid_jsonl.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": long_content},
                }
            )
            + "\n"
        )

        # Broken .jsonl symlink — stat() raises OSError
        broken = proj_dir / "broken-link.jsonl"
        broken.symlink_to(proj_dir / "nonexistent-target.jsonl")

        api_calls: list[str] = []

        def fake_extract(text: str, model: str = "") -> list[str]:
            api_calls.append(text)
            return ["Works at Widgets Inc"]

        with (
            patch("gptme_user_memories.extractor.LOGS_DIR", tmp_path / "no-gptme-logs"),
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", cc_logs_dir),
            patch("gptme_user_memories.extractor.extract_facts", fake_extract),
        ):
            # Must not raise OSError; valid session must still be processed
            run_batch(days=9999, limit=10, dry_run=True)

        assert len(api_calls) == 1, (
            f"Expected 1 API call (valid CC session), got {len(api_calls)}. "
            "A broken .jsonl symlink in a CC project dir must not abort the batch scan."
        )

    def test_per_source_limit_prevents_cc_starvation(self, tmp_path: Path) -> None:
        """Regression: shared processed counter caused CC logs to be skipped entirely
        when gptme sessions filled the limit first.

        With limit=4 and 4 gptme sessions, the old code set processed=4 before
        reaching CC logs, so the CC break fired immediately and zero CC sessions
        were extracted. Per-source limits (limit//2 each) fix this.
        """
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        cc_logs_dir = tmp_path / "cc-logs"
        cc_logs_dir.mkdir()

        long_content = (
            "I work as a software engineer at Acme Corp. "
            "I have been programming for over ten years using Python and Go."
        )

        # Create 4 gptme sessions — enough to exhaust limit=4 with the old shared counter
        for i in range(4):
            d = logs_dir / f"session-{i}"
            d.mkdir()
            (d / "conversation.jsonl").write_text(
                json.dumps({"role": "user", "content": long_content}) + "\n"
            )

        # Create 1 CC session — should be processed even though gptme fills its half
        proj = cc_logs_dir / "my-project"
        proj.mkdir()
        (proj / "conv.jsonl").write_text(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": long_content}}
            )
            + "\n"
        )

        sources_seen: list[str] = []

        def fake_process_logdir(log_dir: Path, **kwargs: object) -> list[str] | None:
            sources_seen.append(f"gptme:{log_dir.name}")
            return ["gptme fact"]

        def fake_process_cc(jsonl_file: Path, **kwargs: object) -> list[str] | None:
            sources_seen.append(f"cc:{jsonl_file.name}")
            return ["cc fact"]

        with (
            patch("gptme_user_memories.extractor.LOGS_DIR", logs_dir),
            patch("gptme_user_memories.extractor.CC_LOGS_DIR", cc_logs_dir),
            patch(
                "gptme_user_memories.extractor.process_logdir",
                side_effect=fake_process_logdir,
            ),
            patch(
                "gptme_user_memories.extractor.process_cc_logfile",
                side_effect=fake_process_cc,
            ),
        ):
            run_batch(days=9999, limit=4, dry_run=True)

        cc_calls = [s for s in sources_seen if s.startswith("cc:")]
        assert len(cc_calls) >= 1, (
            f"CC logs were starved: sources_seen={sources_seen}. "
            "Per-source limits must ensure CC logs are processed even when "
            "gptme sessions fill their half of the limit."
        )


# ---------------------------------------------------------------------------
# process_logdir
# ---------------------------------------------------------------------------


class TestProcessLogdir:
    def _make_logdir(self, tmp_path: Path, messages: list[dict]) -> Path:
        log_dir = tmp_path / "session1"
        log_dir.mkdir()
        (log_dir / "conversation.jsonl").write_text(
            "\n".join(json.dumps(m) for m in messages) + "\n"
        )
        return log_dir

    _LONG_MSG = "I work as a software engineer at Acme Corp and have been coding for over ten years."

    def test_returns_facts_for_personal_session(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": self._LONG_MSG}],
        )
        with patch(
            "gptme_user_memories.extractor.extract_facts",
            return_value=["Works at Acme Corp"],
        ):
            facts = process_logdir(log_dir, dry_run=True)
        assert facts == ["Works at Acme Corp"]

    def test_skips_sentinel_exists(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": self._LONG_MSG}],
        )
        (log_dir / SENTINEL_FILENAME).touch()
        facts = process_logdir(log_dir)
        assert facts is None

    def test_force_ignores_sentinel(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": self._LONG_MSG}],
        )
        (log_dir / SENTINEL_FILENAME).touch()
        with patch(
            "gptme_user_memories.extractor.extract_facts",
            return_value=["Works at Acme Corp"],
        ):
            facts = process_logdir(log_dir, force=True, dry_run=True)
        assert facts == ["Works at Acme Corp"]

    def test_skips_autonomous_session(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [
                {
                    "role": "system",
                    "content": "You are starting an autonomous work session.",
                }
            ],
        )
        facts = process_logdir(log_dir, dry_run=True)
        assert facts is None

    def test_skips_short_conversation(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": "ok"}],
        )
        facts = process_logdir(log_dir, dry_run=True)
        assert facts is None

    def test_touches_sentinel_on_success(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": self._LONG_MSG}],
        )
        with patch("gptme_user_memories.extractor.extract_facts", return_value=[]):
            process_logdir(log_dir)
        assert (log_dir / SENTINEL_FILENAME).exists()

    def test_dry_run_does_not_touch_sentinel(self, tmp_path: Path) -> None:
        log_dir = self._make_logdir(
            tmp_path,
            [{"role": "user", "content": self._LONG_MSG}],
        )
        with patch("gptme_user_memories.extractor.extract_facts", return_value=[]):
            process_logdir(log_dir, dry_run=True)
        assert not (log_dir / SENTINEL_FILENAME).exists()

    def test_missing_conv_file_returns_none(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "empty_session"
        log_dir.mkdir()
        facts = process_logdir(log_dir)
        assert facts is None


# ---------------------------------------------------------------------------
# process_cc_logfile
# ---------------------------------------------------------------------------


class TestProcessCCLogfile:
    _LONG_MSG = "I work as a software engineer at Acme Corp and have been coding for over ten years."

    def _make_cc_file(self, tmp_path: Path, messages: list[dict]) -> Path:
        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
        return jsonl_file

    def test_returns_facts_for_personal_session(self, tmp_path: Path) -> None:
        jsonl_file = self._make_cc_file(
            tmp_path,
            [{"type": "user", "message": {"role": "user", "content": self._LONG_MSG}}],
        )
        with patch(
            "gptme_user_memories.extractor.extract_facts",
            return_value=["Works at Acme Corp"],
        ):
            facts = process_cc_logfile(jsonl_file, dry_run=True)
        assert facts == ["Works at Acme Corp"]

    def test_skips_sentinel_exists(self, tmp_path: Path) -> None:
        jsonl_file = self._make_cc_file(
            tmp_path,
            [{"type": "user", "message": {"role": "user", "content": self._LONG_MSG}}],
        )
        jsonl_file.with_suffix(".memories-extracted").touch()
        facts = process_cc_logfile(jsonl_file)
        assert facts is None

    def test_skips_autonomous_session(self, tmp_path: Path) -> None:
        jsonl_file = self._make_cc_file(
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
        facts = process_cc_logfile(jsonl_file, dry_run=True)
        assert facts is None

    def test_touches_sentinel_on_success(self, tmp_path: Path) -> None:
        jsonl_file = self._make_cc_file(
            tmp_path,
            [{"type": "user", "message": {"role": "user", "content": self._LONG_MSG}}],
        )
        with patch("gptme_user_memories.extractor.extract_facts", return_value=[]):
            process_cc_logfile(jsonl_file)
        assert jsonl_file.with_suffix(".memories-extracted").exists()

    def test_dry_run_does_not_touch_sentinel(self, tmp_path: Path) -> None:
        jsonl_file = self._make_cc_file(
            tmp_path,
            [{"type": "user", "message": {"role": "user", "content": self._LONG_MSG}}],
        )
        with patch("gptme_user_memories.extractor.extract_facts", return_value=[]):
            process_cc_logfile(jsonl_file, dry_run=True)
        assert not jsonl_file.with_suffix(".memories-extracted").exists()


# ---------------------------------------------------------------------------
# main (CLI entry point)
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_saves_new_facts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "memories.md"
        monkeypatch.setattr(
            "sys.argv",
            ["gptme-user-memories", "--output", str(output)],
        )
        with (
            patch(
                "gptme_user_memories.extractor.run_batch",
                return_value=["Works at Acme Corp"],
            ),
            patch("gptme_user_memories.extractor.USER_MEMORIES_FILE", output),
        ):
            from gptme_user_memories.extractor import main

            main()
        assert output.exists()
        assert "Works at Acme Corp" in output.read_text()

    def test_main_dry_run_does_not_save(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:  # type: ignore[type-arg]
        output = tmp_path / "memories.md"
        monkeypatch.setattr(
            "sys.argv",
            ["gptme-user-memories", "--dry-run", "--output", str(output)],
        )
        with patch(
            "gptme_user_memories.extractor.run_batch",
            return_value=["Works at Acme Corp"],
        ):
            from gptme_user_memories.extractor import main

            main()
        assert not output.exists()
        captured = capsys.readouterr()
        assert "Works at Acme Corp" in captured.out

    def test_main_no_facts_prints_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:  # type: ignore[type-arg]
        output = tmp_path / "memories.md"
        monkeypatch.setattr(
            "sys.argv",
            ["gptme-user-memories", "--output", str(output)],
        )
        with patch("gptme_user_memories.extractor.run_batch", return_value=[]):
            from gptme_user_memories.extractor import main

            main()
        assert not output.exists()
        captured = capsys.readouterr()
        assert "No new facts" in captured.out

    def test_main_forwards_model_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "memories.md"
        monkeypatch.setattr(
            "sys.argv",
            [
                "gptme-user-memories",
                "--model",
                "claude-haiku-3-5",
                "--output",
                str(output),
            ],
        )
        captured_model: list[str] = []

        def fake_run_batch(**kwargs: object) -> list[str]:
            captured_model.append(str(kwargs.get("model", "")))
            return []

        with patch(
            "gptme_user_memories.extractor.run_batch",
            side_effect=fake_run_batch,
        ):
            from gptme_user_memories.extractor import main

            main()

        assert (
            captured_model == ["claude-haiku-3-5"]
        ), f"Expected model 'claude-haiku-3-5' forwarded to run_batch, got {captured_model}"


# ---------------------------------------------------------------------------
# session_end hook (sentinel pre-check)
# ---------------------------------------------------------------------------


class TestSessionEndHook:
    """Tests for the SESSION_END hook sentinel pre-check."""

    _LONG_MSG = "I work as a software engineer at Acme Corp and have been coding for over ten years."

    def _make_logdir(self, tmp_path: Path, messages: list[dict]) -> Path:
        log_dir = tmp_path / "session1"
        log_dir.mkdir()
        (log_dir / "conversation.jsonl").write_text(
            "\n".join(json.dumps(m) for m in messages) + "\n"
        )
        return log_dir

    def test_skips_session_when_sentinel_exists(self, tmp_path: Path) -> None:
        """Regression: hook must check sentinel.exists() before calling extract_facts.

        If run_batch already processed a session mid-conversation and set the sentinel,
        the hook should skip it at session-end rather than making a redundant API call.
        """
        from unittest.mock import MagicMock

        from gptme_user_memories.hooks.session_end import session_end_user_memories_hook

        log_dir = self._make_logdir(
            tmp_path, [{"role": "user", "content": self._LONG_MSG}]
        )
        # Simulate run_batch having already processed this session
        (log_dir / SENTINEL_FILENAME).touch()

        api_calls: list[str] = []

        with patch(
            "gptme_user_memories.hooks.session_end.extract_facts",
            side_effect=lambda text, **kw: api_calls.append(text) or [],
        ):
            list(session_end_user_memories_hook(MagicMock(), logdir=log_dir))

        assert api_calls == [], (
            "extract_facts must not be called when sentinel already exists "
            "(would waste API quota on double-processing)"
        )

    def test_hook_resilient_to_save_errors(self, tmp_path: Path) -> None:
        """Regression: OSError in save_memories must not propagate out of the hook.

        An OSError (disk full, bad permissions on ~/.local/share/gptme/) would
        otherwise crash gptme at session end. The hook should log a warning and
        NOT touch the sentinel — so the session is retried on the next run.
        """
        from unittest.mock import MagicMock

        from gptme_user_memories.hooks.session_end import session_end_user_memories_hook

        log_dir = self._make_logdir(
            tmp_path, [{"role": "user", "content": self._LONG_MSG}]
        )

        with (
            patch(
                "gptme_user_memories.hooks.session_end.extract_facts",
                return_value=["user is a software engineer"],
            ),
            patch(
                "gptme_user_memories.hooks.session_end.load_existing_memories",
                return_value=[],
            ),
            patch(
                "gptme_user_memories.hooks.session_end.save_memories",
                side_effect=OSError("disk full"),
            ),
        ):
            # Must not raise — hook should catch OSError and log at WARNING
            list(session_end_user_memories_hook(MagicMock(), logdir=log_dir))

        # Sentinel must NOT be touched — transient failure should allow retry on next run.
        # If we touch sentinel here, successfully-extracted facts are permanently lost.
        assert not (log_dir / SENTINEL_FILENAME).exists(), (
            "sentinel must NOT be touched when save_memories raises — "
            "successfully-extracted facts would be permanently lost if we mark "
            "the session as processed before confirming a successful write"
        )

    def test_hook_touches_sentinel_after_successful_save(self, tmp_path: Path) -> None:
        """Sentinel is written after a successful save_memories call."""
        from unittest.mock import MagicMock

        from gptme_user_memories.hooks.session_end import session_end_user_memories_hook

        log_dir = self._make_logdir(
            tmp_path, [{"role": "user", "content": self._LONG_MSG}]
        )

        with (
            patch(
                "gptme_user_memories.hooks.session_end.extract_facts",
                return_value=["user is a software engineer"],
            ),
            patch(
                "gptme_user_memories.hooks.session_end.load_existing_memories",
                return_value=[],
            ),
            patch("gptme_user_memories.hooks.session_end.save_memories"),
        ):
            list(session_end_user_memories_hook(MagicMock(), logdir=log_dir))

        assert (
            log_dir / SENTINEL_FILENAME
        ).exists(), "sentinel must be touched after a successful save"
