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


def _make_pkg(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []
    return mod


def _load_workflow_module() -> Any:
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

    twitter_api_stub: Any = types.ModuleType("twitter.twitter")
    twitter_api_stub.cached_get_me = lambda *args, **kwargs: SimpleNamespace(
        data=SimpleNamespace(id=0)
    )
    twitter_api_stub.load_twitter_client = lambda *args, **kwargs: None

    stubbed_modules: dict[str, Any] = {
        "gptmail": gptmail_stub,
        "gptmail.communication_utils": gptmail_comm_stub,
        "gptmail.communication_utils.monitoring": monitoring_stub,
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

    for name, stub in stubbed_modules.items():
        sys.modules.setdefault(name, stub)

    spec = importlib.util.spec_from_file_location(
        "twitter_workflow_under_test", WORKFLOW_PATH
    )
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow_module() -> Generator[Any, None, None]:
    stub_keys = (
        "gptmail",
        "gptmail.communication_utils",
        "gptmail.communication_utils.monitoring",
        "gptme",
        "gptme.init",
        "dotenv",
        "click",
        "rich",
        "rich.console",
        "rich.prompt",
        "trusted_users",
        "twitter",
        "twitter.llm",
        "twitter.twitter",
        "twitter_workflow_under_test",
    )
    pre_existing = {key for key in stub_keys if key in sys.modules}
    module = _load_workflow_module()
    yield module
    for key in stub_keys:
        if key not in pre_existing:
            sys.modules.pop(key, None)


def _set_status_dirs(module: Any, tmp_path: Path) -> None:
    tweets_dir = tmp_path / "tweets"
    module.TWEETS_DIR = tweets_dir
    module.NEW_DIR = tweets_dir / "new"
    module.REVIEW_DIR = tweets_dir / "review"
    module.APPROVED_DIR = tweets_dir / "approved"
    module.POSTED_DIR = tweets_dir / "posted"
    module.REJECTED_DIR = tweets_dir / "rejected"
    module.CACHE_DIR = tweets_dir / "cache"
    for directory in (
        module.NEW_DIR,
        module.REVIEW_DIR,
        module.APPROVED_DIR,
        module.POSTED_DIR,
        module.REJECTED_DIR,
        module.CACHE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _write_markdown_draft(
    path: Path,
    *,
    body: str,
    draft_type: str = "tweet",
    in_reply_to: str | None = None,
) -> None:
    frontmatter = [
        "---",
        "status: draft",
        "created: 2026-03-19",
        f"type: {draft_type}",
    ]
    if in_reply_to is not None:
        frontmatter.append(f'in_reply_to: "{in_reply_to}"')
    frontmatter.extend(["---", "", body])
    path.write_text("\n".join(frontmatter) + "\n")


def test_tweet_draft_loads_markdown_frontmatter_body_as_text(
    workflow_module: Any, tmp_path: Path
) -> None:
    draft_path = tmp_path / "manual-draft.md"
    _write_markdown_draft(
        draft_path,
        body="Manual markdown draft body.",
        draft_type="reply",
        in_reply_to="12345",
    )

    draft = workflow_module.TweetDraft.load(draft_path)

    assert draft.text == "Manual markdown draft body."
    assert draft.type == "reply"
    assert draft.in_reply_to == "12345"


def test_list_and_find_drafts_include_markdown_files(
    workflow_module: Any, tmp_path: Path
) -> None:
    _set_status_dirs(workflow_module, tmp_path)
    md_path = workflow_module.NEW_DIR / "manual-draft.md"
    yml_path = workflow_module.NEW_DIR / "tweet_20260319_120000.yml"

    _write_markdown_draft(md_path, body="Manual markdown draft body.")
    yml_path.write_text(
        "text: YAML draft body.\ntype: tweet\ncreated_at: 2026-03-19T12:00:00\n"
    )

    drafts = workflow_module.list_drafts("new")

    assert [path.name for path in drafts] == sorted([md_path.name, yml_path.name])
    assert (
        workflow_module.find_draft("manual-draft", "new", show_error=False) == md_path
    )


def test_move_draft_preserves_markdown_round_trip(
    workflow_module: Any, tmp_path: Path
) -> None:
    _set_status_dirs(workflow_module, tmp_path)
    draft_path = workflow_module.NEW_DIR / "manual-draft.md"
    _write_markdown_draft(draft_path, body="Keep this markdown body intact.")

    moved_path = workflow_module.move_draft(draft_path, "approved")
    moved = workflow_module.TweetDraft.load(moved_path)

    assert moved_path == workflow_module.APPROVED_DIR / "manual-draft.md"
    assert moved.text == "Keep this markdown body intact."


def test_duplicate_reply_detection_reads_markdown_drafts(
    workflow_module: Any, tmp_path: Path
) -> None:
    _set_status_dirs(workflow_module, tmp_path)
    existing_path = workflow_module.APPROVED_DIR / "reply_existing.md"
    _write_markdown_draft(
        existing_path,
        body="Existing reply body.",
        draft_type="reply",
        in_reply_to="4242",
    )

    draft = workflow_module.TweetDraft(
        text="New reply",
        type="reply",
        in_reply_to="4242",
    )
    duplicates = workflow_module._check_for_duplicate_replies_internal(draft)

    assert duplicates == {"approved": [existing_path]}


def test_find_live_duplicate_reply_ids_matches_own_replies(
    workflow_module: Any,
) -> None:
    workflow_module.cached_get_me = lambda *args, **kwargs: SimpleNamespace(
        data=SimpleNamespace(username="TestBotUser")
    )
    workflow_module.get_conversation_thread = lambda *args, **kwargs: [
        {"id": "9001", "author": "TestBotUser", "replied_to_id": "4242"},
        {"id": "9002", "author": "someoneelse", "replied_to_id": "4242"},
        {"id": "9003", "author": "TestBotUser", "replied_to_id": "1111"},
    ]

    duplicate_ids = workflow_module._find_live_duplicate_reply_ids(object(), "4242")

    assert duplicate_ids == ["9001"]


def test_find_live_duplicate_reply_ids_returns_empty_without_identity(
    workflow_module: Any,
) -> None:
    workflow_module.cached_get_me = lambda *args, **kwargs: SimpleNamespace(data=None)
    workflow_module.get_conversation_thread = lambda *args, **kwargs: [
        {"id": "9001", "author": "TestBotUser", "replied_to_id": "4242"},
    ]

    duplicate_ids = workflow_module._find_live_duplicate_reply_ids(object(), "4242")

    assert duplicate_ids == []
