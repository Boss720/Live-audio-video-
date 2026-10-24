"""Unit tests for ``core.dispatcher``."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from audit.journal import Journal
from core.commands import GatingMode
from core.dispatcher import _TOOL_TABLE, ToolDispatcher
from core.events import (
    ToolApprovalRequested,
    ToolCallCancelled,
    ToolCallReceived,
    ToolResultSent,
)
from policy import PolicyEngine
from tools._state import set_policy_engine, set_workspace_root
from tools.registry import ComputerClickArgs, ComputerKeyArgs, ComputerTypeArgs, ToolResult


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def policy(workspace: Path) -> PolicyEngine:
    return PolicyEngine(
        allow_roots=[workspace],
        deny_globs=[],
        never_allow_regex=[],
    )


@pytest.fixture
def event_queue() -> asyncio.Queue[Any]:
    return asyncio.Queue()


@pytest.fixture
def journal(tmp_path: Path) -> Generator[Journal, None, None]:
    journal = Journal(run_id="test-run", log_dir=tmp_path)
    yield journal
    journal._close()


@pytest.fixture
def dispatcher(
    policy: PolicyEngine,
    event_queue: asyncio.Queue[Any],
    journal: Journal,
) -> ToolDispatcher:
    set_policy_engine(policy)
    set_workspace_root(policy._allow_roots[0])
    return ToolDispatcher(
        policy=policy,
        journal=journal,
        event_queue=event_queue,
        run_id="test-run",
    )


async def drain(queue: asyncio.Queue[Any]) -> list[Any]:
    items: list[Any] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _capture_callback(
    responses: list[tuple[str, str, ToolResult]],
) -> Callable[[str, str, ToolResult], Awaitable[bool]]:
    async def respond(call_id: str, name: str, result: ToolResult) -> bool:
        responses.append((call_id, name, result))
        return True

    return respond


@pytest.mark.asyncio
async def test_unknown_tool(
    dispatcher: ToolDispatcher, event_queue: asyncio.Queue[Any]
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    await dispatcher.submit("call-1", "not_a_tool", {})
    events = await drain(event_queue)

    assert any(
        isinstance(e, ToolCallReceived) and e.call_id == "call-1" for e in events
    )
    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert "Unknown tool" in result_event.result
    assert len(responses) == 1
    assert responses[0][2].ok is False


@pytest.mark.asyncio
async def test_invalid_arguments(
    dispatcher: ToolDispatcher, event_queue: asyncio.Queue[Any]
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    # read_file requires a ``path`` argument.
    await dispatcher.submit("call-2", "read_file", {})
    events = await drain(event_queue)

    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert "Invalid arguments" in result_event.result


@pytest.mark.asyncio
async def test_policy_denies_path_outside_root(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    workspace: Path,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    outside = workspace.parent / "outside.txt"
    await dispatcher.submit("call-3", "read_file", {"path": str(outside)})
    events = await drain(event_queue)

    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert "Policy denied" in result_event.result


@pytest.mark.asyncio
async def test_approval_flow(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    workspace: Path,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    target = workspace / "approved.txt"
    task = asyncio.create_task(
        dispatcher.submit(
            "call-4",
            "write_file",
            {"path": str(target), "content": "hello"},
        )
    )

    # Yield so the dispatcher reaches the approval wait.
    await asyncio.sleep(0)
    events = await drain(event_queue)
    assert any(
        isinstance(e, ToolApprovalRequested) and e.call_id == "call-4"
        for e in events
    )

    dispatcher.approve("call-4")
    await task

    events = await drain(event_queue)
    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is True
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_cancel_pending(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    workspace: Path,
) -> None:
    target = workspace / "cancelled.txt"
    task = asyncio.create_task(
        dispatcher.submit(
            "call-5",
            "write_file",
            {"path": str(target), "content": "hello"},
        )
    )

    await asyncio.sleep(0)
    dispatcher.cancel(["call-5"])
    await task

    events = await drain(event_queue)
    assert any(
        isinstance(e, ToolCallCancelled) and "call-5" in e.call_ids for e in events
    )
    # The file should not have been written because approval was cancelled.
    assert not target.exists()


@pytest.mark.asyncio
async def test_approval_timeout(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    workspace: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("core.dispatcher.APPROVAL_TIMEOUT_SECONDS", 0.01)

    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    target = workspace / "timeout.txt"
    await dispatcher.submit(
        "call-6",
        "write_file",
        {"path": str(target), "content": "hello"},
    )
    events = await drain(event_queue)

    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert "approval_timeout" in result_event.result
    assert not target.exists()


async def test_aclose_cancels_in_flight_tasks(dispatcher: ToolDispatcher) -> None:
    """Shutdown cancels tracked tasks instead of leaving them pending.

    Regression: tool calls were spawned with a bare ``asyncio.create_task`` and
    never cancelled, so tearing down the loop printed "Task was destroyed but it
    is pending!" and dropped the call.
    """
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    task = dispatcher.spawn(never_finishes())
    await started.wait()
    assert not task.done()

    await dispatcher.aclose()

    assert task.cancelled()
    assert dispatcher._tasks == set()


async def test_spawn_keeps_a_strong_reference(dispatcher: ToolDispatcher) -> None:
    """A spawned task is retained until it completes, then released.

    asyncio holds only a weak reference to a running task, so an untracked one
    can be garbage-collected mid-call.
    """
    release = asyncio.Event()

    async def waits() -> None:
        await release.wait()

    task = dispatcher.spawn(waits())
    assert task in dispatcher._tasks

    release.set()
    await task
    # The done callback drops it, so the set does not grow without bound.
    assert task not in dispatcher._tasks


@pytest.mark.asyncio
async def test_desktop_actions_and_frames_follow_submission_order(
    dispatcher: ToolDispatcher, monkeypatch: Any, workspace: Path
) -> None:
    """Fake delayed click, typing and Enter must finish with their own frames."""
    dispatcher.set_gating_mode(GatingMode.YOLO)
    loop = asyncio.get_running_loop()
    click_started = asyncio.Event()
    release_click = threading.Event()
    frame_started = asyncio.Event()
    release_frame = asyncio.Event()
    order: list[str] = []

    def click(args: Any, *, call_id: str) -> ToolResult:
        loop.call_soon_threadsafe(click_started.set)
        assert release_click.wait(3)
        order.append("click")
        return ToolResult(True, "clicked")

    def action(args: Any, *, call_id: str) -> ToolResult:
        order.append(call_id)
        return ToolResult(True, call_id)

    monkeypatch.setitem(_TOOL_TABLE, "computer_left_click", (click, ComputerClickArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_type", (action, ComputerTypeArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_key", (action, ComputerKeyArgs))

    async def respond(call_id: str, name: str, result: ToolResult) -> bool:
        if call_id == "click":
            frame_started.set()
            await release_frame.wait()
        if name.startswith("computer_"):
            order.append(f"frame:{call_id}")
        return True

    dispatcher.set_respond_callback(respond)
    tasks = [dispatcher.spawn(dispatcher.submit("click", "computer_left_click", {"x": 1, "y": 1}))]
    try:
        await asyncio.wait_for(click_started.wait(), 1)
        tasks.extend(
            [
                dispatcher.spawn(dispatcher.submit("type", "computer_type", {"text": "hello"})),
                dispatcher.spawn(dispatcher.submit("enter", "computer_key", {"keys": "enter"})),
            ]
        )
        target = workspace / "read.txt"
        target.write_text("available", encoding="utf-8")
        # An unrelated read completes even while the desktop is occupied.
        await asyncio.wait_for(dispatcher.submit("read", "read_file", {"path": str(target)}), 1)
        assert order == []
        release_click.set()
        await asyncio.wait_for(frame_started.wait(), 1)
        await asyncio.sleep(0)
        assert order == ["click"]
        release_frame.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order == ["click", "frame:click", "type", "frame:type", "enter", "frame:enter"]
    finally:
        release_click.set()
        release_frame.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_cancel", [True, False])
async def test_cancelled_desktop_waiter_cannot_release_predecessor(
    dispatcher: ToolDispatcher,
    monkeypatch: Any,
    user_cancel: bool,
    event_queue: asyncio.Queue[Any],
) -> None:
    dispatcher.set_gating_mode(GatingMode.YOLO)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    order: list[str] = []
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    def click(args: Any, *, call_id: str) -> ToolResult:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        order.append(call_id)
        return ToolResult(True, call_id)

    def action(args: Any, *, call_id: str) -> ToolResult:
        order.append(call_id)
        return ToolResult(True, call_id)

    monkeypatch.setitem(_TOOL_TABLE, "computer_left_click", (click, ComputerClickArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_type", (action, ComputerTypeArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_key", (action, ComputerKeyArgs))
    first = dispatcher.spawn(dispatcher.submit("click", "computer_left_click", {"x": 1, "y": 1}))
    tasks = [first]
    try:
        await asyncio.wait_for(started.wait(), 1)
        cancelled = dispatcher.spawn(dispatcher.submit("type", "computer_type", {"text": "skip"}))
        tasks.append(cancelled)
        last = dispatcher.spawn(dispatcher.submit("enter", "computer_key", {"keys": "enter"}))
        tasks.append(last)
        await asyncio.sleep(0)
        # Queued input is still audited and shown as received immediately.
        received = [
            event.call_id
            for event in await drain(event_queue)
            if isinstance(event, ToolCallReceived)
        ]
        assert received == ["click", "type", "enter"]
        if user_cancel:
            dispatcher.cancel(["type"])
        else:
            cancelled.cancel()
        await asyncio.wait_for(asyncio.gather(cancelled, return_exceptions=True), 1)
        assert order == []
        assert not last.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, last), 2)
        assert order == ["click", "enter"]
        assert [item[0] for item in responses] == ["click", "enter"]
        assert dispatcher._desktop_tail is None
        assert dispatcher._desktop_waiters == {}
        assert dispatcher._cancelled_ids == set()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_desktop_denial_releases_next_approval_in_order(
    dispatcher: ToolDispatcher, monkeypatch: Any, event_queue: asyncio.Queue[Any]
) -> None:
    actions: list[str] = []
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    def action(args: Any, *, call_id: str) -> ToolResult:
        actions.append(call_id)
        return ToolResult(True, call_id)

    monkeypatch.setitem(_TOOL_TABLE, "computer_left_click", (action, ComputerClickArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_type", (action, ComputerTypeArgs))
    first = dispatcher.spawn(dispatcher.submit("click", "computer_left_click", {"x": 1, "y": 1}))
    second = dispatcher.spawn(dispatcher.submit("type", "computer_type", {"text": "hello"}))
    try:
        await asyncio.sleep(0)
        approvals = [
            event.call_id
            for event in await drain(event_queue)
            if isinstance(event, ToolApprovalRequested)
        ]
        assert approvals == ["click"]
        dispatcher.deny("click")
        await asyncio.wait_for(first, 1)

        # Wait for the second approval without assuming task callback scheduling.
        async def next_approval() -> str:
            while True:
                event = await event_queue.get()
                if isinstance(event, ToolApprovalRequested):
                    return event.call_id

        assert await asyncio.wait_for(next_approval(), 1) == "type"
        dispatcher.approve("type")
        await asyncio.wait_for(second, 1)
        assert actions == ["type"]
        assert [item[0] for item in responses] == ["click", "type"]
        assert not responses[0][2].ok
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_shutdown_joins_running_desktop_thread_and_skips_queue(
    dispatcher: ToolDispatcher, monkeypatch: Any
) -> None:
    dispatcher.set_gating_mode(GatingMode.YOLO)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    actions: list[str] = []

    def click(args: Any, *, call_id: str) -> ToolResult:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        actions.append(call_id)
        return ToolResult(True, call_id)

    def type_text(args: Any, *, call_id: str) -> ToolResult:
        actions.append(call_id)
        return ToolResult(True, call_id)

    monkeypatch.setitem(_TOOL_TABLE, "computer_left_click", (click, ComputerClickArgs))
    monkeypatch.setitem(_TOOL_TABLE, "computer_type", (type_text, ComputerTypeArgs))
    first = dispatcher.spawn(dispatcher.submit("click", "computer_left_click", {"x": 1, "y": 1}))
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = dispatcher.spawn(dispatcher.submit("type", "computer_type", {"text": "skip"}))
        await asyncio.sleep(0)
        closing = asyncio.create_task(dispatcher.aclose())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done()
        assert not first.done()
        release.set()
        await asyncio.wait_for(closing, 2)
        assert first.cancelled()
        assert second.cancelled()
        assert actions == ["click"]
        assert dispatcher._tasks == set()
        assert dispatcher._desktop_tail is None
        assert dispatcher._desktop_waiters == {}
        assert dispatcher._approval_events == {}
    finally:
        release.set()
        if closing is not None:
            await closing
        await dispatcher.aclose()
