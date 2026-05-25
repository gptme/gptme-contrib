from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TWITTER_PATH = REPO_ROOT / "scripts" / "twitter" / "twitter.py"
_MISSING = object()


def _make_pkg(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []
    return mod


def _load_twitter_module() -> tuple[Any, dict[str, Any]]:
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
    click_stub.IntRange = lambda *args, **kwargs: None

    tweepy_stub: Any = types.ModuleType("tweepy")
    tweepy_stub.Client = object
    tweepy_stub.TweepyException = Exception
    tweepy_stub.Tweet = object
    tweepy_stub.Response = object

    dotenv_stub: Any = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    dotenv_stub.find_dotenv = lambda *args, **kwargs: ""

    auth_stub: Any = types.ModuleType("gptmail.communication_utils.auth")
    auth_stub.refresh_twitter_token_if_needed = lambda *args, **kwargs: (None, None)
    auth_stub.run_oauth_callback = lambda *args, **kwargs: (None, None)
    auth_stub.save_tokens_to_env = lambda *args, **kwargs: None

    auth_oauth_stub: Any = types.ModuleType("gptmail.communication_utils.auth.oauth")
    auth_oauth_stub.OAuthManager = SimpleNamespace(
        for_twitter=lambda *args, **kwargs: None
    )

    auth_tokens_stub: Any = types.ModuleType("gptmail.communication_utils.auth.tokens")
    auth_tokens_stub.TokenInfo = SimpleNamespace

    messaging_stub: Any = types.ModuleType("gptmail.communication_utils.messaging")
    messaging_stub.split_thread = lambda text: [SimpleNamespace(text=text)]

    rich_stub = _make_pkg("rich")
    rich_console_stub: Any = types.ModuleType("rich.console")
    rich_console_stub.Console = lambda *args, **kwargs: SimpleNamespace(
        print=lambda *print_args, **print_kwargs: None
    )

    gptmail_stub = _make_pkg("gptmail")
    gptmail_comm_stub = _make_pkg("gptmail.communication_utils")

    stubbed_modules: dict[str, Any] = {
        "click": click_stub,
        "tweepy": tweepy_stub,
        "dotenv": dotenv_stub,
        "rich": rich_stub,
        "rich.console": rich_console_stub,
        "gptmail": gptmail_stub,
        "gptmail.communication_utils": gptmail_comm_stub,
        "gptmail.communication_utils.auth": auth_stub,
        "gptmail.communication_utils.auth.oauth": auth_oauth_stub,
        "gptmail.communication_utils.auth.tokens": auth_tokens_stub,
        "gptmail.communication_utils.messaging": messaging_stub,
    }

    original_modules = {
        name: sys.modules.get(name, _MISSING) for name in stubbed_modules
    }
    for name, stub in stubbed_modules.items():
        sys.modules[name] = stub

    spec = importlib.util.spec_from_file_location(
        "twitter_post_guard_under_test", TWITTER_PATH
    )
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, original_modules


@pytest.fixture(scope="module")
def twitter_module() -> Generator[Any, None, None]:
    module, original_modules = _load_twitter_module()
    yield module
    sys.modules.pop("twitter_post_guard_under_test", None)
    for key, original in original_modules.items():
        if original is _MISSING:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original


def test_post_aborts_before_single_tweet_with_dead_url(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    monkeypatch.setattr(
        twitter_module,
        "validate_urls_in_text",
        lambda text: [("https://timetobuildbob.github.io/blog/missing/", 404)],
    )
    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True: SimpleNamespace(
            create_tweet=lambda **kwargs: posted.append(kwargs["text"])
        ),
    )

    with pytest.raises(SystemExit):
        twitter_module.post(
            "https://timetobuildbob.github.io/blog/missing/", None, False
        )

    assert posted == []


def test_post_aborts_before_thread_with_dead_followup_url(
    twitter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    monkeypatch.setattr(
        twitter_module,
        "split_thread",
        lambda text: [
            SimpleNamespace(text="main tweet"),
            SimpleNamespace(text="https://timetobuildbob.github.io/blog/missing/"),
        ],
    )
    monkeypatch.setattr(
        twitter_module,
        "validate_urls_in_text",
        lambda text: (
            [("https://timetobuildbob.github.io/blog/missing/", 404)]
            if "missing" in text
            else []
        ),
    )
    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True: SimpleNamespace(
            create_tweet=lambda **kwargs: posted.append(kwargs["text"])
        ),
    )

    with pytest.raises(SystemExit):
        twitter_module.post("ignored", None, True)

    assert posted == []
