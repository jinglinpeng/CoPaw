# -*- coding: utf-8 -*-
"""Explicit MCP waits stream status and preserve runtime task ownership."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.runtime import runtime as runtime_module
from qwenpaw.runtime.hooks import HookResult
from qwenpaw.runtime.message_convert import selected_mcp_servers
from qwenpaw.runtime.phases import Phase
from qwenpaw.schemas import AgentRequest


def request(ids=None):
    return AgentRequest(
        session_id="chat-a",
        input=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
                "metadata": {"mcp_server_ids": ids} if ids is not None else {},
            },
        ],
    )


def test_selection_is_current_request_metadata_only():
    assert selected_mcp_servers(request(["echo", "echo"]).input) == ("echo",)
    assert not selected_mcp_servers(request().input)
    assert not selected_mcp_servers(
        [
            SimpleNamespace(
                role="assistant",
                metadata={"mcp_server_ids": ["echo"]},
            ),
        ],
    )
    assert not selected_mcp_servers(
        [
            SimpleNamespace(
                role="user",
                metadata={},
                content="/mcp:echo hello",
            ),
        ],
    )


@pytest.mark.parametrize("ids", ["echo", [None], [""], ["  "], ["echo\n"]])
def test_invalid_selection_is_rejected(ids):
    with pytest.raises(ValueError, match="mcp_server_ids"):
        selected_mcp_servers(request(ids).input)


@pytest.fixture(name="selected_runtime")
def selected_runtime_fixture(monkeypatch):
    gate = asyncio.Event()
    building = asyncio.Event()
    closed = asyncio.Event()
    events = []
    phases = []
    contexts = []
    owner = ContextVar("mcp_test_owner")
    token = None

    async def hook(phase, ctx):
        nonlocal token
        phases.append(phase)
        if phase == Phase.PRE_DISPATCH:
            token = owner.set(ctx.session_id)
        if phase == Phase.FINALLY:
            owner.reset(token)
            closed.set()
        return HookResult()

    async def build(_builder, ctx):
        contexts.append(ctx)
        building.set()
        assert owner.get() == "chat-a"
        await gate.wait()
        if ctx.on_mcp_preparation:
            await ctx.on_mcp_preparation("echo", "Echo", "ready")
        return SimpleNamespace(close=AsyncMock())

    async def execute(executor, _msgs):
        from agentscope.message import Msg, TextBlock

        # pylint: disable=protected-access
        async for event in executor._envelope.from_msg(
            Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(text="hello")],
            ),
        ):
            yield event

    # The fake model is the only replaced execution boundary; the runtime and
    # envelope use their real lifecycle, sequencing, and cancellation code.
    monkeypatch.setattr(runtime_module.AgentBuilder, "build", build)
    monkeypatch.setattr(runtime_module.AgentExecutor, "run", execute)
    monkeypatch.setattr(runtime_module, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    workspace = SimpleNamespace(
        agent_id="agent-a",
        plugins=SimpleNamespace(
            hook_registry=SimpleNamespace(run=hook),
            slash_command_registry=SimpleNamespace(
                dispatch=AsyncMock(return_value=None),
            ),
            modes=[],
        ),
    )
    runtime = runtime_module.Runtime(workspace=workspace, app_services=None)

    async def collect(req):
        async for event in runtime.run(req):
            events.append(event.model_dump(mode="json"))

    return SimpleNamespace(
        runtime=runtime,
        collect=collect,
        events=events,
        gate=gate,
        building=building,
        closed=closed,
        phases=phases,
        contexts=contexts,
    )


async def test_selected_wait_streams_before_build_with_heartbeat(
    selected_runtime,
):
    case = selected_runtime
    task = asyncio.create_task(case.collect(request(["echo"])))
    try:
        await asyncio.wait_for(case.building.wait(), 1)
        await asyncio.sleep(0.06)
        assert not task.done()
        assert case.events[0]["type"] == "mcp_preparation"
        assert case.events[0]["data"] == {
            "agent_id": "agent-a",
            "session_id": "chat-a",
            "server_id": "echo",
            "name": "echo",
            "status": "preparing",
        }
        assert any(event.get("type") == "heartbeat" for event in case.events)
        case.gate.set()
        await asyncio.wait_for(task, 1)
        states = [
            event["data"]["status"]
            for event in case.events
            if event.get("type") == "mcp_preparation"
        ]
        assert states == ["preparing", "ready"]
        assert case.events[-1]["status"] == "completed"
        sequences = [event["sequence_number"] for event in case.events]
        assert sequences == sorted(set(sequences))
        assert case.closed.is_set()
        case.contexts[0].agent.close.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_selected_cancel_during_build_closes_in_the_owning_task(
    selected_runtime,
):
    case = selected_runtime
    task = asyncio.create_task(case.collect(request(["echo"])))
    await asyncio.wait_for(case.building.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert case.closed.is_set()
    assert Phase.ON_ERROR in case.phases
    assert case.events[-1]["status"] == "completed"
    assert not any(
        task.get_name() == "mcp-selected-turn" for task in asyncio.all_tasks()
    )


async def test_selected_disconnect_at_yield_closes_lifecycle(selected_runtime):
    case = selected_runtime
    case.gate.set()
    stream = case.runtime.run(request(["echo"]))
    while True:
        event = await anext(stream)
        if getattr(event, "object", None) == "response":
            break
    await asyncio.wait_for(stream.aclose(), 1)
    assert case.closed.is_set()
    case.contexts[0].agent.close.assert_awaited_once()


async def test_plain_turn_has_no_selection_or_preparation_events(
    selected_runtime,
):
    case = selected_runtime
    case.gate.set()
    await case.collect(request())
    assert case.contexts[0].mcp_server_ids == ()
    assert case.contexts[0].on_mcp_preparation is None
    assert not any(
        event.get("type") == "mcp_preparation" for event in case.events
    )
