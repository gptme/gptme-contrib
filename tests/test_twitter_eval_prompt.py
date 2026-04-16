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
    gptme_message_stub.Message = type("Message", (), {})  # type: ignore[attr-defined]
    gptme_prompts_stub = types.ModuleType("gptme.prompts")
    gptme_prompts_stub.prompt_workspace = lambda *args, **kwargs: iter([])  # type: ignore[attr-defined]

    sys.modules.setdefault("gptme", gptme_stub)
    sys.modules.setdefault("gptme.llm", gptme_llm_stub)
    sys.modules.setdefault("gptme.llm.models", gptme_llm_models_stub)
    sys.modules.setdefault("gptme.dirs", gptme_dirs_stub)
    sys.modules.setdefault("gptme.message", gptme_message_stub)
    sys.modules.setdefault("gptme.prompts", gptme_prompts_stub)

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


def test_unset_handle_skips_detection(
    llm_module: types.ModuleType,
    eval_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TWITTER_HANDLE is unset, detection must be skipped entirely.

    Regression guard for review feedback: the original code used a fallback
    default 'agent', which would cause false non-matches on tweets mentioning
    @agent. The fix removed that default — with no env var, no note is added.
    """
    monkeypatch.delenv("TWITTER_HANDLE", raising=False)
    # Tweet that would have matched the old default handle 'agent'
    tweet = _base_tweet("@agent can you help with this?")

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "IS our account" not in prompt


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
    monkeypatch.delenv("TWITTER_HANDLE", raising=False)
    tweet = _base_tweet("a very specific string 17d2g")
    tweet["author"] = "UniqueAuthor17d2g"

    prompt = llm_module.create_tweet_eval_prompt(tweet, eval_config)

    assert "a very specific string 17d2g" in prompt
    assert "UniqueAuthor17d2g" in prompt
