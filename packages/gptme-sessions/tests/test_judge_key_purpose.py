"""Working-session judges must not borrow isolated EVAL keys.

Synthetic credentials and mocked gptme transport only. No real config,
network, or inference.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gptme_sessions.judge import (
    CONTEXT_FALLBACKS,
    _candidate_openrouter_env_var_names,
    _judge_openrouter_env,
    _resolve_openrouter_api_key,
    judge_session,
)

SYNTHETIC_EVAL = "synthetic-eval"
SYNTHETIC_WORKING = "synthetic-working"
SYNTHETIC_ALIAS = "synthetic-alias"
SYNTHETIC_SHARED = "synthetic-shared"
OPENROUTER_MODEL = "openrouter/anthropic/claude-haiku-4-5"
NON_OPENROUTER_MODEL = "openai-subscription/gpt-5.4"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
WORKING_CONTEXTS = ("JUDGE", "LLM_JUDGE")
SCOPED_KEY_VARS = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY_JUDGE",
    "OPENROUTER_API_KEY_LLM_JUDGE",
    "OPENROUTER_API_KEY_EVAL",
)


@pytest.fixture
def isolate_openrouter_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep resolver and gptme promotion off the operator's real keys/config."""
    monkeypatch.setattr("gptme_sessions.judge.CONFIG_PATHS", ())
    for name in SCOPED_KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def _eval_only_config(tmp_path: Path) -> Path:
    config = tmp_path / "synthetic.toml"
    config.write_text(
        f'[env]\nOPENROUTER_API_KEY_EVAL = "{SYNTHETIC_EVAL}"\n',
        encoding="utf-8",
    )
    return config


class TestWorkingFallbackCandidates:
    def test_working_chains_exclude_eval(self) -> None:
        assert CONTEXT_FALLBACKS["JUDGE"] == ("JUDGE", "LLM_JUDGE")
        assert CONTEXT_FALLBACKS["LLM_JUDGE"] == ("LLM_JUDGE", "JUDGE")
        assert CONTEXT_FALLBACKS["EVAL"] == ("EVAL", "JUDGE", "LLM_JUDGE")

    @pytest.mark.parametrize("context", WORKING_CONTEXTS)
    def test_working_candidates_skip_eval_and_keep_shared(self, context: str) -> None:
        names = _candidate_openrouter_env_var_names(context)
        assert "OPENROUTER_API_KEY_EVAL" not in names
        assert f"OPENROUTER_API_KEY_{context}" in names
        assert names[-1] == "OPENROUTER_API_KEY"

    def test_eval_candidates_keep_working_fallback(self) -> None:
        names = _candidate_openrouter_env_var_names("EVAL")
        assert names[:3] == [
            "OPENROUTER_API_KEY_EVAL",
            "OPENROUTER_API_KEY_JUDGE",
            "OPENROUTER_API_KEY_LLM_JUDGE",
        ]
        assert names[-1] == "OPENROUTER_API_KEY"


class TestWorkingResolverIsolation:
    @pytest.mark.parametrize("context", WORKING_CONTEXTS)
    def test_eval_only_env_does_not_resolve(self, context: str) -> None:
        assert (
            _resolve_openrouter_api_key(
                context,
                environ={"OPENROUTER_API_KEY_EVAL": SYNTHETIC_EVAL},
                config_paths=(),
            )
            is None
        )

    @pytest.mark.parametrize("context", WORKING_CONTEXTS)
    def test_eval_only_config_does_not_resolve(self, tmp_path: Path, context: str) -> None:
        assert (
            _resolve_openrouter_api_key(
                context,
                environ={},
                config_paths=(_eval_only_config(tmp_path),),
            )
            is None
        )

    @pytest.mark.parametrize("context", WORKING_CONTEXTS)
    def test_eval_env_does_not_outrank_config_working_key(
        self, tmp_path: Path, context: str
    ) -> None:
        config = tmp_path / "synthetic.toml"
        config.write_text(
            f'[env]\nOPENROUTER_API_KEY_{context} = "{SYNTHETIC_WORKING}"\n',
            encoding="utf-8",
        )
        assert (
            _resolve_openrouter_api_key(
                context,
                environ={"OPENROUTER_API_KEY_EVAL": SYNTHETIC_EVAL},
                config_paths=(config,),
            )
            == SYNTHETIC_WORKING
        )

    def test_env_alias_still_outranks_config_judge(self, tmp_path: Path) -> None:
        """Existing env-then-config order: a scoped env alias beats config JUDGE."""
        config = tmp_path / "synthetic.toml"
        config.write_text(
            f'[env]\nOPENROUTER_API_KEY_JUDGE = "{SYNTHETIC_WORKING}"\n',
            encoding="utf-8",
        )
        assert (
            _resolve_openrouter_api_key(
                "JUDGE",
                environ={"OPENROUTER_API_KEY_LLM_JUDGE": SYNTHETIC_ALIAS},
                config_paths=(config,),
            )
            == SYNTHETIC_ALIAS
        )

    def test_working_alias_and_shared_controls(self) -> None:
        assert (
            _resolve_openrouter_api_key(
                "JUDGE",
                environ={"OPENROUTER_API_KEY_LLM_JUDGE": SYNTHETIC_ALIAS},
                config_paths=(),
            )
            == SYNTHETIC_ALIAS
        )
        assert (
            _resolve_openrouter_api_key(
                "LLM_JUDGE",
                environ={"OPENROUTER_API_KEY_JUDGE": SYNTHETIC_WORKING},
                config_paths=(),
            )
            == SYNTHETIC_WORKING
        )
        assert (
            _resolve_openrouter_api_key(
                "JUDGE",
                environ={
                    "OPENROUTER_API_KEY": SYNTHETIC_SHARED,
                    "OPENROUTER_API_KEY_EVAL": SYNTHETIC_EVAL,
                },
                config_paths=(),
            )
            == SYNTHETIC_SHARED
        )

    def test_eval_keeps_own_key_and_working_fallback(self) -> None:
        assert (
            _resolve_openrouter_api_key(
                "EVAL",
                environ={
                    "OPENROUTER_API_KEY_EVAL": SYNTHETIC_EVAL,
                    "OPENROUTER_API_KEY_JUDGE": SYNTHETIC_WORKING,
                },
                config_paths=(),
            )
            == SYNTHETIC_EVAL
        )
        assert (
            _resolve_openrouter_api_key(
                "EVAL",
                environ={"OPENROUTER_API_KEY_JUDGE": SYNTHETIC_WORKING},
                config_paths=(),
            )
            == SYNTHETIC_WORKING
        )


class TestDelegatedOpenRouterPromotion:
    def test_eval_only_env_is_not_promoted(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        seen: list[str | None] = []
        with _judge_openrouter_env(OPENROUTER_MODEL):
            seen.append(os.environ.get("OPENROUTER_API_KEY"))
        assert SYNTHETIC_EVAL not in seen
        assert seen == [None]
        assert os.environ.get("OPENROUTER_API_KEY_EVAL") == SYNTHETIC_EVAL
        assert "OPENROUTER_API_KEY" not in os.environ

    def test_eval_only_config_is_not_promoted(
        self,
        isolate_openrouter_sources: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "gptme_sessions.judge.CONFIG_PATHS",
            (_eval_only_config(tmp_path),),
        )
        seen: list[str | None] = []
        with _judge_openrouter_env(OPENROUTER_MODEL):
            seen.append(os.environ.get("OPENROUTER_API_KEY"))
        assert seen == [None]
        assert "OPENROUTER_API_KEY" not in os.environ

    def test_working_key_is_promoted_and_restored(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_JUDGE", SYNTHETIC_WORKING)
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        seen: list[str | None] = []
        with _judge_openrouter_env(OPENROUTER_MODEL):
            seen.append(os.environ.get("OPENROUTER_API_KEY"))
        assert seen == [SYNTHETIC_WORKING]
        assert "OPENROUTER_API_KEY" not in os.environ
        assert os.environ.get("OPENROUTER_API_KEY_EVAL") == SYNTHETIC_EVAL

    def test_non_openrouter_model_does_not_promote_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        seen: list[str | None] = []
        with _judge_openrouter_env(NON_OPENROUTER_MODEL):
            seen.append(os.environ.get("OPENROUTER_API_KEY"))
        assert seen == [None]
        assert "OPENROUTER_API_KEY" not in os.environ


def _capture_gptme_keys() -> tuple[list[dict[str, str | None]], object, object]:
    captured: list[dict[str, str | None]] = []

    def fake_init(*args: object, **kwargs: object) -> None:
        captured.append(
            {
                "stage": "init",
                "shared": os.environ.get("OPENROUTER_API_KEY"),
                "eval": os.environ.get("OPENROUTER_API_KEY_EVAL"),
            }
        )

    def fake_reply(*args: object, **kwargs: object) -> SimpleNamespace:
        captured.append(
            {
                "stage": "reply",
                "shared": os.environ.get("OPENROUTER_API_KEY"),
                "eval": os.environ.get("OPENROUTER_API_KEY_EVAL"),
            }
        )
        return SimpleNamespace(content='{"score": 0.51, "reason": "synthetic"}')

    return captured, fake_init, fake_reply


class TestDelegatedInitReply:
    def test_eval_only_openrouter_path_does_not_transmit_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        captured, fake_init, fake_reply = _capture_gptme_keys()
        with (
            patch("gptme.init.init", fake_init),
            patch("gptme.llm.reply", fake_reply),
        ):
            result = judge_session(
                "working journal text",
                category="code",
                model=OPENROUTER_MODEL,
            )
        assert captured
        assert all(row["shared"] != SYNTHETIC_EVAL for row in captured)
        assert all(row["shared"] in (None, "") for row in captured)
        if result is not None:
            assert result.get("score") == 0.51

    def test_working_key_is_used_instead_of_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_JUDGE", SYNTHETIC_WORKING)
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        captured, fake_init, fake_reply = _capture_gptme_keys()
        with (
            patch("gptme.init.init", fake_init),
            patch("gptme.llm.reply", fake_reply),
        ):
            result = judge_session(
                "working journal text",
                category="code",
                model=OPENROUTER_MODEL,
            )
        assert result is not None
        assert result["score"] == 0.51
        assert [row["shared"] for row in captured] == [
            SYNTHETIC_WORKING,
            SYNTHETIC_WORKING,
        ]
        assert os.environ.get("OPENROUTER_API_KEY_JUDGE") == SYNTHETIC_WORKING
        assert "OPENROUTER_API_KEY" not in os.environ

    def test_alias_is_used_instead_of_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_LLM_JUDGE", SYNTHETIC_ALIAS)
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        captured, fake_init, fake_reply = _capture_gptme_keys()
        with (
            patch("gptme.init.init", fake_init),
            patch("gptme.llm.reply", fake_reply),
        ):
            judge_session(
                "working journal text",
                category="code",
                model=OPENROUTER_MODEL,
            )
        assert [row["shared"] for row in captured] == [SYNTHETIC_ALIAS, SYNTHETIC_ALIAS]

    def test_non_openrouter_gptme_path_does_not_promote_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        captured, fake_init, fake_reply = _capture_gptme_keys()
        with (
            patch("gptme.init.init", fake_init),
            patch("gptme.llm.reply", fake_reply),
        ):
            judge_session(
                "working journal text",
                category="code",
                model=NON_OPENROUTER_MODEL,
            )
        assert captured
        assert all(row["shared"] != SYNTHETIC_EVAL for row in captured)
        assert all(row["shared"] in (None, "") for row in captured)

    def test_anthropic_direct_path_ignores_eval(
        self, isolate_openrouter_sources: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_EVAL", SYNTHETIC_EVAL)
        with (
            patch(
                "gptme_sessions.judge._judge_via_anthropic_direct",
                return_value={
                    "score": 0.7,
                    "reason": "ok",
                    "model": ANTHROPIC_MODEL,
                },
            ) as mock_direct,
            patch("gptme_sessions.judge._judge_via_gptme") as mock_gptme,
        ):
            result = judge_session(
                "working journal text",
                category="code",
                model=ANTHROPIC_MODEL,
            )
        assert result is not None
        assert result["score"] == 0.7
        mock_direct.assert_called_once()
        mock_gptme.assert_not_called()
