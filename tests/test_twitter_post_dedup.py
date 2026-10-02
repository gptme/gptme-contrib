"""Send-site duplicate guard for ``twitter.py post``.

Concurrent dispatched sessions call the raw CLI directly, so the dedup has to
live at the send site rather than in the draft workflow.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Reuse the stubbed-module loader + fixture from the URL-guard tests (tests/ is
# not a package, so load it by path).
_spec = importlib.util.spec_from_file_location(
    "test_twitter_post_url_guard",
    Path(__file__).resolve().parent / "test_twitter_post_url_guard.py",
)
assert _spec and _spec.loader
_guard = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_twitter_post_url_guard", _guard)
_spec.loader.exec_module(_guard)
twitter_module = _guard.twitter_module  # fixture


@pytest.fixture
def posted(twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path) -> list:
    """Isolate the marker dir and record every create_tweet call."""
    calls: list[dict] = []

    def create_tweet(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data={"id": str(len(calls))})

    monkeypatch.setattr(twitter_module, "POST_DEDUP_DIR", tmp_path / "dedup")
    monkeypatch.setattr(twitter_module, "validate_urls_in_text", lambda text: [])
    monkeypatch.setattr(twitter_module, "_get_user_auth", lambda client: True)
    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=create_tweet
        ),
    )
    monkeypatch.delenv("TWITTER_ACCOUNT", raising=False)
    return calls


def _refused(twitter_module: Any, *args, **kwargs) -> bool:
    with pytest.raises(SystemExit) as exc:
        twitter_module.post(*args, **kwargs)
    return bool(exc.value.code == twitter_module.POST_DUPLICATE_EXIT_CODE)


def test_identical_tweet_within_ttl_is_refused(twitter_module: Any, posted) -> None:
    twitter_module.post("hello world", None, False)
    assert _refused(twitter_module, "hello  world", None, False)
    assert len(posted) == 1


def test_force_overrides_dedup(twitter_module: Any, posted) -> None:
    twitter_module.post("hello world", None, False)
    twitter_module.post("hello world", None, False, force=True)
    assert len(posted) == 2


def test_different_text_is_not_a_duplicate(twitter_module: Any, posted) -> None:
    twitter_module.post("hello world", None, False)
    twitter_module.post("something else", None, False)
    assert len(posted) == 2


def test_second_reply_to_same_tweet_refused_even_with_new_wording(
    twitter_module: Any, posted
) -> None:
    twitter_module.post("thanks!", "123", False)
    assert _refused(twitter_module, "thank you so much!", "123", False)
    twitter_module.post("thanks!", "456", False)
    assert [c["in_reply_to_tweet_id"] for c in posted] == ["123", "456"]


def test_second_quote_of_same_tweet_refused(twitter_module: Any, posted) -> None:
    twitter_module.post("look at this", None, False, quote_id="789")
    assert _refused(twitter_module, "really, look", None, False, quote_id="789")
    assert len(posted) == 1


def test_accounts_are_deduped_independently(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    twitter_module.post("v1.0 released", None, False)
    monkeypatch.setenv("TWITTER_ACCOUNT", "gptmeorg")
    twitter_module.post("v1.0 released", None, False)
    assert len(posted) == 2


def test_expired_marker_allows_repost(twitter_module: Any, posted) -> None:
    twitter_module.post("hello world", None, False)
    (marker,) = twitter_module.POST_DEDUP_DIR.glob("*.posted")
    old = time.time() - twitter_module.POST_DEDUP_TTL - 60
    os.utime(marker, (old, old))
    twitter_module.post("hello world", None, False)
    assert len(posted) == 2


def test_failed_post_does_not_mark(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(create_tweet=boom),
    )
    with pytest.raises(RuntimeError):
        twitter_module.post("hello world", None, False)
    assert list(twitter_module.POST_DEDUP_DIR.glob("*.posted")) == []


def test_empty_response_data_does_not_mark(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response with no data is a failed post, not a posted one: no marker, and
    a retry is allowed (the dedup must not strand the user with --force)."""
    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=lambda **kwargs: SimpleNamespace(data=None)
        ),
    )
    with pytest.raises(SystemExit) as exc:
        twitter_module.post("hello world", None, False)
    assert exc.value.code == 1
    assert list(twitter_module.POST_DEDUP_DIR.glob("*.posted")) == []

    # Recovery: the same text posts normally once the API behaves.
    def working_create_tweet(**kwargs):
        posted.append(kwargs)
        return SimpleNamespace(data={"id": "1"})

    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=working_create_tweet
        ),
    )
    twitter_module.post("hello world", None, False)
    assert len(posted) == 1


def test_thread_first_tweet_with_empty_data_does_not_mark(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        twitter_module,
        "split_thread",
        lambda text: [SimpleNamespace(text="one"), SimpleNamespace(text="two")],
    )
    calls: list[str] = []

    def create_tweet(**kwargs):
        calls.append(kwargs["text"])
        return SimpleNamespace(data=None)

    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=create_tweet
        ),
    )
    with pytest.raises(SystemExit) as exc:
        twitter_module.post("one\n---\ntwo", None, True)
    assert exc.value.code == 1
    assert calls == ["one"]
    assert list(twitter_module.POST_DEDUP_DIR.glob("*.posted")) == []


def test_thread_failing_after_first_tweet_still_marks(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        twitter_module,
        "split_thread",
        lambda text: [SimpleNamespace(text="one"), SimpleNamespace(text="two")],
    )
    calls: list[str] = []

    def create_tweet(**kwargs):
        calls.append(kwargs["text"])
        if len(calls) > 1:
            raise RuntimeError("rate limited")
        return SimpleNamespace(data={"id": "1"})

    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=create_tweet
        ),
    )
    with pytest.raises(RuntimeError):
        twitter_module.post("one\n---\ntwo", None, True)
    assert _refused(twitter_module, "one\n---\ntwo", None, True)
    assert calls == ["one", "two"]


def test_concurrent_identical_posts_send_once(
    twitter_module: Any, posted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock spans check -> post -> mark, so racing sessions can't both pass."""
    results: list[str] = []

    def slow_create_tweet(**kwargs):
        time.sleep(0.2)  # widen the window a check-then-send gate would lose
        posted.append(kwargs)
        return SimpleNamespace(data={"id": "1"})

    monkeypatch.setattr(
        twitter_module,
        "load_twitter_client",
        lambda require_auth=True, headless=False: SimpleNamespace(
            create_tweet=slow_create_tweet
        ),
    )

    def worker() -> None:
        try:
            twitter_module.post("race", None, False)
            results.append("posted")
        except SystemExit as exc:
            results.append(f"exit:{exc.code}")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(posted) == 1
    assert sorted(results) == ["exit:3"] * 3 + ["posted"]
