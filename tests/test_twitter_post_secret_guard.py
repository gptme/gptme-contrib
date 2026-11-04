"""Recording-client regressions for the raw CLI's final outbound boundary."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "twitter_secret_guard_loader",
    Path(__file__).with_name("test_twitter_post_url_guard.py"),
)
assert _spec and _spec.loader
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)
twitter_module = _loader.twitter_module


@pytest.fixture
def delivery(twitter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[dict] = []
    marks: list[bool] = []
    output: list[str] = []
    scans: list[str] = []

    def create_tweet(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data={"id": str(len(calls))})

    # Load the actual shared guard even though the CLI fixture stubs gptmail.
    path = (
        Path(__file__).resolve().parents[1]
        / "packages/gptmail/src/gptmail/communication_utils/outbound_redact.py"
    )
    spec = importlib.util.spec_from_file_location(
        "gptmail.communication_utils.outbound_redact", path
    )
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    monkeypatch.setitem(sys.modules, spec.name, guard)

    def scan(text, literals):
        scans.append(text)
        return SimpleNamespace(
            blocked="SYNTHETIC_FORBIDDEN_LITERAL" in text,
            block_reasons=["1× secret_literal (critical)"],
        )

    monkeypatch.setitem(
        sys.modules,
        "redact",
        SimpleNamespace(collect_secret_literals=lambda: [], redact=scan),
    )
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(twitter_module, "POST_DEDUP_DIR", tmp_path / "dedup")
    monkeypatch.setattr(twitter_module, "validate_urls_in_text", lambda text: [])
    monkeypatch.setattr(twitter_module, "_get_user_auth", lambda client: True)
    monkeypatch.setattr(
        twitter_module.console, "print", lambda text: output.append(text)
    )
    monkeypatch.setattr(
        twitter_module,
        "split_thread",
        lambda text: [SimpleNamespace(text=part.strip()) for part in text.split("---")],
    )
    client = SimpleNamespace(create_tweet=create_tweet)
    monkeypatch.setattr(twitter_module, "load_twitter_client", lambda **kwargs: client)
    return SimpleNamespace(
        calls=calls, marks=marks, output=output, scans=scans, client=client
    )


@pytest.mark.parametrize("thread", [False, True])
def test_secret_rejected_before_any_direct_delivery(twitter_module, delivery, thread):
    text = "SYNTHETIC_FORBIDDEN_LITERAL"
    if thread:
        text = "clean opening --- " + text
    with pytest.raises(SystemExit):
        twitter_module._post_now(
            delivery.client,
            text,
            None,
            thread,
            None,
            lambda: delivery.marks.append(True),
        )
    assert delivery.calls == []
    assert delivery.marks == []
    assert "SYNTHETIC_FORBIDDEN_LITERAL" not in "\n".join(delivery.output)


@pytest.mark.parametrize("force", [False, True])
def test_cli_blocks_secret_before_url_or_auth(
    twitter_module, delivery, monkeypatch, force
):
    monkeypatch.setattr(
        twitter_module, "validate_urls_in_text", lambda text: pytest.fail("URL request")
    )
    monkeypatch.setattr(
        twitter_module, "load_twitter_client", lambda **kwargs: pytest.fail("OAuth")
    )
    with pytest.raises(SystemExit):
        twitter_module.post("SYNTHETIC_FORBIDDEN_LITERAL", None, False, force=force)
    assert delivery.calls == []


@pytest.mark.parametrize("failure", ["missing", "scan", "collect"])
def test_scanner_failure_refuses_delivery(
    twitter_module, delivery, monkeypatch, failure
):
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "redact", None)
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("SYNTHETIC_FORBIDDEN_LITERAL")

        monkeypatch.setattr(
            sys.modules["redact"],
            "redact" if failure == "scan" else "collect_secret_literals",
            fail,
        )
    with pytest.raises(SystemExit):
        twitter_module._post_now(
            delivery.client,
            "clean",
            None,
            False,
            None,
            lambda: delivery.marks.append(True),
        )
    assert delivery.calls == []
    assert delivery.marks == []
    assert "SYNTHETIC_FORBIDDEN_LITERAL" not in "\n".join(delivery.output)


@pytest.mark.parametrize(
    ("reply", "quote", "thread", "text", "expected"),
    [
        (None, None, False, "clean", ["clean"]),
        ("123", None, False, "clean reply", ["clean reply"]),
        (None, "456", False, "clean quote", ["clean quote"]),
        (None, None, True, "first --- second", ["first", "second"]),
    ],
)
def test_clean_delivery_preserves_targets_and_thread(
    twitter_module, delivery, reply, quote, thread, text, expected
):
    twitter_module._post_now(
        delivery.client, text, reply, thread, quote, lambda: delivery.marks.append(True)
    )
    assert [call["text"] for call in delivery.calls] == expected
    assert delivery.marks == [True]
    assert delivery.scans
    if thread:
        assert [call["in_reply_to_tweet_id"] for call in delivery.calls] == [None, "1"]
    else:
        assert delivery.calls[0]["in_reply_to_tweet_id"] == reply
        assert delivery.calls[0]["quote_tweet_id"] == quote


def test_real_workspace_scanner_blocks_synthetic_env_literal(
    twitter_module, delivery, monkeypatch, tmp_path
):
    # Optional integration: contrib alone does not ship the workspace scanner.
    # Other tests can leave synthetic token variables behind. Never collect the
    # inherited environment, even when this test is invoked outside env -i.
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TEST_API_TOKEN", "d964-synthetic-token-not-a-real-credential")
    monkeypatch.delitem(sys.modules, "redact")
    scanner = pytest.importorskip("redact")
    with pytest.raises(SystemExit):
        twitter_module._post_now(
            delivery.client,
            "clean --- d964-synthetic-token-not-a-real-credential",
            None,
            True,
            None,
            lambda: delivery.marks.append(True),
        )
    assert scanner.collect_secret_literals() == [
        "d964-synthetic-token-not-a-real-credential"
    ]
    assert delivery.calls == []
    assert delivery.marks == []
    ledger = (tmp_path / "state/outbound-redact-blocks.jsonl").read_text()
    assert "d964-synthetic-token" not in ledger
    assert "d964-synthetic-token" not in "\n".join(delivery.output)


@pytest.mark.parametrize(
    "missing", ["gptmail.communication_utils.outbound_redact", "scanner_dependency"]
)
def test_import_failure_at_sink_is_private_and_blocks(
    twitter_module, delivery, monkeypatch, missing, caplog
):
    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "gptmail.communication_utils.outbound_redact":
            raise ModuleNotFoundError("SYNTHETIC_FORBIDDEN_LITERAL", name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(SystemExit):
        twitter_module._post_now(
            delivery.client,
            "clean",
            None,
            False,
            None,
            lambda: delivery.marks.append(True),
        )
    assert not delivery.calls
    assert not delivery.marks
    assert "SYNTHETIC_FORBIDDEN_LITERAL" not in caplog.text + "\n".join(delivery.output)


def test_actual_thread_splitter_preflights_all_segments(
    twitter_module, delivery, monkeypatch
):
    path = (
        Path(__file__).resolve().parents[1]
        / "packages/gptmail/src/gptmail/communication_utils/messaging/formatting.py"
    )
    spec = importlib.util.spec_from_file_location("twitter_actual_formatting", path)
    assert spec and spec.loader
    formatting = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, formatting)
    spec.loader.exec_module(formatting)
    monkeypatch.setattr(twitter_module, "split_thread", formatting.split_thread)
    with pytest.raises(SystemExit):
        twitter_module._post_now(
            delivery.client,
            "clean\n---\nSYNTHETIC_FORBIDDEN_LITERAL",
            None,
            True,
            None,
            lambda: delivery.marks.append(True),
        )
    assert not delivery.calls
    assert not delivery.marks
    twitter_module._post_now(
        delivery.client,
        "first\n---\nsecond",
        None,
        True,
        None,
        lambda: delivery.marks.append(True),
    )
    assert [call["text"] for call in delivery.calls] == ["first", "second"]
    assert delivery.marks == [True]
