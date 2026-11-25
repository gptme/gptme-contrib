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


def test_response_prompt_forbids_reply_when_not_allowed(
    llm_module: types.ModuleType,
) -> None:
    """Reply-restricted tweets get a standalone-post instruction, not a reply.

    The gate must be applied before the metered response call (so the reply is
    never generated), while still letting the item produce standalone output.
    """
    tweet = _base_tweet("An interesting unmentioned post about local-first tools")
    config: dict[str, Any] = {"templates": {"examples": []}}

    allowed = llm_module.create_response_prompt(
        tweet, {"action": "respond"}, config, allow_reply=True
    )
    blocked = llm_module.create_response_prompt(
        tweet, {"action": "respond"}, config, allow_reply=False
    )

    assert "Reply constraint" not in allowed
    assert "cannot reply to or quote" in blocked
    assert '"type" to "tweet"' in blocked


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

    cc_calls: list[tuple[list[Any], str]] = []
    expected = llm_module.Message("assistant", "cc response")

    def fake_cc_subprocess(messages: list[Any], model_name: str) -> Any:
        cc_calls.append((messages, model_name))
        return expected

    monkeypatch.setattr(llm_module, "_reply_with_cc_subprocess", fake_cc_subprocess)
    monkeypatch.setattr(
        llm_module,
        "reply",
        lambda *a, **k: pytest.fail("reply() must not be called with dummy key"),
    )

    messages = [llm_module.Message("user", "hello")]
    model = "anthropic/claude-sonnet-4-5"
    result = llm_module._reply_with_max_tokens(messages, model)

    assert result is expected
    assert len(cc_calls) == 1
    assert cc_calls[0] == (messages, model)


def test_reply_with_max_tokens_uses_cc_subprocess_when_no_key(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANTHROPIC_API_KEY is absent entirely, use CC subprocess fallback."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_TWITTER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_SOCIAL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cc_calls: list[tuple[list[Any], str]] = []
    expected = llm_module.Message("assistant", "cc response")

    def fake_cc_subprocess(messages: list[Any], model_name: str) -> Any:
        cc_calls.append((messages, model_name))
        return expected

    monkeypatch.setattr(llm_module, "_reply_with_cc_subprocess", fake_cc_subprocess)
    monkeypatch.setattr(
        llm_module,
        "reply",
        lambda *a, **k: pytest.fail("reply() must not be called without key"),
    )

    messages = [llm_module.Message("user", "hello")]
    model = "anthropic/claude-sonnet-4-5"
    result = llm_module._reply_with_max_tokens(messages, model)

    assert result is expected
    assert len(cc_calls) == 1
    assert cc_calls[0] == (messages, model)


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
    result = llm_module._reply_with_cc_subprocess(
        messages, "anthropic/claude-sonnet-4-5"
    )

    assert result.role == "assistant"
    assert result.content == "review response"
    assert len(run_calls) == 1
    cmd = run_calls[0]["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--no-session-persistence" in cmd
    assert "--model" in cmd
    # llm.py strips "anthropic/" prefix before passing to the claude CLI
    assert "claude-sonnet-4-5" in cmd
    assert "anthropic/claude-sonnet-4-5" not in cmd
    # The prompt must travel over stdin rather than argv: real timeline context can
    # exceed Linux's per-argument limit and fail before Claude starts. With no
    # positional prompt, `claude -p` reads stdin; a positional "-" is ambiguous.
    assert cmd == [
        "claude",
        "-p",
        "--no-session-persistence",
        "--model",
        "claude-sonnet-4-5",
    ]
    prompt_input = run_calls[0]["kwargs"]["input"]
    assert "You are a helpful agent." in prompt_input
    assert "Review this tweet." in prompt_input


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


def test_longer_handle_with_our_prefix_is_not_our_mention(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@TimeToBuildBobby is a different account: handle matching is boundary-aware.

    The earlier substring needle documented this as a known false positive.
    """
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = _base_tweet("@TimeToBuildBobby nice work!")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" not in prompt


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


# --- Inherited reply-prefix mentions (ErikBjare/bob#1519) ---

_THREAD = [
    {"author": "ErikBjare", "text": "We shipped a game"},
    {"author": "TimeToBuildBob", "text": "The puzzle budget is the interesting part"},
    {"author": "ErikBjare", "text": "Thanks everyone for playing"},
]


def test_reply_prefix_handle_is_inherited_not_typed(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X prepends thread handles to a reply; that is not a mention typed to us."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "text": "@ErikBjare @TimeToBuildBob this is great, love it",
        "author": "someone",
        "context": {},
        "thread_context": _THREAD,
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" not in prompt
    assert "automatic reply prefix" in prompt
    assert "IS our account" in prompt


def test_handle_typed_in_reply_body_is_direct(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "text": "@ErikBjare @TimeToBuildBob what do you think, @timetobuildbob?",
        "author": "someone",
        "context": {},
        "thread_context": _THREAD,
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" in prompt
    assert "automatic reply prefix" not in prompt


def test_leading_handle_without_thread_stays_direct(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without thread context a leading handle cannot be told apart; keep #663."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    prompt = llm_module.create_tweet_eval_prompt(
        _base_tweet("@TimeToBuildBob hello"), eval_config
    )

    assert "addressed TO us" in prompt


@pytest.mark.parametrize("parent_id", [42, "42"])
def test_reply_to_our_message_is_direct(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    parent_id: int | str,
) -> None:
    """Use the target's reply ID, even when a newer sibling follows the parent."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "id": "target",
        "text": "@TimeToBuildBob not sure that's right",
        "author": "ErikBjare",
        "context": {},
        "thread_context": [
            {"id": 42, "author": "TimeToBuildBob", "text": "My claim"},
            {
                "id": "target",
                "author": "ErikBjare",
                "text": "not sure",
                "replied_to_id": parent_id,
            },
            {"id": "sibling", "author": "someone", "text": "Hello"},
        ],
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" in prompt
    assert "automatic reply prefix" not in prompt


@pytest.mark.parametrize(
    ("text", "thread"),
    [
        ("@ErikBjare @TimeToBuildBob @TimeToBuildBob what do you think?", _THREAD),
        ("@ErikBjare @TimeToBuildBob @timetobuildbob", _THREAD),
        ("@ErikBjare @TimeToBuildBob what do you think?", _THREAD[:1]),
        ("@ErikBjare @AnotherUser @TimeToBuildBob help?", _THREAD),
    ],
)
def test_leading_typed_mention_survives_reply_prefix(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    thread: list[dict[str, str]],
) -> None:
    """Repeated or non-thread handles are not part of an inherited prefix."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "text": text,
        "author": "someone",
        "context": {},
        "thread_context": thread,
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" in prompt
    assert "automatic reply prefix" not in prompt


def test_latest_thread_entry_is_not_necessarily_reply_parent(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversation context includes siblings, not just the target's ancestors."""
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "id": "target",
        "text": "@ErikBjare @TimeToBuildBob great game",
        "author": "someone",
        "thread_context": [
            {"id": "root", "author": "ErikBjare", "text": "My game"},
            {
                "id": "target",
                "author": "someone",
                "text": "great game",
                "replied_to_id": "root",
            },
            {"id": "sibling", "author": "TimeToBuildBob", "text": "My review"},
        ],
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" not in prompt
    assert "possible automatic reply prefix" in prompt


def test_leading_mention_without_actual_parent_prefix_is_direct(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITTER_HANDLE", "TimeToBuildBob")
    tweet = {
        "id": "target",
        "text": "@TimeToBuildBob should we ship?",
        "author": "someone",
        "thread_context": [
            {"id": "root", "author": "ErikBjare", "text": "Release?"},
            {"id": "earlier", "author": "TimeToBuildBob", "text": "My review"},
            {
                "id": "target",
                "author": "someone",
                "text": "@TimeToBuildBob should we ship?",
                "replied_to_id": "root",
            },
        ],
    }

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "addressed TO us" in prompt
    assert "automatic reply prefix" not in prompt


@pytest.mark.parametrize("style_passed", [None, False])
def test_style_must_be_reviewed_and_pass_before_auto_approval(
    llm_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    style_passed: bool | None,
) -> None:
    criteria = {
        "Technical Accuracy": llm_module.ReviewResult(notes="Accurate", passed=True)
    }
    if style_passed is not None:
        criteria["Natural writing style"] = llm_module.ReviewResult(
            notes="Repeated separator is unnecessary", passed=style_passed
        )
    response = llm_module.ReviewResponse(
        criteria_results=criteria,
        recommendation="approve",
        improvements=[],
        reasoning="All good",
    )
    monkeypatch.setattr(llm_module, "review_draft", lambda _: response)
    approved, result = llm_module.verify_draft(
        {"text": "One point — another point", "type": "tweet"}
    )
    assert approved is False
    assert result.criteria_results["Natural writing style"].passed is False


@pytest.mark.parametrize(
    "text",
    [
        "Fixed the timeout. The call now completes.",
        "The delay was 60 seconds — exactly the configured timeout.",
    ],
)
def test_style_decision_can_approve_clear_prose_with_or_without_a_dash(
    llm_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    response = llm_module.ReviewResponse(
        criteria_results={
            "Natural writing style": llm_module.ReviewResult(
                notes="Clear, precise punctuation", passed=True
            )
        },
        recommendation="approve",
        improvements=[],
        reasoning="Good",
    )
    monkeypatch.setattr(llm_module, "review_draft", lambda _: response)
    approved, _ = llm_module.verify_draft({"text": text, "type": "reply"})
    assert approved is True


def test_style_signals_count_authored_thread_not_quoted_context(
    llm_module: types.ModuleType,
) -> None:
    draft = {
        "text": "A clear sentence.",
        "type": "thread",
        "thread": ["One point — another point."],
        "context": {"original_tweet": {"text": "Quoted — third — party — text"}},
    }
    report = llm_module.draft_style_report(draft)
    assert report["em_dash_count"] == 1
    assert report["word_count"] > 0


def test_reviewer_receives_authored_followups_not_only_aggregate_counts(
    llm_module: types.ModuleType,
) -> None:
    followup = "Unique followup zxqv1234 — unsupported claim — canned conclusion."
    draft = {
        "text": "Fixed the bug.",
        "type": "thread",
        "thread": [followup],
        "context": {},
    }
    prompt = llm_module.create_review_prompt(draft, {"review": {"required_checks": []}})
    assert "zxqv1234" in prompt
    assert "unsupported claim" in prompt


@pytest.mark.parametrize("key", ["natural_writing_style", "Natural Writing Style"])
def test_explicit_style_decision_accepts_normal_criterion_key_formats(
    llm_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    response = llm_module.ReviewResponse(
        criteria_results={
            key: llm_module.ReviewResult(notes="Deliberate dash", passed=True)
        },
        recommendation="approve",
        improvements=[],
        reasoning="Clear",
    )
    monkeypatch.setattr(llm_module, "review_draft", lambda _: response)
    approved, _ = llm_module.verify_draft(
        {"text": "It took 60 seconds — exactly the timeout.", "type": "reply"}
    )
    assert approved
    assert set(response.criteria_results) == {"Natural writing style"}
