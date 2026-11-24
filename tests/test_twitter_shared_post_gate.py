"""Manual CLI posts and workflow posts share one per-target lock and receipt.

``twitter.py post`` (manual) and ``workflow.py`` (approved queue, trusted-user
auto-post) used to keep separate histories, so both could reply to the same
tweet. These tests wire the workflow to the real ``twitter.py`` gate and check
that a receipt written by either path stops the other.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parent


@dataclass
class _Eval:
    action: str
    relevance: int
    priority: int


@dataclass
class _Response:
    text: str
    type: str
    thread_needed: bool
    follow_up: str | None


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _TESTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_guard = _load("test_twitter_post_url_guard")
_drafts = _load("test_twitter_workflow_drafts")
twitter_module = _guard.twitter_module  # fixture
workflow_module = _drafts.workflow_module  # fixture


@pytest.fixture
def shared(
    twitter_module: Any,
    workflow_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[dict]:
    """Point both paths at one real gate and record every create_tweet call."""
    calls: list[dict] = []

    def create_tweet(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(data={"id": str(1000 + len(calls))})

    client = SimpleNamespace(create_tweet=create_tweet)
    monkeypatch.setattr(twitter_module, "POST_DEDUP_DIR", tmp_path / "dedup")
    monkeypatch.setattr(twitter_module, "validate_urls_in_text", lambda text: [])
    monkeypatch.setattr(twitter_module, "_get_user_auth", lambda client: True)
    monkeypatch.setattr(twitter_module, "load_twitter_client", lambda *a, **k: client)
    monkeypatch.delenv("TWITTER_ACCOUNT", raising=False)

    for name in ("_post_dedup_gate", "_post_dedup_key", "current_account"):
        monkeypatch.setattr(workflow_module, name, getattr(twitter_module, name))
    monkeypatch.setattr(workflow_module, "load_twitter_client", lambda *a, **k: client)
    monkeypatch.setattr(
        workflow_module, "_check_for_duplicate_replies_internal", lambda draft: {}
    )
    monkeypatch.setattr(
        workflow_module, "_find_live_duplicate_reply_ids", lambda *a, **k: []
    )
    monkeypatch.setattr(workflow_module, "_validate_draft_urls", lambda draft: [])
    monkeypatch.setattr(workflow_module, "_validate_draft_length", lambda draft: [])
    monkeypatch.setattr(workflow_module, "_git_commit_posted", lambda *a, **k: None)
    _drafts._set_status_dirs(workflow_module, tmp_path)
    return calls


def _queue_reply(workflow_module: Any, target: str, text: str) -> Path:
    path: Path = workflow_module.APPROVED_DIR / f"reply-{target}.yml"
    workflow_module.TweetDraft(text=text, type="reply", in_reply_to=target).save(path)
    return path


def _post_queue(workflow_module: Any) -> None:
    workflow_module.post(
        dry_run=False, yes=True, draft_id=None, max_posts=5, skip_url_check=False
    )


def test_manual_reply_blocks_queued_workflow_reply(
    twitter_module: Any, workflow_module: Any, shared: list[dict]
) -> None:
    twitter_module.post("Manual answer", "4242", False)
    path = _queue_reply(workflow_module, "4242", "Different automatic wording")

    _post_queue(workflow_module)

    assert [call["text"] for call in shared] == ["Manual answer"]
    rejected = workflow_module.REJECTED_DIR / path.name
    assert rejected.exists()
    draft = workflow_module.TweetDraft.load(rejected)
    assert draft.reject_reason == workflow_module.SHARED_DUPLICATE_REASON


def test_workflow_reply_blocks_later_manual_reply(
    twitter_module: Any, workflow_module: Any, shared: list[dict]
) -> None:
    _queue_reply(workflow_module, "4242", "Automatic answer")
    _post_queue(workflow_module)

    with pytest.raises(SystemExit) as exc:
        twitter_module.post("Manual follow-up", "4242", False)

    assert exc.value.code == twitter_module.POST_DUPLICATE_EXIT_CODE
    assert [call["text"] for call in shared] == ["Automatic answer"]


def test_other_targets_still_post(
    twitter_module: Any, workflow_module: Any, shared: list[dict]
) -> None:
    twitter_module.post("Manual answer", "4242", False)
    _queue_reply(workflow_module, "5353", "Answer to a different tweet")

    _post_queue(workflow_module)

    assert [call["text"] for call in shared] == [
        "Manual answer",
        "Answer to a different tweet",
    ]


def test_failed_workflow_post_leaves_no_receipt(
    twitter_module: Any,
    workflow_module: Any,
    shared: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: Any) -> Any:
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(
        workflow_module,
        "load_twitter_client",
        lambda *a, **k: SimpleNamespace(create_tweet=fail),
    )
    path = _queue_reply(workflow_module, "4242", "Automatic answer")

    _post_queue(workflow_module)

    assert path.exists()  # transient failure stays queued
    assert list(twitter_module.POST_DEDUP_DIR.glob("*.posted")) == []
    twitter_module.post("Manual answer", "4242", False)
    assert [call["text"] for call in shared] == ["Manual answer"]


def test_workflow_waits_for_in_flight_manual_post(
    twitter_module: Any, workflow_module: Any, shared: list[dict]
) -> None:
    """A workflow post that races a manual one sees the manual receipt."""
    key = twitter_module._post_dedup_key("", "4242", None, "")
    _queue_reply(workflow_module, "4242", "Automatic answer")
    holding = threading.Event()
    release = threading.Event()

    def manual() -> None:
        with twitter_module._post_dedup_gate(key, False) as mark_posted:
            holding.set()
            release.wait(5)
            shared.append({"text": "Manual answer"})
            mark_posted()

    thread = threading.Thread(target=manual)
    thread.start()
    assert holding.wait(5)
    worker = threading.Thread(target=_post_queue, args=(workflow_module,))
    worker.start()
    worker.join(0.2)
    assert worker.is_alive()  # blocked on the shared lock, not posting
    release.set()
    thread.join(5)
    worker.join(5)

    assert [call["text"] for call in shared] == ["Manual answer"]


def test_trusted_user_autopost_respects_manual_receipt(
    twitter_module: Any,
    workflow_module: Any,
    shared: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-reply path, where the Nov 2026 double reply came from."""
    twitter_module.post("Manual answer", "4242", False)
    tweet = SimpleNamespace(
        id="4242",
        author_id="7",
        text="New game is live, try it out!",
        created_at=datetime.now(timezone.utc),
        public_metrics={},
        conversation_id=None,
    )
    user = SimpleNamespace(
        id="7", username="ErikBjare", public_metrics={"followers_count": 1}
    )
    saved: list[tuple[str, str | None]] = []

    def fake_save_draft(draft: Any, status: str = "new") -> Path:
        saved.append((status, draft.reject_reason))
        path: Path = workflow_module.TWEETS_DIR / status / "saved.yml"
        path.write_text("text: saved\n")
        return path

    monkeypatch.setattr(
        workflow_module,
        "cached_get_me",
        lambda *a, **k: SimpleNamespace(data=SimpleNamespace(id="999")),
    )
    monkeypatch.setattr(
        workflow_module, "should_evaluate_tweet", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr(workflow_module, "is_reply_restricted", lambda *a, **k: False)
    monkeypatch.setattr(workflow_module, "is_tweet_cached", lambda *a, **k: False)
    monkeypatch.setattr(
        workflow_module,
        "process_tweet",
        lambda *a, **k: (
            _Eval("respond", 100, 100),
            _Response("Automatic answer", "reply", False, None),
        ),
    )
    monkeypatch.setattr(workflow_module, "save_to_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        workflow_module, "is_trusted_user", lambda username: username == "ErikBjare"
    )
    monkeypatch.setattr(workflow_module, "save_draft", fake_save_draft)

    workflow_module.process_timeline_tweets(
        [tweet],
        [user],
        source="mentions",
        client=object(),
        times=1,
        dry_run=False,
        max_drafts=10,
    )

    assert [call["text"] for call in shared] == ["Manual answer"]
    assert saved == [("rejected", workflow_module.SHARED_DUPLICATE_REASON)]
