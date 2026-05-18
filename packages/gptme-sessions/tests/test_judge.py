"""Tests for the LLM-as-judge module."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gptme.message import Message

from gptme_sessions.judge import (
    DEFAULT_GOALS,
    DEFAULT_JUDGE_MODEL,
    JUDGE_VERSION,
    JUDGE_PROMPT_TEMPLATE,
    JUDGE_SYSTEM,
    NO_THINK_PREFILL,
    _get_api_key,
    _judge_openrouter_env,
    _parse_judge_payload,
    _prepare_messages_for_model,
    _resolve_openrouter_api_key,
    _is_anthropic_direct_model,
    _strip_anthropic_prefix,
    format_intent_context,
    format_routing_context,
    judge_and_writeback,
    judge_from_signals,
    judge_session,
    judge_session_with_fallback,
    normalize_judge_verdict,
)
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


class TestJudgeSession:
    """Tests for judge_session()."""

    def test_returns_none_without_anthropic(self) -> None:
        """Judge returns None when anthropic is not installed."""
        # Setting sys.modules["anthropic"] = None causes `import anthropic` to raise ImportError
        with patch.dict("sys.modules", {"anthropic": None}):
            result = judge_session("test session text", category="code", api_key="fake-key")
        assert result is None

    def test_returns_none_without_api_key(self) -> None:
        """Judge returns None when no API key is available."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gptme_sessions.judge._get_api_key", return_value=""),
        ):
            result = judge_session("test text", category="code")
            assert result is None

    def test_successful_evaluation(self) -> None:
        """Judge returns score and reason on success."""
        # Create a mock anthropic module
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.75, "reason": "Good work"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        # patch.dict replaces sys.modules["anthropic"] so the `import anthropic`
        # inside judge_session picks up the mock without needing a module reload.
        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text", category="code")

        assert result is not None
        assert result["score"] == 0.75
        assert result["reason"] == "Good work"
        assert result["model"] == DEFAULT_JUDGE_MODEL

    def test_prompt_template_wellformed(self) -> None:
        """Verify that the prompt template and system prompt are well-formed."""
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            goals="Test goals",
            category="code",
            routing_context="",
            intent_context="",
            journal="Did some work",
        )
        assert "Test goals" in prompt
        assert "code" in prompt
        assert "Did some work" in prompt
        assert "0.0-1.0" in prompt
        assert "JSON" in JUDGE_SYSTEM

    def test_prompt_template_includes_category_interpretation(self) -> None:
        """The prompt must instruct the judge to score within-category, not penalize support categories.

        Regression guard: when the judge does not see a category-interpretation block,
        it applies revenue/goal-#1 alignment as a fixed ceiling on
        infrastructure/monitoring/knowledge work, producing systematic harness
        bias documented in
        knowledge/strategic/2026-05-10-gptme-harness-verification-gap.md.
        """
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            goals="Test goals",
            category="infrastructure",
            routing_context="",
            intent_context="",
            journal="Reduced future friction",
        )
        assert "Category Interpretation" in prompt
        assert "within-category value" in prompt
        # The block must explicitly name representative support categories so
        # the judge does not collapse them into "non-revenue, low-score".
        for support_cat in ("infrastructure", "monitoring", "knowledge", "research"):
            assert support_cat in prompt

    def test_prompt_includes_worked_examples(self) -> None:
        """The judge prompt must include worked reasoning examples showing how to grade
        support-category sessions.

        Regression guard: if the worked examples are removed or become unreachable,
        the judge loses the "teaching why" layer that counteracts the remaining bias
        documented in knowledge/research/2026-05-09-teaching-claude-why-lesson-validation.md.
        """
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            goals="Test goals",
            category="cleanup",
            routing_context="",
            intent_context="",
            journal="Cleaned up stale references",
        )
        assert "## Worked Examples" in prompt
        assert "infrastructure, ~0.78" in prompt
        assert "cleanup, ~0.75" in prompt
        assert "research, ~0.72" in prompt
        assert "within-category" in prompt
        assert "compounding" in prompt

    def test_score_clamping(self) -> None:
        """Out-of-range scores from the LLM are clamped to [0.0, 1.0]."""
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        # LLM returns score above 1.0
        mock_response.content = [MagicMock(text='{"score": 1.5, "reason": "Overconfident LLM"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text")

        assert result is not None
        assert result["score"] == 1.0  # clamped from 1.5

        # Also test clamping from below
        mock_response.content = [MagicMock(text='{"score": -0.3, "reason": "Below zero"}')]
        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text")

        assert result is not None
        assert result["score"] == 0.0  # clamped from -0.3

    def test_curly_braces_in_journal_dont_crash(self) -> None:
        """Journal text with curly braces (JSON, Python dicts) doesn't raise KeyError.

        str.format() only parses {placeholder} in the template itself — keyword
        argument values are substituted verbatim, so {/} in the journal are safe.
        """
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.6, "reason": "Wrote code"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        curly_brace_journal = (
            'Wrote code: {"key": "value", "nested": {"a": 1}}\n'
            "Shell expansion: ${HOME}/path\n"
            "Dict literal: {'foo': 'bar'}"
        )

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session(curly_brace_journal, category="code")

        assert result is not None
        assert result["score"] == 0.6

    def test_default_goals_is_generic(self) -> None:
        """Default goals should work for any agent, not just Bob."""
        assert "Bob" not in DEFAULT_GOALS
        assert "agent" in DEFAULT_GOALS.lower()

    def test_format_routing_context_returns_empty_when_unset(self) -> None:
        """No cascade_context => no Routing Context block (back-compat)."""
        assert format_routing_context(None) == ""
        assert format_routing_context({}) == ""

    def test_format_routing_context_ignores_tier1_and_tier2(self) -> None:
        """Tier 1/2 routing is itself top-priority work; no adjustment block."""
        assert format_routing_context({"tier": 1, "blocked_tier1_2_count": 0}) == ""
        assert format_routing_context({"tier": 2, "blocked_tier1_2_count": 0}) == ""
        # Junk types should also degrade safely.
        assert format_routing_context({"tier": "tier3"}) == ""

    def test_format_routing_context_renders_for_tier3(self) -> None:
        """Tier 3 with cascade evidence => routing block with summary lines."""
        block = format_routing_context(
            {
                "tier": 3,
                "blocked_tier1_2_count": 47,
                "selector_reason": "Cleanup neglected boost suppressed",
            }
        )
        assert "## Routing Context" in block
        assert "Tier 3" in block
        assert "47" in block
        assert "Cleanup neglected boost suppressed" in block

    def test_format_routing_context_truncates_long_reason(self) -> None:
        """Selector reason is bounded so it can't dominate the prompt."""
        block = format_routing_context({"tier": 3, "selector_reason": "x" * 500})
        # Cap at 200 chars; the raw 500-char string must not appear verbatim.
        assert "x" * 500 not in block
        assert "x" * 200 in block

    def test_format_routing_context_rejects_bool_as_blocked_count(self) -> None:
        """bool is a subclass of int; True should NOT render as blocked count."""
        block = format_routing_context({"tier": 3, "blocked_tier1_2_count": True})
        # The bool guard must stop 'True' from appearing in the prompt.
        assert "True" not in block
        assert "1" not in block

    def test_format_routing_context_rejects_non_string_selector_reason(self) -> None:
        """Non-string selector_reason (e.g. int) must not raise TypeError."""
        # int: would crash reason[:200] without the isinstance guard
        block = format_routing_context({"tier": 3, "selector_reason": 42})
        assert "42" not in block
        assert "Selector reason" not in block
        # list: another non-string truthy type
        block = format_routing_context({"tier": 3, "selector_reason": ["a", "b"]})
        assert "Selector reason" not in block
        # None degrades gracefully (existing behaviour)
        block = format_routing_context({"tier": 3, "selector_reason": None})
        assert "Selector reason" not in block

    def test_format_intent_context_returns_empty_for_none(self) -> None:
        """None intent returns empty string."""
        assert format_intent_context(None) == ""

    def test_format_intent_context_requires_required_fields(self) -> None:
        """Missing required fields returns empty string."""
        assert format_intent_context({"lane": "Tier1:code"}) == ""
        assert format_intent_context({"objective": "do work"}) == ""
        assert format_intent_context({"expected_artifact": "a PR"}) == ""

    def test_format_intent_context_renders_full_block(self) -> None:
        """All required fields produce a well-formed intent block."""
        block = format_intent_context(
            {
                "session_id": "d255",
                "lane": "Tier3:internal-code",
                "objective": "Design session intent contract",
                "expected_artifact": "scripts/session-intent.py + design doc",
            }
        )
        assert "## Session Intent" in block
        assert "Design session intent contract" in block
        assert "scripts/session-intent.py" in block
        assert "Tier3:internal-code" in block
        assert "Self-assigned alignment" not in block

    def test_format_intent_context_includes_self_alignment(self) -> None:
        """When outcome_alignment is set, the block shows the self-assigned verdict."""
        block = format_intent_context(
            {
                "session_id": "d255",
                "lane": "Tier1:strategic",
                "objective": "Wire intent contract into run wrapper",
                "expected_artifact": "autonomous-run.sh patches",
                "outcome_alignment": "on_track",
            }
        )
        assert "## Session Intent" in block
        assert "**Self-assigned alignment**: on_track" in block

    def test_judge_session_passes_intent_into_prompt(self) -> None:
        """When intent is provided, the prompt carries the block."""
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "score": 0.80,
                        "reason": "Aligned with intent",
                        "alignment_score": 0.90,
                        "pivot_verdict": "on_track",
                    }
                )
            )
        ]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        intent = {
            "session_id": "abc123",
            "lane": "Tier1:code",
            "objective": "Fix a bug",
            "expected_artifact": "a PR",
        }

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="fake-key"),
            patch(
                "gptme_sessions.judge.format_intent_context",
                wraps=format_intent_context,
            ) as mock_format,
        ):
            result = judge_session(
                "Fixed the bug", category="code", api_key="fake-key", intent=intent
            )

        assert result is not None
        assert result["score"] == 0.80
        assert result["alignment_score"] == 0.90
        assert result["pivot_verdict"] == "on_track"
        mock_format.assert_called_once_with(intent)

    def test_judge_session_passes_routing_context_into_prompt(self) -> None:
        """When cascade_context names Tier 3, the prompt carries the block."""
        captured: dict[str, str] = {}

        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.7, "reason": "OK"}')]

        def _capture_create(**kwargs):
            # The prompt is the first user message's content
            captured["prompt"] = kwargs["messages"][0]["content"]
            return mock_response

        mock_anthropic.Anthropic.return_value.messages.create.side_effect = _capture_create

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session(
                "session text",
                category="cleanup",
                cascade_context={"tier": 3, "blocked_tier1_2_count": 30},
            )

        assert result is not None
        assert "\n## Routing Context\n" in captured["prompt"]
        assert "## Routing-Aware Adjustment" in captured["prompt"]
        # The adjustment instructs the judge to cap the priority penalty.
        assert "−0.15" in captured["prompt"] or "-0.15" in captured["prompt"]

    def test_judge_session_omits_routing_block_when_no_context(self) -> None:
        """No cascade_context => the prompt has no Routing Context block."""
        captured: dict[str, str] = {}

        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.5, "reason": "Mid"}')]

        def _capture_create(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return mock_response

        mock_anthropic.Anthropic.return_value.messages.create.side_effect = _capture_create

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text", category="code")

        assert result is not None
        # The Routing-Aware adjustment text references the block name in
        # backticks; the actual block header is bare. Look for the header form.
        assert "\n## Routing Context\n" not in captured["prompt"]
        # The adjustment paragraph is always rendered (it self-skips when the
        # block is absent), so we don't assert its presence/absence here.

    def test_parse_judge_payload_handles_think_tags_and_fences(self) -> None:
        parsed = _parse_judge_payload(
            """
<think>internal</think>
```json
{"score": 1.4, "reason": "Shipped a real fix"}
```
""",
            "openai-subscription/gpt-5.4",
        )

        assert parsed == {
            "score": 1.0,
            "reason": "Shipped a real fix",
            "model": "openai-subscription/gpt-5.4",
        }

    def test_prepare_messages_for_qwen_adds_no_think_prefill(self) -> None:
        prepared = _prepare_messages_for_model(
            [Message("system", "judge"), Message("user", "score this")],
            "lmstudio/qwen/qwen3.6-35b-a3b",
        )

        assert [msg.role for msg in prepared] == ["system", "user", "assistant"]
        assert prepared[-1].content == NO_THINK_PREFILL

    def test_prepare_messages_for_other_models_is_noop(self) -> None:
        messages = [Message("system", "judge"), Message("user", "score this")]

        prepared = _prepare_messages_for_model(
            messages,
            "openai-subscription/gpt-5.4",
        )

        assert prepared == messages


class TestJudgeSessionWithFallback:
    """Tests for judge_session_with_fallback()."""

    def test_returns_primary_on_success(self, monkeypatch) -> None:
        """When the primary model succeeds, return its result without trying fallbacks."""
        calls: list[str] = []

        def _fake_judge_session(text, category=None, *, goals, model, **kw):
            calls.append(model)
            return {"score": 0.8, "reason": "Primary worked", "model": model}

        monkeypatch.setattr("gptme_sessions.judge.judge_session", _fake_judge_session)

        result = judge_session_with_fallback(
            "session text",
            goals="ship useful work",
            fallback_models=("fallback/model-a",),
        )

        assert result is not None
        assert result["score"] == 0.8
        assert calls == [DEFAULT_JUDGE_MODEL]

    def test_tries_fallback_after_primary_fails(self, monkeypatch) -> None:
        """When the primary fails, the first fallback model is used."""
        calls: list[str] = []

        def _fake_judge_session(text, category=None, *, goals, model, **kw):
            calls.append(model)
            if model == DEFAULT_JUDGE_MODEL:
                return None
            return {"score": 0.7, "reason": "Fallback worked", "model": model}

        monkeypatch.setattr("gptme_sessions.judge.judge_session", _fake_judge_session)

        result = judge_session_with_fallback(
            "session text",
            goals="ship useful work",
            fallback_models=("fallback/model-a", "fallback/model-b"),
        )

        assert result is not None
        assert result["score"] == 0.7
        assert calls == [DEFAULT_JUDGE_MODEL, "fallback/model-a"]

    def test_returns_none_when_all_models_fail(self, monkeypatch) -> None:
        """Returns None when every model in the chain fails."""
        monkeypatch.setattr(
            "gptme_sessions.judge.judge_session",
            lambda *a, **kw: None,
        )

        result = judge_session_with_fallback(
            "session text",
            goals="ship useful work",
            fallback_models=("fallback/model-a",),
        )

        assert result is None

    def test_no_fallbacks_uses_default_model_only(self, monkeypatch) -> None:
        """With no fallback_models, only the default model is tried."""
        calls: list[str] = []

        def _fake_judge_session(text, category=None, *, goals, model, **kw):
            calls.append(model)
            return None

        monkeypatch.setattr("gptme_sessions.judge.judge_session", _fake_judge_session)

        result = judge_session_with_fallback("session text", goals="ship work")
        assert result is None
        assert calls == [DEFAULT_JUDGE_MODEL]


class TestJudgeAndWritebackFallback:
    """Tests for judge_and_writeback() fallback_models parameter."""

    def test_uses_fallback_when_provided(self, tmp_path: Path, monkeypatch) -> None:
        """When fallback_models is given, judge_session_with_fallback is used."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="sid1", outcome="productive"))

        calls: list[str] = []

        def _fake_fallback(text, category=None, *, goals, default_model, fallback_models, **kw):
            calls.append(("fallback", default_model, fallback_models))
            return {"score": 0.75, "reason": "Fallback judge", "model": "fallback/m"}

        monkeypatch.setattr("gptme_sessions.judge.judge_session_with_fallback", _fake_fallback)

        result = judge_and_writeback(
            text="text",
            category="code",
            goals="ship work",
            session_id="sid1",
            sessions_dir=tmp_path,
            fallback_models=("fallback/m",),
        )

        assert result["status"] == "ok"
        assert calls == [("fallback", DEFAULT_JUDGE_MODEL, ("fallback/m",))]


class TestJudgeFromSignals:
    """Tests for judge_from_signals()."""

    def test_synthesizes_summary_from_signals(self) -> None:
        """When no journal text is provided, a summary is built from signals."""
        signals: dict = {
            "git_commits": ["feat: add feature (abc1234)"],
            "file_writes": ["/path/to/file.py"],
            "tool_calls": {"Bash": 5, "Edit": 3},
            "grade": 0.70,
            "error_count": 1,
            "session_duration_s": 300,
        }

        # Mock judge_session to capture the synthesized text
        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.65, "reason": "Test", "model": "haiku"}
            judge_from_signals(signals, category="code")

            # Verify judge_session was called with synthesized text
            mock_judge.assert_called_once()
            call_args = mock_judge.call_args
            journal_text = call_args[0][0]  # first positional arg
            assert "abc1234" in journal_text
            assert "/path/to/file.py" in journal_text
            assert "Bash:5" in journal_text
            assert "0.70" in journal_text

    def test_uses_provided_journal_text(self) -> None:
        """When journal text is provided, it's used directly."""
        signals: dict = {"git_commits": [], "file_writes": [], "tool_calls": {}}

        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.50, "reason": "Test", "model": "haiku"}
            judge_from_signals(signals, journal_text="My custom journal", category="triage")

            call_args = mock_judge.call_args
            assert call_args[0][0] == "My custom journal"

    def test_forwards_kwargs(self) -> None:
        """Extra kwargs are forwarded to judge_session."""
        signals: dict = {"git_commits": [], "file_writes": [], "tool_calls": {}}

        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.50, "reason": "Test", "model": "haiku"}
            judge_from_signals(
                signals,
                journal_text="text",
                category="code",
                goals="Custom goals",
                model="custom-model",
            )

            call_args = mock_judge.call_args
            assert call_args[1]["goals"] == "Custom goals"
            assert call_args[1]["model"] == "custom-model"


class TestModelRouting:
    """Tests for Anthropic-direct vs gptme.llm routing logic."""

    def test_bare_claude_id_is_direct(self) -> None:
        assert _is_anthropic_direct_model("claude-haiku-4-5-20251001") is True
        assert _is_anthropic_direct_model("claude-sonnet-4-5") is True

    def test_anthropic_prefix_is_direct(self) -> None:
        assert _is_anthropic_direct_model("anthropic/claude-sonnet-4.6") is True
        assert _is_anthropic_direct_model("anthropic/claude-haiku-4-5-20251001") is True

    def test_provider_prefixed_is_not_direct(self) -> None:
        assert _is_anthropic_direct_model("openrouter/anthropic/claude-sonnet-4.6") is False
        assert _is_anthropic_direct_model("openai-subscription/gpt-5.4") is False
        assert _is_anthropic_direct_model("lmstudio/qwen/qwen3.6-35b-a3b") is False
        assert _is_anthropic_direct_model("openai/gpt-4o") is False

    def test_strip_anthropic_prefix(self) -> None:
        assert _strip_anthropic_prefix("anthropic/claude-sonnet-4.6") == "claude-sonnet-4.6"
        assert _strip_anthropic_prefix("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
        # Non-anthropic prefixes are left alone
        assert (
            _strip_anthropic_prefix("openrouter/anthropic/claude-sonnet-4.6")
            == "openrouter/anthropic/claude-sonnet-4.6"
        )

    def test_non_anthropic_model_routes_via_gptme(self) -> None:
        """Non-Anthropic-direct models call _judge_via_gptme, not _judge_via_anthropic_direct."""
        with (
            patch(
                "gptme_sessions.judge._judge_via_gptme",
                return_value={"score": 0.6, "reason": "ok", "model": "openrouter/x"},
            ) as mock_gptme,
            patch("gptme_sessions.judge._judge_via_anthropic_direct") as mock_direct,
        ):
            result = judge_session(
                "journal text", category="code", model="openrouter/anthropic/claude-sonnet-4.6"
            )
        assert result is not None
        assert result["score"] == 0.6
        mock_gptme.assert_called_once()
        mock_direct.assert_not_called()

    def test_anthropic_direct_model_routes_via_anthropic(self) -> None:
        """Bare Anthropic IDs call _judge_via_anthropic_direct, not _judge_via_gptme."""
        with (
            patch(
                "gptme_sessions.judge._judge_via_anthropic_direct",
                return_value={"score": 0.7, "reason": "ok", "model": "claude-haiku-4-5-20251001"},
            ) as mock_direct,
            patch("gptme_sessions.judge._judge_via_gptme") as mock_gptme,
        ):
            result = judge_session("journal text", category="code")
        assert result is not None
        assert result["score"] == 0.7
        mock_direct.assert_called_once()
        mock_gptme.assert_not_called()

    def test_gptme_path_returns_none_when_gptme_missing(self) -> None:
        """When gptme is not installed, non-Anthropic models get None cleanly."""
        with patch.dict(
            "sys.modules",
            {"gptme": None, "gptme.init": None, "gptme.llm": None, "gptme.message": None},
        ):
            result = judge_session(
                "journal text", category="code", model="openrouter/anthropic/claude-sonnet-4.6"
            )
        assert result is None

    def test_resolve_openrouter_api_key_prefers_scoped_env(self) -> None:
        env = {
            "OPENROUTER_API_KEY_JUDGE": "judge-key",
            "OPENROUTER_API_KEY": "shared-key",
        }

        assert _resolve_openrouter_api_key("judge", environ=env) == "judge-key"

    def test_resolve_openrouter_api_key_reads_config_local(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[env]\nOPENROUTER_API_KEY = "shared-key"\n', encoding="utf-8")
        config_local = tmp_path / "config.local.toml"
        config_local.write_text(
            '[env]\nOPENROUTER_API_KEY_JUDGE = "judge-key"\n',
            encoding="utf-8",
        )

        assert (
            _resolve_openrouter_api_key(
                "judge",
                environ={},
                config_paths=(config, config_local),
            )
            == "judge-key"
        )

    def test_get_api_key_reads_config_local(self, tmp_path: Path, monkeypatch) -> None:
        """_get_api_key() falls back to config.local.toml, not just config.toml."""
        config = tmp_path / "config.toml"
        config.write_text("[env]\n", encoding="utf-8")
        config_local = tmp_path / "config.local.toml"
        config_local.write_text('[env]\nANTHROPIC_API_KEY = "local-key"\n', encoding="utf-8")

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _get_api_key(config_paths=(config, config_local)) == "local-key"

    def test_judge_openrouter_env_promotes_scoped_key(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "gptme_sessions.judge._resolve_openrouter_api_key",
            lambda *args, **kwargs: "judge-key",
        )
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        seen: list[str | None] = []
        with _judge_openrouter_env("openrouter/qwen/qwen3-next-80b"):
            seen.append(os.environ.get("OPENROUTER_API_KEY"))

        assert seen == ["judge-key"]
        assert "OPENROUTER_API_KEY" not in os.environ

    def test_normalize_judge_verdict_attaches_meta(self) -> None:
        normalized = normalize_judge_verdict(
            {
                "score": 0.74,
                "reason": "Meaningful progress",
                "model": "openai-subscription/gpt-5.4",
            }
        )

        assert normalized == {
            "score": 0.74,
            "reason": "Meaningful progress",
            "model": "openai-subscription/gpt-5.4",
            "alignment_score": None,
            "pivot_verdict": None,
            "meta": {
                "backend": "gptme-fallback",
                "judge_version": JUDGE_VERSION,
            },
        }

    def test_normalize_judge_verdict_preserves_alignment_fields(self) -> None:
        """Phase 3: alignment_score and pivot_verdict survive normalization."""
        normalized = normalize_judge_verdict(
            {
                "score": 0.65,
                "reason": "Partial progress, well-pivoted",
                "model": "claude-haiku-4-5",
                "alignment_score": 0.40,
                "pivot_verdict": "pivot",
            }
        )

        assert normalized["score"] == 0.65
        assert normalized["alignment_score"] == 0.40
        assert normalized["pivot_verdict"] == "pivot"

    def test_normalize_judge_verdict_rejects_out_of_range_alignment(self) -> None:
        """alignment_score outside 0.0-1.0 is coerced to None."""
        normalized = normalize_judge_verdict(
            {
                "score": 0.50,
                "reason": "test",
                "model": "test",
                "alignment_score": 1.5,
                "pivot_verdict": "on_track",
            }
        )
        assert normalized["alignment_score"] is None
        assert normalized["pivot_verdict"] == "on_track"

    def test_normalize_judge_verdict_rejects_invalid_pivot_verdict(self) -> None:
        """pivot_verdict outside recognised values is coerced to None."""
        normalized = normalize_judge_verdict(
            {
                "score": 0.50,
                "reason": "test",
                "model": "test",
                "alignment_score": 0.75,
                "pivot_verdict": "good_pivot",
            }
        )
        assert normalized["alignment_score"] == 0.75
        assert normalized["pivot_verdict"] is None


class TestSessionRecordJudgeFields:
    """Tests for LLM judge fields on SessionRecord."""

    def test_judge_fields_default_to_none(self) -> None:
        """New records have judge fields as None by default."""
        record = SessionRecord(session_id="test")
        assert record.llm_judge_score is None
        assert record.llm_judge_reason is None
        assert record.llm_judge_model is None

    def test_judge_fields_serialization(self) -> None:
        """Judge fields survive round-trip serialization."""
        record = SessionRecord(
            session_id="test",
            llm_judge_score=0.75,
            llm_judge_reason="Good strategic work",
            llm_judge_model="claude-haiku-4-5-20251001",
        )
        d = record.to_dict()
        assert d["llm_judge_score"] == 0.75
        assert d["llm_judge_reason"] == "Good strategic work"
        assert d["llm_judge_model"] == "claude-haiku-4-5-20251001"

        # Round-trip via JSON
        json_str = record.to_json()
        restored = SessionRecord.from_dict(json.loads(json_str))
        assert restored.llm_judge_score == 0.75
        assert restored.llm_judge_reason == "Good strategic work"
        assert restored.llm_judge_model == "claude-haiku-4-5-20251001"

    def test_judge_fields_ignored_in_old_records(self) -> None:
        """Old records without judge fields load cleanly."""
        old_data = {
            "session_id": "abc123",
            "timestamp": "2026-03-07T12:00:00+00:00",
            "outcome": "productive",
        }
        record = SessionRecord.from_dict(old_data)
        assert record.llm_judge_score is None

    def test_writeback_helpers_store_alignment_meta(self, tmp_path: Path) -> None:
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        with patch(
            "gptme_sessions.judge.judge_session",
            return_value={
                "score": 0.74,
                "reason": "Real work shipped",
                "model": "openai-subscription/gpt-5.4",
            },
        ):
            result = judge_and_writeback(
                text="session text",
                category="code",
                goals="ship useful work",
                session_id="abc123",
                sessions_dir=tmp_path,
                model="openai-subscription/gpt-5.4",
            )

        assert result["status"] == "ok"
        updated = SessionStore(sessions_dir=tmp_path).load_all()[0]
        assert updated.grades["alignment"] == 0.74
        assert updated.grade_reasons["alignment"] == "Real work shipped"
        assert updated.llm_judge_score == 0.74
        assert updated.llm_judge_reason == "Real work shipped"
        assert updated.llm_judge_model == "openai-subscription/gpt-5.4"
        assert updated.to_dict()["llm_judge_meta"] == {
            "backend": "gptme-fallback",
            "judge_version": JUDGE_VERSION,
        }

    def test_writeback_populates_span_aggregates(self, tmp_path: Path) -> None:
        """write_alignment_grade opportunistically calls populate_span_aggregates."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        with (
            patch(
                "gptme_sessions.judge.judge_session",
                return_value={
                    "score": 0.74,
                    "reason": "Real work shipped",
                    "model": "openai-subscription/gpt-5.4",
                },
            ),
            patch.object(
                SessionRecord, "populate_span_aggregates", return_value=True
            ) as mock_populate,
        ):
            result = judge_and_writeback(
                text="session text",
                category="code",
                goals="ship useful work",
                session_id="abc123",
                sessions_dir=tmp_path,
                model="openai-subscription/gpt-5.4",
            )

        assert result["status"] == "ok"
        mock_populate.assert_called_once()

    def test_writeback_tolerates_span_aggregates_failure(self, tmp_path: Path) -> None:
        """Writeback still succeeds if span aggregation raises unexpectedly."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        with (
            patch(
                "gptme_sessions.judge.judge_session",
                return_value={
                    "score": 0.5,
                    "reason": "Did work",
                    "model": "openai-subscription/gpt-5.4",
                },
            ),
            patch.object(
                SessionRecord,
                "populate_span_aggregates",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = judge_and_writeback(
                text="session text",
                category="code",
                goals="ship useful work",
                session_id="abc123",
                sessions_dir=tmp_path,
                model="openai-subscription/gpt-5.4",
            )

        assert result["status"] == "ok"
        updated = SessionStore(sessions_dir=tmp_path).load_all()[0]
        assert updated.grades["alignment"] == 0.5

    def test_judge_and_writeback_reports_missing_record(self, tmp_path: Path) -> None:
        with patch(
            "gptme_sessions.judge.judge_session",
            return_value={
                "score": 0.5,
                "reason": "Did work",
                "model": "openai-subscription/gpt-5.4",
            },
        ):
            result = judge_and_writeback(
                text="session text",
                category="code",
                goals="ship useful work",
                session_id="missing",
                sessions_dir=tmp_path,
                model="openai-subscription/gpt-5.4",
            )

        assert result == {
            "status": "no_record",
            "score": 0.5,
            "reason": "Did work",
            "model": "openai-subscription/gpt-5.4",
            "alignment_score": None,
            "pivot_verdict": None,
            "meta": {
                "backend": "gptme-fallback",
                "judge_version": JUDGE_VERSION,
            },
        }

    def test_writeback_persists_alignment_fields(self, tmp_path: Path) -> None:
        """Phase 3: alignment_score and pivot_verdict survive into legacy_fields."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        with patch(
            "gptme_sessions.judge.judge_session",
            return_value={
                "score": 0.72,
                "reason": "Good work with justified pivot",
                "model": "claude-haiku-4-5",
                "alignment_score": 0.35,
                "pivot_verdict": "pivot",
            },
        ):
            result = judge_and_writeback(
                text="session text",
                category="cross-repo",
                goals="ship useful work",
                session_id="abc123",
                sessions_dir=tmp_path,
            )

        assert result["status"] == "ok"
        updated = SessionStore(sessions_dir=tmp_path).load_all()[0]
        legacy_fields = getattr(updated, "_legacy_fields", {})
        assert legacy_fields["alignment_score"] == 0.35
        assert legacy_fields["pivot_verdict"] == "pivot"

    def test_writeback_skips_legacy_when_alignment_absent(self, tmp_path: Path) -> None:
        """When the judge returns no alignment fields, legacy_fields stays clean."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        with patch(
            "gptme_sessions.judge.judge_session",
            return_value={
                "score": 0.72,
                "reason": "Good work",
                "model": "claude-haiku-4-5",
            },
        ):
            result = judge_and_writeback(
                text="session text",
                category="code",
                goals="ship useful work",
                session_id="abc123",
                sessions_dir=tmp_path,
            )

        assert result["status"] == "ok"
        updated = SessionStore(sessions_dir=tmp_path).load_all()[0]
        legacy_fields = getattr(updated, "_legacy_fields", {})
        assert "alignment_score" not in legacy_fields
        assert "pivot_verdict" not in legacy_fields


class TestJudgeCLI:
    """Tests for the judge CLI command."""

    def test_judge_command_exists(self) -> None:
        """The 'judge' command is registered in the CLI group."""
        from gptme_sessions.cli import cli

        assert "judge" in cli.commands

    def test_signals_has_llm_judge_flag(self) -> None:
        """The 'signals' command has --llm-judge flag."""
        from gptme_sessions.cli import cli

        signals_cmd = cli.commands["signals"]
        param_names = [p.name for p in signals_cmd.params]
        assert "llm_judge" in param_names
        assert "goals" in param_names

    def test_judge_and_signals_have_model_flag(self) -> None:
        """Both 'judge' and 'signals' expose a --model flag for judge routing."""
        from gptme_sessions.cli import cli

        for cmd_name in ("judge", "signals"):
            params = [p for p in cli.commands[cmd_name].params if p.name == "judge_model"]
            assert params, f"{cmd_name!r} is missing --model flag"
            flag = params[0]
            assert "--model" in flag.opts

    @pytest.mark.skipif(
        getattr(os, "getuid", lambda: -1)() == 0,
        reason="chmod(0o000) has no effect as root",
    )
    def test_judge_skips_unreadable_files(self, tmp_path: "Path") -> None:
        """A single unreadable journal file is skipped; other entries are processed."""
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        # Good entry — must match the glob "autonomous-session-*.md"
        good_day = journal_dir / "2026-03-07"
        good_day.mkdir()
        good_entry = good_day / "autonomous-session-abc123.md"
        good_entry.write_text("## Session\nDid some work", encoding="utf-8")

        # Bad entry — unreadable (no read permission)
        bad_day = journal_dir / "2026-03-06"
        bad_day.mkdir()
        bad_entry = bad_day / "autonomous-session-def456.md"
        bad_entry.write_text("corrupt", encoding="utf-8")
        bad_entry.chmod(0o000)

        runner = CliRunner()
        try:
            result = runner.invoke(
                cli,
                ["judge", "--journal-dir", str(journal_dir), "--dry-run"],
            )
            # Should not crash; good entry should be processed, bad skipped
            assert result.exit_code == 0, result.output
            # Good entry was found (not "No autonomous session journal entries found")
            assert "2026-03-07" in result.output or "abc123" in result.output
        finally:
            bad_entry.chmod(0o644)  # restore so tmp_path cleanup works

    def test_judge_update_store_writes_alignment_grade(self, tmp_path: "Path") -> None:
        """judge --update-store keeps legacy judge fields and grades.alignment in sync."""
        from click.testing import CliRunner
        from gptme_sessions.cli import cli
        from gptme_sessions.store import SessionStore

        journal_dir = tmp_path / "journal" / "2026-03-07"
        journal_dir.mkdir(parents=True)
        (journal_dir / "autonomous-session-abc123.md").write_text(
            "## Session\nDid real work.\n",
            encoding="utf-8",
        )

        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir)
        store.append(SessionRecord(session_id="abc123", outcome="productive"))

        runner = CliRunner()
        with patch(
            "gptme_sessions.judge.judge_session",
            return_value={
                "score": 0.81,
                "reason": "Meaningful progress on the active task.",
                "model": "claude-haiku-4-5",
            },
        ):
            result = runner.invoke(
                cli,
                [
                    "--sessions-dir",
                    str(sessions_dir),
                    "judge",
                    "--journal-dir",
                    str(tmp_path / "journal"),
                    "--update-store",
                ],
            )

        assert result.exit_code == 0, result.output
        record = SessionStore(sessions_dir=sessions_dir).load_all()[0]
        assert record.llm_judge_score == 0.81
        assert record.llm_judge_reason == "Meaningful progress on the active task."
        assert record.llm_judge_model == "claude-haiku-4-5"
        assert record.grades == {"alignment": 0.81}
        assert record.grade_reasons == {"alignment": "Meaningful progress on the active task."}

    def test_classify_update_store_writes_alignment_grade(self, tmp_path: "Path") -> None:
        """classify --judge --update-store mirrors judge output into grades.alignment."""
        from click.testing import CliRunner
        from gptme_sessions.classification import ClassificationResult
        from gptme_sessions.cli import cli
        from gptme_sessions.store import SessionStore

        journal_dir = tmp_path / "journal" / "2026-03-07"
        journal_dir.mkdir(parents=True)
        (journal_dir / "autonomous-session-def456.md").write_text(
            "## Session\nFixed a real bug.\n",
            encoding="utf-8",
        )

        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir)
        store.append(SessionRecord(session_id="def456", outcome="productive"))

        runner = CliRunner()
        with patch(
            "gptme_sessions.classification.judge_and_classify",
            return_value=(
                ClassificationResult(
                    category="code",
                    confidence=0.93,
                    productive=True,
                    classifier="llm",
                ),
                {
                    "score": 0.77,
                    "reason": "Good progress on core implementation.",
                    "model": "claude-haiku-4-5",
                },
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "--sessions-dir",
                    str(sessions_dir),
                    "classify",
                    "--journal-dir",
                    str(tmp_path / "journal"),
                    "--judge",
                    "--update-store",
                ],
            )

        assert result.exit_code == 0, result.output
        record = SessionStore(sessions_dir=sessions_dir).load_all()[0]
        assert record.category == "code"
        assert record.llm_judge_score == 0.77
        assert record.grades == {"alignment": 0.77}
        assert record.grade_reasons == {"alignment": "Good progress on core implementation."}
