"""Tests for Twitter eval prompt builder — specifically the identity-confusion fix.

Regression tests for gptme/gptme-contrib#663 (ErikBjare/bob#602):
when a tweet contains @{TWITTER_HANDLE}, the eval prompt must clarify that
the handle IS our account, otherwise the LLM may wrongly conclude the
mention is directed elsewhere and return IGNORE.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_PATH = REPO_ROOT / "scripts" / "twitter" / "llm.py"


def _load_llm_module() -> types.ModuleType:
    """Load scripts/twitter/llm.py with gptme dependencies stubbed out.

    llm.py imports `from gptme.llm import reply` and `from gptme.dirs import
    get_project_git_dir` at module load. We don't exercise those code paths
    in these tests, so we stub them before importing.
    """

    # Stub gptme as a package — llm.py imports from several submodules
    # (gptme.llm.reply, gptme.llm.models.get_default_model, gptme.dirs, gptme.message)
    def _make_pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []  # marks as package
        return mod

    gptme_stub = _make_pkg("gptme")
    gptme_llm_stub = _make_pkg("gptme.llm")
    gptme_llm_stub.reply = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    gptme_llm_models_stub = types.ModuleType("gptme.llm.models")
    gptme_llm_models_stub.get_default_model = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    gptme_dirs_stub = types.ModuleType("gptme.dirs")
    gptme_dirs_stub.get_project_git_dir = lambda: Path("/tmp")  # type: ignore[attr-defined]
    gptme_message_stub = types.ModuleType("gptme.message")

    class _StubMessage:
        def __init__(self, role: str, content: str) -> None:
            self.role = role
            self.content = content

    gptme_message_stub.Message = _StubMessage  # type: ignore[attr-defined]
    gptme_prompts_stub = types.ModuleType("gptme.prompts")
    gptme_prompts_stub.prompt_workspace = lambda *args, **kwargs: iter([])  # type: ignore[attr-defined]
    rich_stub: Any = _make_pkg("rich")
    rich_console_stub: Any = types.ModuleType("rich.console")
    rich_console_stub.Console = lambda *args, **kwargs: types.SimpleNamespace(
        print=lambda *print_args, **print_kwargs: None
    )

    sys.modules.setdefault("gptme", gptme_stub)
    sys.modules.setdefault("gptme.llm", gptme_llm_stub)
    sys.modules.setdefault("gptme.llm.models", gptme_llm_models_stub)
    sys.modules.setdefault("gptme.dirs", gptme_dirs_stub)
    sys.modules.setdefault("gptme.message", gptme_message_stub)
    sys.modules.setdefault("gptme.prompts", gptme_prompts_stub)
    sys.modules.setdefault("rich", rich_stub)
    sys.modules.setdefault("rich.console", rich_console_stub)

    spec = importlib.util.spec_from_file_location("twitter_llm_under_test", LLM_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def llm_module() -> Generator[types.ModuleType, None, None]:
    # Track which keys we added so we can remove them after the module-scope fixture
    _STUB_KEYS = (
        "gptme",
        "gptme.llm",
        "gptme.llm.models",
        "gptme.dirs",
        "gptme.message",
        "gptme.prompts",
        "rich",
        "rich.console",
    )
    pre_existing = {k for k in _STUB_KEYS if k in sys.modules}
    module = _load_llm_module()
    yield module
    for key in _STUB_KEYS:
        if key not in pre_existing:
            sys.modules.pop(key, None)


@pytest.fixture
def eval_config() -> dict[str, Any]:
    """Minimal config matching the structure create_tweet_eval_prompt expects."""
    return {
        "evaluation": {
            "topics": ["AI/ML", "Programming"],
            "projects": ["gptme", "ActivityWatch"],
            "triggers": [{"type": "mention", "description": "Direct mentions"}],
        },
        "blacklist": {
            "topics": ["crypto-scams"],
            "patterns": ["follow-for-follow"],
        },
    }


def _base_tweet(text: str) -> dict[str, Any]:
    return {"text": text, "author": "SomeUser", "context": {}}


# --- Identity-confusion fix (gptme/gptme-contrib#663) regression tests ---


def test_direct_mention_adds_identity_clarification(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tweet containing @TimeToBuildBob should get an IMPORTANT identity note.

    Before #663: LLM sometimes reasoned 'mention is directed at @TimeToBuildBob,
    not at our agent account' — wrong, because we ARE @TimeToBuildBob.
    """
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = _base_tweet("@TimeToBuildBob More 404'ing links :(")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IMPORTANT" in prompt
    assert "@TimeToBuildBob" in prompt
    assert "IS our account" in prompt


def test_non_mention_tweet_gets_no_identity_note(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tweet NOT mentioning our handle should not trigger the clarification."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = _base_tweet("Just shipped a cool feature in my project!")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" not in prompt


def test_different_user_mention_does_not_trigger(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tweet mentioning a different user should not trigger the clarification."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = _base_tweet("@ErikBjare what do you think about this?")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" not in prompt


def test_case_insensitive_handle_match(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twitter handles are case-insensitive — @timetobuildbob should match too.

    This is the behavior explicitly added in the review feedback commit (2d2dd9d).
    """
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = _base_tweet("@timetobuildbob hey, got a minute?")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" in prompt


def test_unset_handle_raises_value_error(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TWITTER_HANDLE must be explicitly set — no silent default.

    ErikBjare/bob#602: defaulting to 'TimeToBuildBob' makes the script
    unusable for other agents without an obvious error. Fail loudly instead.
    """
    monkeypatch.delenv("TWITTER_HANDLE", raising=False)
    tweet = _base_tweet("@agent can you help with this?")

    import pytest as _pytest

    with _pytest.raises(ValueError, match="TWITTER_HANDLE"):
        llm_module.create_tweet_eval_prompt(tweet, eval_config)


def test_response_prompt_defers_to_workspace_persona(
    llm_module: types.ModuleType,
) -> None:
    """Reply drafting should reinforce the loaded agent voice, not flatten it.

    ErikBjare/bob#808 found that generic "professional helpful tone" guidance
    suppressed Bob's direct, opinionated voice even though SOUL.md was loaded.
    """
    tweet = _base_tweet("@TimeToBuildBob loved the gource demo")
    config = {
        "templates": {
            "examples": [
                {
                    "reasoning": "Specific technical response fits",
                    "text": "Nice catch. The media path is still missing, but the post-tweet worker now closes the loop.",
                    "type": "reply",
                    "thread_needed": False,
                    "follow_up": None,
                }
            ]
        }
    }

    prompt = llm_module.create_response_prompt(
        tweet,
        {"action": "respond", "reasoning": "Direct mention"},
        config,
    )

    assert "workspace persona already loaded in the system prompt" in prompt
    assert "sounding corporate, deferential, or sanitized is a failure" in prompt
    assert "Do not promise follow-up, links, or artifacts" in prompt
    assert "professional, helpful tone" not in prompt
    assert "Avoid controversial topics" not in prompt


def test_openrouter_key_resolution_prefers_twitter_specific_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_SOCIAL", "social-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_TWITTER", "twitter-key")

    assert llm_module._resolve_openrouter_api_key() == "twitter-key"


def test_openrouter_key_resolution_falls_back_to_social_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_TWITTER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_SOCIAL", "social-key")

    assert llm_module._resolve_openrouter_api_key() == "social-key"


def test_reply_with_max_tokens_delegates_when_scoped_key_matches_shared_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_TWITTER", "shared-key")
    # Set a real API key so the CC-subprocess fallback is not triggered.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-real-key")

    expected = llm_module.Message("assistant", "delegated")
    calls: dict[str, Any] = {}

    def fake_reply(messages: list[Any], model_name: str, **kwargs: Any) -> Any:
        calls["messages"] = messages
        calls["model_name"] = model_name
        calls["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(llm_module, "reply", fake_reply)

    messages = [llm_module.Message("user", "hello")]
    result = llm_module._reply_with_max_tokens(
        messages, "openrouter/anthropic/claude-sonnet-4-5"
    )

    assert result is expected
    assert calls["messages"] == messages
    assert calls["model_name"] == "openrouter/anthropic/claude-sonnet-4-5"
    assert calls["kwargs"] == {
        "stream": False,
        "max_tokens": llm_module.TWITTER_MAX_TOKENS,
    }


def test_reply_with_max_tokens_keeps_direct_openrouter_call_for_scoped_override(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_TWITTER", "twitter-key")

    monkeypatch.setattr(
        llm_module,
        "reply",
        lambda *args, **kwargs: pytest.fail(
            "reply() should not be used for scoped override"
        ),
    )

    created: dict[str, Any] = {}
    completion_calls: list[dict[str, Any]] = []

    class _FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            created["api_key"] = api_key
            created["base_url"] = base_url
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self.create)
            )

        def create(self, **kwargs: Any) -> Any:
            completion_calls.append(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="scoped override")
                    )
                ]
            )

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    messages = [
        llm_module.Message("system", "system prompt"),
        llm_module.Message("user", "hello"),
    ]
    result = llm_module._reply_with_max_tokens(
        messages, "openrouter/anthropic/claude-sonnet-4-5"
    )

    assert created == {
        "api_key": "twitter-key",
        "base_url": "https://openrouter.ai/api/v1",
    }
    assert completion_calls == [
        {
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
            ],
            "max_tokens": llm_module.TWITTER_MAX_TOKENS,
            "temperature": 0.5,
        }
    ]
    assert result.role == "assistant"
    assert result.content == "scoped override"


def test_reply_with_max_tokens_uses_cc_subprocess_for_dummy_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANTHROPIC_API_KEY is a dummy placeholder, use CC subprocess fallback."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_TWITTER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_SOCIAL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")

    cc_calls: list[list[Any]] = []
    expected = llm_module.Message("assistant", "cc response")

    def fake_cc_subprocess(messages: list[Any]) -> Any:
        cc_calls.append(messages)
        return expected

    monkeypatch.setattr(llm_module, "_reply_with_cc_subprocess", fake_cc_subprocess)
    monkeypatch.setattr(
        llm_module,
        "reply",
        lambda *a, **k: pytest.fail("reply() must not be called with dummy key"),
    )

    messages = [llm_module.Message("user", "hello")]
    result = llm_module._reply_with_max_tokens(messages, "anthropic/claude-sonnet-4-5")

    assert result is expected
    assert len(cc_calls) == 1
    assert cc_calls[0] == messages


def test_reply_with_max_tokens_uses_cc_subprocess_when_no_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANTHROPIC_API_KEY is absent entirely, use CC subprocess fallback."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_TWITTER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_SOCIAL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cc_calls: list[list[Any]] = []
    expected = llm_module.Message("assistant", "cc response")

    def fake_cc_subprocess(messages: list[Any]) -> Any:
        cc_calls.append(messages)
        return expected

    monkeypatch.setattr(llm_module, "_reply_with_cc_subprocess", fake_cc_subprocess)
    monkeypatch.setattr(
        llm_module,
        "reply",
        lambda *a, **k: pytest.fail("reply() must not be called without key"),
    )

    messages = [llm_module.Message("user", "hello")]
    result = llm_module._reply_with_max_tokens(messages, "anthropic/claude-sonnet-4-5")

    assert result is expected
    assert len(cc_calls) == 1


def test_reply_with_cc_subprocess_success(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC subprocess path builds combined prompt and returns assistant Message."""
    import subprocess as subprocess_mod

    run_calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        run_calls.append({"cmd": cmd, "kwargs": kwargs})
        return types.SimpleNamespace(
            returncode=0, stdout="review response\n", stderr=""
        )

    # subprocess is imported inline inside _reply_with_cc_subprocess; patching
    # the cached module object is sufficient since all imports share one instance.
    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    messages = [
        llm_module.Message("system", "You are a helpful agent."),
        llm_module.Message("user", "Review this tweet."),
    ]
    result = llm_module._reply_with_cc_subprocess(messages)

    assert result.role == "assistant"
    assert result.content == "review response"
    assert len(run_calls) == 1
    cmd = run_calls[0]["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--no-session-persistence" in cmd
    # Combined prompt must contain both system and user parts
    prompt_arg = cmd[-1]
    assert "You are a helpful agent." in prompt_arg
    assert "Review this tweet." in prompt_arg


def test_our_handle_in_thread_context_triggers_identity_note(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When our handle appears as a thread author, inject identity context.

    Regression guard for ErikBjare/bob#602: the LLM saw a thread where
    @TimeToBuildBob had already replied and concluded the conversation was
    "a resolved technical issue between two other parties" — completely
    missing that @TimeToBuildBob IS us. The eval prompt must mark our prior
    messages and explain that their presence doesn't mean the thread is done.
    """
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "text": "Doesn't seem to be working?",
        "author": "ErikBjare",
        "context": {},
        "thread_context": [
            {"author": "TimeToBuildBob", "text": "Here's my original tweet"},
            {"author": "ErikBjare", "text": "404 link"},
            {"author": "TimeToBuildBob", "text": "Corrected URL"},
        ],
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    # Identity note fires because we're in the thread (even though the current
    # tweet text doesn't contain @TimeToBuildBob)
    assert "IS our account" in prompt
    # Our prior messages are explicitly marked
    assert "(US — our prior message)" in prompt
    # Explain that prior replies don't mean the conversation is resolved
    assert "does NOT mean the conversation is resolved" in prompt


def test_thread_context_without_us_does_not_trigger(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread without our handle among authors should not trigger identity note."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "text": "Interesting thread",
        "author": "SomeUser",
        "context": {},
        "thread_context": [
            {"author": "OtherUserA", "text": "Original"},
            {"author": "OtherUserB", "text": "Reply"},
        ],
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" not in prompt
    assert "(US — our prior message)" not in prompt


def test_substring_handle_triggers_known_false_positive(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@TimeToBuildBobby triggers the note for @TimeToBuildBob — documented false-positive.

    The implementation uses `f'@{handle}'` as the needle, which means a
    longer handle with the short one as a prefix would match. This is a
    known limitation — document it here so future refactors don't regress
    behavior silently.
    """
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    # Tweet mentions a *different* user whose handle starts with ours
    tweet = _base_tweet("@TimeToBuildBobby nice work!")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    # Current behavior: substring match triggers the note.
    # This is a known false-positive risk but a safer failure mode than the
    # false-negative IGNOREs we're guarding against. If the implementation
    # moves to word-boundary matching, update this assertion.
    assert "IS our account" in prompt


def test_prompt_contains_tweet_text_and_author(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: the prompt should echo back the tweet text and author."""
    monkeypatch.setenv("TWITTER_HANDLE", "TestBot")
    tweet = _base_tweet("a very specific string 17d2g")
    tweet["author"] = "UniqueAuthor17d2g"

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "a very specific string 17d2g" in prompt
    assert "UniqueAuthor17d2g" in prompt
