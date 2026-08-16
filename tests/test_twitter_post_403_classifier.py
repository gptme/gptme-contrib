"""Regression tests for the post-403 classifier and quarantine behavior.

Background: a non-cap 403 (cashtag / dollar-amount content rejection) was being
treated by post-approved-tweets.sh as evidence of a spend cap, refreshing the cap
flag every ~20h and leaving one poison tweet impersonating an active spend cap
forever. workflow.py now distinguishes cap vs non-cap 403s at the call site and
quarantines non-cap 403s so they drain out of the approved queue.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / "scripts" / "twitter" / "workflow.py"
_MISSING = object()


def _make_pkg(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []
    return mod


def _load_workflow_module() -> tuple[Any, dict[str, Any]]:
    class _Operation:
        def complete(self, *args, **kwargs) -> None:
            return None

    class _MetricsCollector:
        def start_operation(self, *args, **kwargs) -> _Operation:
            return _Operation()

    monitoring_stub: Any = types.ModuleType("gptmail.communication_utils.monitoring")
    monitoring_stub.MetricsCollector = _MetricsCollector
    monitoring_stub.get_logger = lambda *args, **kwargs: logging.getLogger(
        "twitter-test"
    )

    redact_stub: Any = types.ModuleType("gptmail.communication_utils.outbound_redact")
    redact_stub.guard_outbound = lambda *args, **kwargs: True

    gptmail_stub = _make_pkg("gptmail")
    gptmail_comm_stub = _make_pkg("gptmail.communication_utils")

    gptme_stub = _make_pkg("gptme")
    gptme_init_stub: Any = types.ModuleType("gptme.init")
    gptme_init_stub.init = lambda *args, **kwargs: None

    dotenv_stub: Any = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None

    click_stub: Any = types.ModuleType("click")

    def _passthrough_decorator(*args, **kwargs):
        def _decorator(func):
            return func

        return _decorator

    def _group_decorator(*args, **kwargs):
        def _decorator(func):
            func.command = _passthrough_decorator
            return func

        return _decorator

    click_stub.group = _group_decorator
    click_stub.command = _passthrough_decorator
    click_stub.option = _passthrough_decorator
    click_stub.argument = _passthrough_decorator
    click_stub.Choice = lambda choices: choices
    click_stub.Path = lambda **kwargs: kwargs
    click_stub.echo = lambda *args, **kwargs: None

    rich_stub = _make_pkg("rich")
    rich_console_stub: Any = types.ModuleType("rich.console")
    rich_console_stub.Console = lambda *args, **kwargs: SimpleNamespace(
        print=lambda *print_args, **print_kwargs: None
    )
    rich_prompt_stub: Any = types.ModuleType("rich.prompt")
    rich_prompt_stub.Confirm = SimpleNamespace(ask=lambda *args, **kwargs: True)
    rich_prompt_stub.Prompt = SimpleNamespace(ask=lambda *args, **kwargs: "")

    trusted_users_stub: Any = types.ModuleType("trusted_users")
    trusted_users_stub.is_trusted_user = lambda *args, **kwargs: False

    twitter_pkg_stub = _make_pkg("twitter")
    twitter_llm_stub: Any = types.ModuleType("twitter.llm")
    twitter_llm_stub.EvaluationResponse = type("EvaluationResponse", (), {})
    twitter_llm_stub.TweetResponse = type("TweetResponse", (), {})
    twitter_llm_stub.process_tweet = lambda *args, **kwargs: None
    twitter_llm_stub.verify_draft = lambda *args, **kwargs: (True, None)
    twitter_llm_stub._unescape_literal_newlines = lambda text: text.replace("\\n", "\n")

    twitter_api_stub: Any = types.ModuleType("twitter.twitter")
    twitter_api_stub.cached_get_me = lambda *args, **kwargs: SimpleNamespace(
        data=SimpleNamespace(id=0)
    )
    twitter_api_stub.load_twitter_client = lambda *args, **kwargs: None
    twitter_api_stub._find_placeholder_in_text = lambda *args, **kwargs: None

    stubbed_modules: dict[str, Any] = {
        "gptmail": gptmail_stub,
        "gptmail.communication_utils": gptmail_comm_stub,
        "gptmail.communication_utils.monitoring": monitoring_stub,
        "gptmail.communication_utils.outbound_redact": redact_stub,
        "gptme": gptme_stub,
        "gptme.init": gptme_init_stub,
        "dotenv": dotenv_stub,
        "click": click_stub,
        "rich": rich_stub,
        "rich.console": rich_console_stub,
        "rich.prompt": rich_prompt_stub,
        "trusted_users": trusted_users_stub,
        "twitter": twitter_pkg_stub,
        "twitter.llm": twitter_llm_stub,
        "twitter.twitter": twitter_api_stub,
    }

    original_modules = {
        name: sys.modules.get(name, _MISSING) for name in stubbed_modules
    }
    for name, stub in stubbed_modules.items():
        sys.modules[name] = stub

    spec = importlib.util.spec_from_file_location(
        "twitter_workflow_under_test", WORKFLOW_PATH
    )
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, original_modules


@pytest.fixture(scope="module")
def workflow_module() -> Generator[Any, None, None]:
    module, original_modules = _load_workflow_module()
    yield module
    sys.modules.pop("twitter_workflow_under_test", None)
    for key, original in original_modules.items():
        if original is _MISSING:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original


class TestClassifyTweetPostError:
    def test_spend_cap_403_classified_as_cap(self, workflow_module):
        err = Exception("403 Forbidden: Your monthly spend cap has been reached")
        assert workflow_module._classify_tweet_post_error(err) == "cap"

    def test_spend_cap_message_without_403_is_cap(self, workflow_module):
        err = Exception("your monthly spend cap has been reached")
        assert workflow_module._classify_tweet_post_error(err) == "cap"

    def test_cashtag_403_classified_permanent(self, workflow_module):
        # This is the poison-tweet case: content-rejection 403 that will never
        # succeed on retry, but must NOT be mistaken for a spend cap.
        err = Exception(
            "403 Forbidden: your post was rejected, you may have entered a "
            "dollar amount or a cashtag"
        )
        assert workflow_module._classify_tweet_post_error(err) == "permanent"

    def test_generic_forbidden_403_is_permanent(self, workflow_module):
        err = Exception("403 Forbidden: policy violation")
        assert workflow_module._classify_tweet_post_error(err) == "permanent"

    def test_typed_tweepy_forbidden_is_permanent(self, workflow_module):
        # The lazy tweepy.errors import may not be available in a bare test env;
        # a typed Forbidden instance must still classify as permanent either via
        # the isinstance check or the status-text fallback.
        try:
            from tweepy.errors import Forbidden
        except Exception:  # pragma: no cover - tweepy not installed in bare env
            pytest.skip("tweepy not installed")

        class _FakeResponse:
            status_code = 403
            status = "Forbidden"
            reason = "Forbidden"

            def json(self) -> dict[str, object]:
                return {"errors": [{"code": 403, "message": "Forbidden"}]}

        err = Forbidden(_FakeResponse())
        assert workflow_module._classify_tweet_post_error(err) == "permanent"

    def test_non_403_error_is_other(self, workflow_module):
        err = Exception("Connection reset by peer")
        assert workflow_module._classify_tweet_post_error(err) == "other"

    def test_rate_limit_429_is_other(self, workflow_module):
        err = Exception("429 Too Many Requests")
        assert workflow_module._classify_tweet_post_error(err) == "other"
