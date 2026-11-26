"""Audible activity and cancellation cues describe attempts, not success."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from gptme_voice.realtime.tool_bridge import (
    DIRECT_TOOL_NAMES,
    GptmeToolBridge,
    PendingTask,
)


async def _flush_cues(bridge):
    await asyncio.gather(*bridge._cue_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(DIRECT_TOOL_NAMES))
async def test_direct_tool_attempt_cues_even_when_unavailable(name):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_dispatch=cue)
    result = await bridge.handle_function_call(name, {})
    await _flush_cues(bridge)
    assert result.get("error") or result.get("status") == "not_supported"
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_direct_tool_activity_cue_is_optional():
    """A bridge with no dispatch callback still runs the tool, silently."""
    rag = AsyncMock()
    rag.enabled = True
    rag.search.return_value = {"results": [{"text": "The matching source"}]}
    bridge = GptmeToolBridge(rag=rag)
    result = await bridge.handle_function_call(
        "workspace_search", {"query": "farewell"}
    )
    await _flush_cues(bridge)
    assert result == rag.search.return_value
    assert not bridge._cue_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["subagent_status", "body_status", "hangup", "unknown", "body_unknown"]
)
async def test_observation_and_unknown_tools_are_silent(name):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_dispatch=cue)
    await bridge.handle_function_call(name, {})
    await _flush_cues(bridge)
    cue.assert_not_awaited()


async def _pending(bridge, task_id):
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(work())
    bridge._pending_tasks[task_id] = PendingTask(
        task=task, description="Test lookup", mode="fast", started_at=time.monotonic()
    )
    await started.wait()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_explicit_cancellation_cues_once_per_request(cancel_all):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    tasks = [await _pending(bridge, "one")]
    if cancel_all:
        tasks.append(await _pending(bridge, "two"))
    arguments = {} if cancel_all else {"task_id": "one"}
    result = await bridge.handle_function_call("subagent_cancel", arguments)
    assert result["status"] == ("cancelled_all" if cancel_all else "cancelled")
    assert all(task.cancelled() for task in tasks)
    await _flush_cues(bridge)
    cue.assert_awaited_once_with()
    await bridge.handle_function_call("subagent_cancel", arguments)
    await _flush_cues(bridge)
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_concurrent_cancel_requests_do_not_duplicate_cue():
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    task = await _pending(bridge, "one")
    await asyncio.gather(
        bridge.handle_function_call("subagent_cancel", {"task_id": "one"}),
        bridge.handle_function_call("subagent_cancel", {"task_id": "one"}),
    )
    assert task.cancelled()
    await _flush_cues(bridge)
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_finished_and_missing_tasks_are_silent():
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    bridge._pending_tasks["finished"] = PendingTask(
        task=finished, description="Done", mode="fast", started_at=time.monotonic()
    )
    for task_id in ("finished", "missing"):
        result = await bridge.handle_function_call(
            "subagent_cancel", {"task_id": task_id}
        )
        assert result["status"] == "not_found"
    await _flush_cues(bridge)
    cue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_completed_work_awaiting_delivery_is_not_cancelled(cancel_all):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    task = await _pending(bridge, "one")
    bridge._pending_tasks["one"].completed_at = time.monotonic()
    arguments = {} if cancel_all else {"task_id": "one"}
    result = await bridge.handle_function_call("subagent_cancel", arguments)
    assert result["status"] == ("no_pending" if cancel_all else "not_pending")
    assert result["message"]
    assert result.get("completed") in (["one"], True)
    assert not task.done()
    await _flush_cues(bridge)
    cue.assert_not_awaited()
    # The work is done but the delivery is still outstanding: keep the entry so
    # the task stays visible to subagent_status instead of becoming an orphan
    # that no later request can see. _run_subagent bounds the delivery and pops
    # the entry itself.
    assert "one" in bridge._pending_tasks
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hung_result_delivery_is_bounded_and_reclaimed(monkeypatch):
    """A stalled delivery cannot keep the task and its entry alive forever."""
    from gptme_voice.realtime import tool_bridge as tb

    monkeypatch.setattr(tb, "_RESULT_DELIVERY_TIMEOUT_SECONDS", 0.05)
    never = asyncio.Event()

    async def hung_delivery(_text):
        await never.wait()

    bridge = GptmeToolBridge(on_result=hung_delivery)

    async def execute(*args, **kwargs):
        return tb.ToolResult(success=True, output="done")

    monkeypatch.setattr(bridge, "_execute", execute)
    receipt = await bridge.handle_function_call("subagent", {"task": "Test lookup"})
    task_id = receipt["task_id"]
    task = bridge._pending_tasks[task_id].task
    await asyncio.wait_for(asyncio.shield(task), timeout=5)
    assert task.done() and not task.cancelled()
    assert task_id not in bridge._pending_tasks


@pytest.mark.asyncio
async def test_cancel_reclaims_a_task_that_ended_with_an_exception():
    """A task that raised before its own pop is still reclaimed by a cancel."""
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)

    async def raises():
        raise RuntimeError("delivery failed")

    task = asyncio.create_task(raises())
    bridge._pending_tasks["one"] = PendingTask(
        task=task, description="Broken", mode="fast", started_at=time.monotonic()
    )
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()

    result = await bridge.handle_function_call("subagent_cancel", {"task_id": "one"})
    assert result["status"] == "not_found"
    assert "one" not in bridge._pending_tasks
    await _flush_cues(bridge)
    cue.assert_not_awaited()


async def _dispatch_blocked_subagent(bridge, monkeypatch):
    """Start the real subagent lifecycle with a subprocess that never finishes."""
    started = asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge, "_execute", execute)
    receipt = await bridge.handle_function_call("subagent", {"task": "Test lookup"})
    await started.wait()
    return receipt["task_id"], bridge._pending_tasks[receipt["task_id"]].task


@pytest.mark.asyncio
async def test_cleanup_cancellation_outside_the_tool_call_is_silent(monkeypatch):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    _, task = await _dispatch_blocked_subagent(bridge, monkeypatch)
    task.cancel()  # Transport cleanup does not represent an explicit request.
    await asyncio.gather(task, return_exceptions=True)
    await _flush_cues(bridge)
    assert task.cancelled()
    cue.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_cancellation_report_still_confirms_cancellation(monkeypatch):
    cue = AsyncMock()
    on_result = AsyncMock(side_effect=ConnectionError("provider socket closing"))
    bridge = GptmeToolBridge(on_cancel=cue, on_result=on_result)
    task_id, task = await _dispatch_blocked_subagent(bridge, monkeypatch)
    result = await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})
    await _flush_cues(bridge)
    on_result.assert_awaited_once()
    assert result["status"] == "cancelled"
    assert task.cancelled()
    assert task_id not in bridge._pending_tasks
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_overlapping_cancel_reports_cancellation_in_progress(
    monkeypatch, cancel_all
):
    cue = AsyncMock()
    reporting = asyncio.Event()
    release = asyncio.Event()

    async def slow_report(_text):
        reporting.set()
        await release.wait()

    bridge = GptmeToolBridge(on_cancel=cue, on_result=slow_report)
    task_id, task = await _dispatch_blocked_subagent(bridge, monkeypatch)
    arguments = {} if cancel_all else {"task_id": task_id}
    first = asyncio.create_task(
        bridge.handle_function_call("subagent_cancel", arguments)
    )
    await reporting.wait()

    second = await bridge.handle_function_call("subagent_cancel", arguments)
    assert second["status"] == "cancelling"
    assert not task.done()

    release.set()
    assert (await first)["status"] == ("cancelled_all" if cancel_all else "cancelled")
    await _flush_cues(bridge)
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_activity_cue_does_not_hide_workspace_search_result():
    cue = AsyncMock(side_effect=ConnectionError("audio transport closed"))
    rag = AsyncMock()
    rag.enabled = True
    rag.search.return_value = {"results": [{"text": "The matching source"}]}
    bridge = GptmeToolBridge(on_dispatch=cue, rag=rag)
    result = await bridge.handle_function_call(
        "workspace_search", {"query": "farewell"}
    )
    await _flush_cues(bridge)
    assert result == rag.search.return_value
    rag.search.assert_awaited_once_with("farewell", n_results=5)
    cue.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_task_that_finishes_normally_during_cancel_has_no_cancel_cue(
    cancel_all,
):
    cue = AsyncMock()
    bridge = GptmeToolBridge(on_cancel=cue)
    started = asyncio.Event()

    async def finishes_on_cancel():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "Work already finished"

    task = asyncio.create_task(finishes_on_cancel())
    bridge._pending_tasks["one"] = PendingTask(
        task=task, description="Finishing", mode="fast", started_at=time.monotonic()
    )
    await started.wait()
    arguments = {} if cancel_all else {"task_id": "one"}
    result = await bridge.handle_function_call("subagent_cancel", arguments)
    assert task.done() and not task.cancelled()
    if cancel_all:
        # Nothing was cancelled, so the response must say so and explain itself
        # instead of reporting an empty cancelled_all.
        assert result["status"] == "no_pending"
        assert result["cancelled_count"] == 0
        assert result["message"]
    else:
        assert result["status"] == "not_pending"
    await _flush_cues(bridge)
    cue.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_cancel_all_reports_cancelled_and_cancelling(monkeypatch):
    """Cancel-all over a task already mid-cancel reports both outcomes."""
    cue = AsyncMock()
    reporting = asyncio.Event()
    release = asyncio.Event()

    async def slow_report(_text):
        reporting.set()
        await release.wait()

    bridge = GptmeToolBridge(on_cancel=cue, on_result=slow_report)
    first_id, first_task = await _dispatch_blocked_subagent(bridge, monkeypatch)
    second_task = await _pending(bridge, "second")

    # Start a single-task cancel that blocks in its cancellation report.
    single = asyncio.create_task(
        bridge.handle_function_call("subagent_cancel", {"task_id": first_id})
    )
    await reporting.wait()

    result = await bridge.handle_function_call("subagent_cancel", {})
    assert result["status"] == "cancelled_all"
    assert result["cancelled_count"] == 1
    assert result["cancelling"] == [first_id]
    assert [entry["task_id"] for entry in result["cancelled"]] == ["second"]

    release.set()
    assert (await single)["status"] == "cancelled"
    assert first_task.cancelled() and second_task.cancelled()
    await _flush_cues(bridge)
    # Two requests each confirmed exactly one cancellation (cancel-all -> second,
    # single -> first), so two cues — never two for the same task.
    assert cue.await_count == 2


@pytest.mark.asyncio
async def test_failed_cancellation_cue_preserves_cancellation_receipt():
    cue = AsyncMock(side_effect=ConnectionError("audio transport closed"))
    bridge = GptmeToolBridge(on_cancel=cue)
    task = await _pending(bridge, "one")
    result = await bridge.handle_function_call("subagent_cancel", {"task_id": "one"})
    await _flush_cues(bridge)
    assert result["status"] == "cancelled"
    assert task.cancelled()
    cue.assert_awaited_once_with()
