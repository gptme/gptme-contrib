"""Offline contract tests for shared advisory lesson selection."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from gptme_lessons_extras.selector import select


def request(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": 1,
        "runtime": "gptme",
        "session_id": "session-a",
        "event_kind": "user_prompt",
        "context_fingerprint": "context-a",
        "policy_version": "policy-a",
        "query": "How do I safely commit changes?",
        "candidates": [
            {"id": "commit", "description": "Commit only owned paths."},
            {"id": "test", "description": "Run focused tests."},
            {"id": "review", "description": "Review changes before committing."},
        ],
        "fallback_ids": ["test", "commit"],
        "max_results": 2,
        "model": "offline-test-model",
        "timeout_seconds": 2.0,
        "max_calls_per_session": 8,
    }
    value.update(overrides)
    return value


def worker(
    tmp_path: Path, response: dict[str, Any] | str, *, body: str = ""
) -> list[str]:
    """Create a real child process; no network/provider package is involved."""
    script = tmp_path / "provider.py"
    output = response if isinstance(response, str) else json.dumps(response)
    script.write_text(
        "import json, os, sys, time\n"
        "request = json.load(sys.stdin)\n"
        f"{body}\n"
        f"print({output!r})\n"
    )
    return [sys.executable, str(script)]


def selected(*ids: str) -> dict[str, Any]:
    return {"version": 1, "status": "selected", "ids": list(ids)}


def test_selected_ids_preserve_provider_order_deduplicate_then_cap(
    tmp_path: Path,
) -> None:
    result = select(
        request(),
        state_dir=tmp_path,
        worker_command=worker(tmp_path, selected("review", "review", "commit", "test")),
    )
    assert result["status"] == "selected"
    assert result["ids"] == ["review", "commit"]
    assert result["cached"] is False
    assert result["calls_used"] == 1
    assert result["version"] == 1
    assert result["runtime"] == "gptme"
    assert result["session_id"] == "session-a"
    assert result["event_kind"] == "user_prompt"
    assert result["fingerprint"]
    assert result["elapsed_ms"] >= 0


def test_explicit_empty_is_success_and_cacheable_on_tool_events(tmp_path: Path) -> None:
    command = worker(tmp_path, {"version": 1, "status": "empty", "ids": []})
    initial = select(request(), state_dir=tmp_path, worker_command=command)
    assert initial["status"] == "empty"
    assert initial["ids"] == []
    assert initial["calls_used"] == 1
    Path(command[1]).unlink()

    cached = select(
        request(event_kind="tool"), state_dir=tmp_path, worker_command=command
    )
    assert cached["status"] == "empty"
    assert cached["ids"] == []
    assert cached["cached"] is True
    assert cached["calls_used"] == 1
    assert cached["event_kind"] == "tool"
    assert cached["fingerprint"] == initial["fingerprint"]


def test_receipts_distinguish_selection_from_cache_without_logging_query(
    tmp_path: Path,
) -> None:
    command = worker(tmp_path, selected("review"))
    select(request(query="private prompt"), state_dir=tmp_path, worker_command=command)
    select(
        request(query="private prompt", event_kind="tool"),
        state_dir=tmp_path,
        worker_command=command,
    )
    raw = (tmp_path / "receipts.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 2
    assert [row["cached"] for row in rows] == [False, True]
    assert [row["calls_used"] for row in rows] == [1, 1]
    assert all(
        row["record_type"] == "selection_receipt" and row["timestamp"] for row in rows
    )
    assert "private prompt" not in raw
    assert "Commit only owned paths." not in raw


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"version": 1, "status": "selected", "ids": ["commit"]',
        "[]",
        {"version": 2, "status": "selected", "ids": ["commit"]},
        {"status": "selected", "ids": ["commit"]},
        {"version": 1, "status": "selected", "ids": []},
        {"version": 1, "status": "empty", "ids": ["commit"]},
        {"version": 1, "status": "selected", "ids": "commit"},
        {"version": 1, "status": "selected", "ids": [42]},
        {"version": 1, "status": "mystery", "ids": ["commit"]},
        selected("commit", "../../secret.md"),
    ],
)
def test_malformed_or_unknown_output_falls_back_not_empty(
    tmp_path: Path, response: dict[str, Any] | str
) -> None:
    result = select(
        request(), state_dir=tmp_path, worker_command=worker(tmp_path, response)
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "invalid_response"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 1
    assert result["cached"] is False


def test_fallback_is_filtered_deduplicated_and_capped(tmp_path: Path) -> None:
    result = select(
        request(fallback_ids=["unknown", "review", "review", "commit", "test"]),
        state_dir=tmp_path,
        worker_command=worker(tmp_path, "broken"),
    )
    assert result["status"] == "fallback"
    assert result["ids"] == ["review", "commit"]


def test_worker_declared_fallback_cannot_choose_ineligible_content(
    tmp_path: Path,
) -> None:
    result = select(
        request(),
        state_dir=tmp_path,
        worker_command=worker(
            tmp_path,
            {"version": 1, "status": "fallback", "ids": [], "reason": "provider_error"},
        ),
    )
    assert result["status"] == "fallback"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 1


def test_fallback_without_reason_is_not_labelled_selected(tmp_path: Path) -> None:
    result = select(
        request(),
        state_dir=tmp_path,
        worker_command=worker(
            tmp_path,
            {"version": 1, "status": "fallback", "ids": []},
        ),
    )
    assert result["status"] == "fallback"
    assert result["ids"] == ["test", "commit"]
    assert result["reason"] == "fallback"


def test_failed_provider_decision_is_not_cached(tmp_path: Path) -> None:
    command = worker(tmp_path, "broken")
    failed = select(request(), state_dir=tmp_path, worker_command=command)
    assert failed["status"] == "fallback"
    command = worker(tmp_path, selected("commit"))
    recovered = select(request(), state_dir=tmp_path, worker_command=command)
    assert recovered["status"] == "selected"
    assert recovered["ids"] == ["commit"]
    assert recovered["cached"] is False
    assert recovered["calls_used"] == 2


def test_tool_event_without_cache_never_invokes_provider(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    command = worker(
        tmp_path,
        selected("review"),
        body=f"open({str(marker)!r}, 'w').write('called')",
    )
    result = select(
        request(event_kind="tool"), state_dir=tmp_path, worker_command=command
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "cache_miss"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 0
    assert not marker.exists()


def test_cached_selection_reused_without_provider_at_zero_remaining_calls(
    tmp_path: Path,
) -> None:
    command = worker(tmp_path, selected("review"))
    initial = select(
        request(max_calls_per_session=1), state_dir=tmp_path, worker_command=command
    )
    Path(command[1]).unlink()
    result = select(
        request(max_calls_per_session=1, event_kind="tool"),
        state_dir=tmp_path,
        worker_command=command,
    )
    assert result["status"] == "selected"
    assert result["ids"] == ["review"]
    assert result["cached"] is True
    assert result["calls_used"] == initial["calls_used"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"query": "A new topic"},
        {"context_fingerprint": "context-b"},
        {"policy_version": "policy-b"},
        {"model": "different-offline-model"},
        {"max_results": 1},
        {"max_calls_per_session": 9},
        {"fallback_ids": ["review"]},
        {
            "candidates": [
                {"id": "commit", "description": "Changed advice"},
                {"id": "test", "description": "Run focused tests."},
            ]
        },
        {
            "candidates": [
                {
                    "id": "commit",
                    "description": "Commit only owned paths.",
                    "metadata": {"revision": 2},
                },
                {"id": "test", "description": "Run focused tests."},
                {"id": "review", "description": "Review changes before committing."},
            ]
        },
    ],
)
def test_tool_cache_fingerprint_invalidates_every_selection_input(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    command = worker(tmp_path, selected("commit"))
    original = request()
    first = select(original, state_dir=tmp_path, worker_command=command)
    altered = deepcopy(original)
    altered.update(change)
    altered["event_kind"] = "tool"
    second = select(altered, state_dir=tmp_path, worker_command=command)
    assert second["status"] == "fallback"
    assert second["reason"] == "cache_miss"
    assert second["fingerprint"] != first["fingerprint"]
    assert second["calls_used"] == 1


def test_timeout_is_not_part_of_successful_decision_cache_key(tmp_path: Path) -> None:
    command = worker(tmp_path, selected("commit"))
    first = select(request(), state_dir=tmp_path, worker_command=command)
    second = select(
        request(timeout_seconds=1.0, event_kind="tool"),
        state_dir=tmp_path,
        worker_command=command,
    )
    assert second["status"] == "selected"
    assert second["cached"] is True
    assert second["fingerprint"] == first["fingerprint"]


def test_cache_is_isolated_by_runtime_and_session(tmp_path: Path) -> None:
    command = worker(tmp_path, selected("commit"))
    select(request(), state_dir=tmp_path, worker_command=command)
    for overrides in ({"runtime": "claude-code"}, {"session_id": "session-b"}):
        result = select(
            request(event_kind="tool", **overrides),
            state_dir=tmp_path,
            worker_command=command,
        )
        assert result["status"] == "fallback"
        assert result["reason"] == "cache_miss"
        assert result["calls_used"] == 0


def test_session_budget_does_not_reset_on_topic_changes(tmp_path: Path) -> None:
    command = worker(tmp_path, selected("commit"))
    first = select(
        request(max_calls_per_session=1), state_dir=tmp_path, worker_command=command
    )
    second = select(
        request(query="Another topic", max_calls_per_session=1),
        state_dir=tmp_path,
        worker_command=command,
    )
    assert first["status"] == "selected"
    assert second["status"] == "fallback"
    assert second["reason"] == "budget_exhausted"
    assert second["calls_used"] == 1


def test_failed_attempt_consumes_session_budget(tmp_path: Path) -> None:
    command = worker(tmp_path, "broken")
    first = select(
        request(max_calls_per_session=1), state_dir=tmp_path, worker_command=command
    )
    second = select(
        request(max_calls_per_session=1), state_dir=tmp_path, worker_command=command
    )
    assert first["reason"] == "invalid_response"
    assert second["reason"] == "budget_exhausted"
    assert second["calls_used"] == 1


def test_concurrent_distinct_topics_share_one_atomic_session_budget(
    tmp_path: Path,
) -> None:
    command = worker(tmp_path, selected("commit"), body="time.sleep(0.1)")

    def invoke(topic: int) -> dict[str, Any]:
        return select(
            request(query=f"Topic {topic}", max_calls_per_session=1),
            state_dir=tmp_path,
            worker_command=command,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(invoke, range(4)))
    assert sum(result["status"] == "selected" for result in results) == 1
    assert sum(result["reason"] == "budget_exhausted" for result in results) == 3
    assert all(result["calls_used"] == 1 for result in results)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"model": None}, "missing_model"),
        ({"max_calls_per_session": 0}, "budget_exhausted"),
    ],
)
def test_missing_authorization_does_not_invoke_worker(
    tmp_path: Path, overrides: dict[str, Any], reason: str
) -> None:
    marker = tmp_path / "called"
    command = worker(
        tmp_path, selected("commit"), body=f"open({str(marker)!r}, 'w').write('called')"
    )
    result = select(request(**overrides), state_dir=tmp_path, worker_command=command)
    assert result["status"] == "fallback"
    assert result["reason"] == reason
    assert result["calls_used"] == 0
    assert not marker.exists()


def test_deadline_kills_and_reaps_owned_provider_process(tmp_path: Path) -> None:
    pid_path = tmp_path / "provider.pid"
    command = worker(
        tmp_path,
        selected("commit"),
        body=f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)",
    )
    start = time.monotonic()
    result = select(
        request(timeout_seconds=1.0, max_calls_per_session=1),
        state_dir=tmp_path,
        worker_command=command,
    )
    assert time.monotonic() - start < 5
    assert result["status"] == "fallback"
    assert result["reason"] == "timeout"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 1
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_worker_nonzero_exit_uses_fallback_and_counts_attempt(tmp_path: Path) -> None:
    result = select(
        request(),
        state_dir=tmp_path,
        worker_command=worker(tmp_path, selected("commit"), body="sys.exit(7)"),
    )
    assert result["status"] == "fallback"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_non_utf8_worker_bytes_do_not_escape_protocol(
    tmp_path: Path, stream: str
) -> None:
    result = select(
        request(),
        state_dir=tmp_path,
        worker_command=worker(
            tmp_path,
            selected("commit"),
            body=f"sys.{stream}.buffer.write(bytes([255])); sys.{stream}.flush()",
        ),
    )
    if stream == "stdout":
        assert result["status"] == "fallback"
        assert result["reason"] == "invalid_response"
        assert result["ids"] == ["test", "commit"]
    else:
        assert result["status"] == "selected"
        assert result["ids"] == ["commit"]
    assert result["calls_used"] == 1


def test_cache_write_contention_uses_remaining_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from gptme_lessons_extras import selector

    # Advance only the selector's clock to model a worker consuming most of
    # its budget. SQLite still waits in real time, under a real competing lock.
    # This avoids requiring tight worker scheduling on busy CI hosts.
    elapsed_worker_time = 0.0
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        selector,
        "time",
        SimpleNamespace(monotonic=lambda: real_monotonic() + elapsed_worker_time),
    )
    blocker = sqlite3.connect(tmp_path / "selector.sqlite3")

    def complete_worker(
        *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal elapsed_worker_time
        blocker.execute("BEGIN IMMEDIATE")
        elapsed_worker_time = 0.75
        return subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(selected("commit")).encode(), stderr=b""
        )

    monkeypatch.setattr(selector.subprocess, "run", complete_worker)
    start = real_monotonic()
    try:
        result = select(request(timeout_seconds=1.0), state_dir=tmp_path)
    finally:
        blocker.close()
    duration = real_monotonic() - start

    assert result["status"] == "fallback"
    assert result["reason"] in ("state_unavailable", "timeout")
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 1
    # Remaining budget is 250ms; allow 350ms scheduling slack. The old
    # implementation waited a whole additional second on this cache write.
    assert duration < 0.6


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"event_kind": "unknown"},
        {"max_results": -1},
        {"max_calls_per_session": -1},
        {"timeout_seconds": -1},
        {"candidates": "not a catalog"},
        {"fallback_ids": "commit"},
    ],
)
def test_invalid_request_is_untrusted_and_never_selects_fallback_paths(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    result = select(request(**change), state_dir=tmp_path)
    assert result["status"] == "fallback"
    assert result["reason"] == "invalid_request"
    assert result["ids"] == []
    assert result["calls_used"] == 0


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_json_cli_transport_has_offline_deterministic_fallback(
    tmp_path: Path, source: str
) -> None:
    payload = json.dumps(request(max_calls_per_session=0))
    input_path = tmp_path / "request.json"
    input_path.write_text(payload)
    env = os.environ.copy()
    package_src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [package_src, env.get("PYTHONPATH")])
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_lessons_extras.selector",
            "--context-json",
            "-" if source == "stdin" else str(input_path),
            "--state-dir",
            str(tmp_path / "cli-state"),
        ],
        input=payload if source == "stdin" else None,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=True,
    )
    result = json.loads(process.stdout)
    assert result["version"] == 1
    assert result["status"] == "fallback"
    assert result["reason"] == "budget_exhausted"
    assert result["ids"] == ["test", "commit"]
    assert result["calls_used"] == 0


@pytest.fixture
def compatible_core_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply #4282's malformed-envelope signal without replacing core.select.

    The installed gptme can predate that fix. These are adapter/provider-boundary
    tests, not a replacement for core's own exhaustive parser regression suite.
    """
    from gptme.context.selector.llm_based import LLMSelector

    installed_parser = LLMSelector._parse_response

    def parse_response(self: Any, response: str) -> list[str]:
        if not response.startswith("<selected>") or not response.endswith(
            "</selected>"
        ):
            raise ValueError("malformed selection envelope")
        return installed_parser(self, response)

    monkeypatch.setattr(LLMSelector, "_parse_response", parse_response)


def test_worker_rejects_ambiguous_installed_parser_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gptme.llm
    from gptme.context.selector.llm_based import LLMSelector
    from gptme_lessons_extras._selector_worker import select_with_core
    from gptme_lessons_extras.selector import validate_request

    monkeypatch.setattr(LLMSelector, "_parse_response", lambda self, text: [])

    def forbidden_reply(**kwargs: Any) -> None:
        pytest.fail("An incompatible core must not make a paid provider call")

    monkeypatch.setattr(gptme.llm, "reply", forbidden_reply)
    result = select_with_core(validate_request(request()))
    assert result == {
        "version": 1,
        "status": "fallback",
        "ids": [],
        "reason": "unsupported_core",
    }


@pytest.mark.usefixtures("compatible_core_parser")
@pytest.mark.parametrize(
    ("response", "status", "ids"),
    [
        ("<selected></selected>", "empty", []),
        ("<selected> \n </selected>", "empty", []),
        (
            "<selected>review\nreview\ncommit\ntest</selected>",
            "selected",
            ["review", "commit"],
        ),
    ],
)
def test_worker_uses_core_provider_stack_and_normalizes_advisory_decision(
    monkeypatch: pytest.MonkeyPatch, response: str, status: str, ids: list[str]
) -> None:
    from types import SimpleNamespace

    import gptme.llm
    from gptme_lessons_extras._selector_worker import select_with_core
    from gptme_lessons_extras.selector import validate_request

    calls: list[dict[str, Any]] = []

    def reply(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(content=response)

    monkeypatch.setattr(gptme.llm, "reply", reply)
    result = select_with_core(validate_request(request()))
    assert result == {"version": 1, "status": status, "ids": ids}
    assert len(calls) == 1
    assert calls[0]["model"] == "offline-test-model"
    assert calls[0]["workspace"] is None
    messages = calls[0]["messages"]
    assert any("Commit only owned paths." in message.content for message in messages)
    assert any(
        "How do I safely commit changes?" in message.content for message in messages
    )


@pytest.mark.usefixtures("compatible_core_parser")
@pytest.mark.parametrize(
    "response",
    [
        "I cannot decide",
        "<selected>commit",
        "<selected>commit\nunknown-id</selected>",
    ],
)
def test_worker_invalid_core_response_does_not_become_empty_or_partial_selection(
    monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    from types import SimpleNamespace

    import gptme.llm
    from gptme_lessons_extras._selector_worker import select_with_core
    from gptme_lessons_extras.selector import validate_request

    monkeypatch.setattr(
        gptme.llm, "reply", lambda **kwargs: SimpleNamespace(content=response)
    )
    with pytest.raises(ValueError):
        select_with_core(validate_request(request()))


def _worker_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    import io

    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )


@pytest.mark.usefixtures("compatible_core_parser")
def test_worker_main_separates_provider_diagnostics_from_json_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    import gptme.llm
    from gptme_lessons_extras import _selector_worker

    def noisy_reply(**kwargs: Any) -> SimpleNamespace:
        print("Provider configuration diagnostic")
        return SimpleNamespace(content="<selected>commit</selected>")

    monkeypatch.setattr(gptme.llm, "reply", noisy_reply)
    _worker_stdin(monkeypatch, request())
    _selector_worker.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "version": 1,
        "status": "selected",
        "ids": ["commit"],
    }
    assert "Provider configuration diagnostic" in captured.err


@pytest.mark.usefixtures("compatible_core_parser")
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ImportError("missing provider dependency"), "missing_dependency"),
        (ValueError("malformed response"), "invalid_response"),
        (RuntimeError("provider unavailable"), "provider_error"),
    ],
)
def test_worker_main_encodes_provider_failures_as_protocol_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    reason: str,
) -> None:
    import gptme.llm
    from gptme_lessons_extras import _selector_worker

    def failing_reply(**kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(gptme.llm, "reply", failing_reply)
    _worker_stdin(monkeypatch, request())
    _selector_worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result == {"version": 1, "status": "fallback", "ids": [], "reason": reason}


def test_no_candidates_empty_decision_is_cached_for_later_tool_event(
    tmp_path: Path,
) -> None:
    """An empty catalog short-circuit must satisfy the cacheable-none contract."""
    # No worker needed: no candidates short-circuits before the provider call.
    initial = select(request(candidates=[]), state_dir=tmp_path, worker_command=None)
    assert initial["status"] == "empty"
    assert initial["reason"] == "no_candidates"
    assert initial["cached"] is False

    cached = select(request(candidates=[], event_kind="tool"), state_dir=tmp_path)
    assert cached["status"] == "empty"
    assert cached["ids"] == []
    assert cached["cached"] is True
    assert cached["fingerprint"] == initial["fingerprint"]
