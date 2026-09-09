# -*- coding: utf-8 -*-
"""First-model-request budgets, snapshots, and target-bound MCP calls."""

from __future__ import annotations

# pylint: disable=protected-access
import asyncio
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest
from agentscope.tool import Toolkit

from qwenpaw.drivers import tool_catalog
from qwenpaw.drivers.adapters.agentscope_tool import (
    build_driver_agent_tools,
    refresh_driver_agent_tools,
)
from qwenpaw.drivers.contracts import DriverCard
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.handlers.mcp import MCPDriverHandler
from qwenpaw.drivers.manager import DriverManager
from qwenpaw.drivers.policy_types import DriverPolicy


@dataclass
class _Server:
    connect_started: asyncio.Event = field(default_factory=asyncio.Event)
    connect: asyncio.Event = field(default_factory=asyncio.Event)
    listing_started: asyncio.Event = field(default_factory=asyncio.Event)
    listing: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list = field(default_factory=list)
    listed: int = 0
    schema_type: str = "string"
    description: str = "Echo input"
    connect_count: int = 0
    listing_error: Exception | None = None
    startup_error: Exception | None = None
    read_timeout: float = 300

    def __post_init__(self):
        self.listing.set()


@pytest.fixture(name="catalog_runtime")
async def _catalog_runtime(tmp_path):
    servers = {}

    class Client:
        def __init__(self, server):
            self.server = server
            self.read_timeout_seconds = server.read_timeout

        async def list_tools(self):
            self.server.listed += 1
            self.server.listing_started.set()
            await self.server.listing.wait()
            if self.server.listing_error is not None:
                raise self.server.listing_error
            return [
                SimpleNamespace(
                    name="echo",
                    description=self.server.description,
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "text": {"type": self.server.schema_type},
                        },
                    },
                ),
            ]

        async def call_tool(self, name, arguments):
            self.server.calls.append((name, arguments))
            return arguments

        async def close(self):
            pass

    class Handler(MCPDriverHandler):
        async def _setup(self):
            server = servers[self.name]
            server.connect_count += 1
            server.connect_started.set()
            await server.connect.wait()
            if server.startup_error is not None:
                raise server.startup_error
            self._client = Client(server)

    manager = DriverManager(
        tmp_path / "drivers",
        AsyncCredentialStore(tmp_path / "credentials.yaml"),
        optional_startup_grace=0.15,
    )
    manager.register_handler_type("mcp", Handler)

    async def add(name, *, ready=False):
        server = servers[name] = _Server()
        if ready:
            server.connect.set()
        card = DriverCard(
            name=name,
            protocol="mcp",
            endpoint={"command": name},
            policy=DriverPolicy(default_effect="allow"),
        )
        await manager.register_driver(card, wait=ready)
        await asyncio.wait_for(server.connect_started.wait(), 1)
        return card, server

    try:
        yield manager, servers, add
    finally:
        await manager.shutdown_all()
        assert not manager._tool_catalog._tasks


async def test_explicit_target_waits_without_waiting_for_other_servers(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, selected = await add("selected")
    _, other = await add("other")
    selected.listing.clear()
    statuses = []

    async def status(*args):
        statuses.append(args)

    request = asyncio.create_task(
        build_driver_agent_tools(
            manager,
            {},
            required_mcp_servers=("selected",),
            on_mcp_preparation=status,
        ),
    )
    try:
        await asyncio.sleep(0.2)
        assert not request.done()
        selected.connect.set()
        await asyncio.wait_for(selected.listing_started.wait(), 1)
        assert not request.done()
        selected.listing.set()
        tools, _ = await asyncio.wait_for(request, 1)
        assert [tool.name for tool in tools] == ["selected__echo"]
        assert not other.connect.is_set()
        assert statuses[-1] == ("selected", "selected", "ready")
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_explicit_callers_share_connection_and_cancellation(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, server = await add("shared")
    requests = [
        asyncio.create_task(
            build_driver_agent_tools(
                manager,
                {},
                required_mcp_servers=("shared",),
            ),
        )
        for _ in range(2)
    ]
    try:
        await asyncio.sleep(0.2)
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        assert not manager._initializations["shared"].task.cancelled()
        server.connect.set()
        tools, _ = await asyncio.wait_for(requests[1], 1)
        assert len(tools) == 1
        assert server.connect_count == 1
    finally:
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.parametrize(
    "error, expected",
    [
        (RuntimeError("private error"), "catalog_failed"),
        (TimeoutError(), "catalog_timeout"),
    ],
)
async def test_explicit_listing_failure_and_recovery(
    catalog_runtime,
    monkeypatch,
    error,
    expected,
):
    manager, _, add = catalog_runtime
    _, server = await add("selected")
    server.listing_error = error
    server.connect.set()
    states = []

    async def status(*args):
        states.append(args)

    tools, _ = await build_driver_agent_tools(
        manager,
        {},
        required_mcp_servers=("selected",),
        on_mcp_preparation=status,
    )
    assert tools == []
    assert states[-1][-1] == expected
    assert "private error" not in repr(states)
    server.listing_error = None
    monkeypatch.setattr(tool_catalog, "_REFRESH_INTERVAL_SECONDS", 0)
    tools, _ = await build_driver_agent_tools(
        manager,
        {},
        required_mcp_servers=("selected",),
        on_mcp_preparation=status,
    )
    assert len(tools) == 1
    assert states[-1][-1] == "ready"


async def test_explicit_target_disabled_during_wait(catalog_runtime):
    manager, _, add = catalog_runtime
    card, _ = await add("selected")
    states = []

    async def status(*args):
        states.append(args)

    request = asyncio.create_task(
        build_driver_agent_tools(
            manager,
            {},
            required_mcp_servers=("selected",),
            on_mcp_preparation=status,
        ),
    )
    try:
        await asyncio.sleep(0.2)
        await manager.register_driver(replace(card, enabled=False), wait=False)
        assert (await asyncio.wait_for(request, 1))[0] == []
        assert states[-1][-1] == "disabled"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_explicit_target_scope_and_empty_whitelist(catalog_runtime):
    manager, _, add = catalog_runtime
    card, _ = await add("selected", ready=True)
    await manager.register_driver(replace(card, config={"tools": []}))
    states = []

    async def status(*args):
        states.append(args)

    tools, _ = await build_driver_agent_tools(
        manager,
        {},
        required_mcp_servers=("selected", "missing"),
        on_mcp_preparation=status,
    )
    assert tools == []
    assert ("selected", "selected", "empty") in states
    assert ("missing", "missing", "unavailable") in states


async def test_explicit_discovery_does_not_await_all_initializations(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    selected_card, selected = await add("selected")
    other_card, _ = await add("other")
    await manager.delete_driver("selected")
    await manager.delete_driver("other")
    await manager.card_store.save(selected_card)
    await manager.card_store.save(other_card)
    manager.start_background()
    selected.connect.set()
    tools, _ = await asyncio.wait_for(
        build_driver_agent_tools(
            manager,
            {},
            required_mcp_servers=("selected",),
        ),
        1,
    )
    assert [tool.name for tool in tools] == ["selected__echo"]
    assert not manager._startup_task.done()


async def test_mcp_summary_does_not_read_credentials_or_connect(
    catalog_runtime,
    monkeypatch,
):
    from qwenpaw.app.mcp.config_service import MCPConfigService
    from qwenpaw.app.routers import mcp
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock

    manager, _, add = catalog_runtime
    _, server = await add("selected")
    service = MCPConfigService(
        SimpleNamespace(
            driver_manager=manager,
            workspace_dir=manager.cards_dir.parent,
        ),
    )
    service.load_card = AsyncMock(
        side_effect=AssertionError("no detail reads"),
    )
    service.build_info_from_card = AsyncMock(
        side_effect=AssertionError("no credentials"),
    )
    monkeypatch.setattr(
        mcp,
        "_agent_for_request",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(mcp, "_mcp_service", lambda _: service)
    app = FastAPI()
    app.include_router(mcp.router)
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get("/mcp?view=summary")
    assert response.status_code == 200
    assert response.json() == [
        {
            "key": "selected",
            "name": "selected",
            "description": "",
            "enabled": True,
            "runtime_status": "connecting",
        },
    ]
    assert server.listed == 0


@pytest.mark.parametrize(
    "error, expected",
    [
        (RuntimeError("private startup error"), "startup_failed"),
        (TimeoutError(), "startup_timeout"),
    ],
)
async def test_explicit_startup_failure_keeps_other_tools(
    catalog_runtime,
    error,
    expected,
):
    manager, _, add = catalog_runtime
    _, server = await add("selected")
    await add("healthy", ready=True)
    server.startup_error = error
    server.connect.set()
    states = []

    async def status(*args):
        states.append(args)

    tools, _ = await build_driver_agent_tools(
        manager,
        {},
        required_mcp_servers=("selected",),
        on_mcp_preparation=status,
    )
    assert [tool.name for tool in tools] == ["healthy__echo"]
    assert states[-1][-1] == expected
    assert "private startup error" not in repr(states)


async def test_explicit_catalog_wait_uses_the_shared_operation_timeout(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, server = await add("selected")
    server.read_timeout = 0.03
    server.listing.clear()
    server.connect.set()
    states = []

    async def status(*args):
        states.append(args)

    requests = [
        build_driver_agent_tools(
            manager,
            {},
            required_mcp_servers=("selected",),
            on_mcp_preparation=status,
        )
        for _ in range(2)
    ]
    results = await asyncio.wait_for(asyncio.gather(*requests), 1)
    assert all(not tools for tools, _ in results)
    assert server.listed == 1
    assert states[-1][-1] == "catalog_timeout"


async def test_explicit_wait_follows_replaced_connection(catalog_runtime):
    manager, servers, add = catalog_runtime
    card, old = await add("selected")
    task = asyncio.create_task(
        build_driver_agent_tools(
            manager,
            {},
            required_mcp_servers=("selected",),
        ),
    )
    try:
        await asyncio.sleep(0.2)
        replacement = servers["selected"] = _Server(schema_type="integer")
        replacement.connect.set()
        await manager.register_driver(
            replace(card, endpoint={"command": "new-command"}),
            wait=False,
        )
        tools, _ = await asyncio.wait_for(task, 1)
        assert len(tools) == 1
        assert tools[0].input_schema["properties"]["text"]["type"] == "integer"
        assert not old.connect.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_selected_wait_rechecks_metadata_after_reporting(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    card, _ = await add("selected", ready=True)
    states = []

    async def status(_name, _display, state):
        states.append(state)
        if state == "ready":
            await manager.register_driver(replace(card, config={"tools": []}))

    tools, _ = await build_driver_agent_tools(
        manager,
        {},
        required_mcp_servers=("selected",),
        on_mcp_preparation=status,
    )
    assert tools == []
    assert states[-1] == "empty"


async def test_selected_model_hint_refreshes_without_entering_history(
    catalog_runtime,
    monkeypatch,
):
    from qwenpaw.agents.react_agent import QwenPawAgent

    manager, _, add = catalog_runtime
    card, _ = await add("selected", ready=True)
    agent = object.__new__(QwenPawAgent)
    agent._driver_manager = manager
    agent._request_context = {}
    agent._required_mcp_servers = ("selected",)
    agent._mcp_preparation = {}
    agent._on_mcp_preparation = None
    agent.toolkit = Toolkit(tools=[])
    agent.state = SimpleNamespace(
        context=[],
        summary="",
        tool_context=SimpleNamespace(activated_groups=[]),
    )

    async def system_prompt():
        return "Test agent"

    monkeypatch.setattr(agent, "_get_system_prompt", system_prompt)
    first = await agent._prepare_model_input()
    assert len(first["tools"]) == 1
    assert '"status": "ready"' in first["messages"][1].get_text_content()
    await manager.register_driver(replace(card, enabled=False))
    second = await agent._prepare_model_input()
    assert not second["tools"]
    assert '"status": "disabled"' in second["messages"][1].get_text_content()
    assert (
        '"status": "disabled"' not in first["messages"][1].get_text_content()
    )
    assert agent.state.context == []
    agent._required_mcp_servers = ()
    third = await agent._prepare_model_input()
    assert len(third["messages"]) == 1


async def test_servers_and_later_requests_share_one_budget(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    for name in ("one", "two", "three"):
        await add(name)
    first, second = await asyncio.wait_for(
        asyncio.gather(
            manager.capture_tool_catalog({}),
            manager.capture_tool_catalog({}),
        ),
        0.35,
    )
    assert first == second == []
    deadline = manager._tool_catalog_deadline
    assert await asyncio.wait_for(manager.capture_tool_catalog({}), 0.05) == []
    assert manager._tool_catalog_deadline == deadline
    assert len(manager._initializations) == 3


async def test_slow_tool_listing_is_in_the_same_budget_and_late_tools_appear(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, server = await add("slow")
    server.listing.clear()
    server.connect.set()
    tools, _ = await asyncio.wait_for(
        build_driver_agent_tools(manager, {}),
        0.35,
    )
    assert tools == []
    assert server.listing_started.is_set()
    server.listing.set()
    await asyncio.gather(*manager._tool_catalog._tasks)
    tools, _ = await asyncio.wait_for(
        build_driver_agent_tools(manager, {}),
        0.05,
    )
    assert [tool.name for tool in tools] == ["slow__echo"]


async def test_valid_snapshot_returns_while_refresh_is_pending(
    catalog_runtime,
    monkeypatch,
):
    manager, _, add = catalog_runtime
    _, server = await add("cached", ready=True)
    tools, _ = await build_driver_agent_tools(manager, {"session_id": "one"})
    server.listing.clear()
    server.listing_started.clear()
    manager._handlers["cached"]._capability_cache = None
    monkeypatch.setattr(tool_catalog, "_REFRESH_INTERVAL_SECONDS", 0)
    cached, _ = await asyncio.wait_for(
        build_driver_agent_tools(manager, {"session_id": "two"}),
        0.05,
    )
    assert cached[0].input_schema == tools[0].input_schema
    await asyncio.wait_for(server.listing_started.wait(), 0.2)
    assert any(not task.done() for task in manager._tool_catalog._tasks)


async def test_cancelled_capture_does_not_cancel_connection_or_catalog(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, server = await add("pending")
    waiter = asyncio.create_task(build_driver_agent_tools(manager, {}))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not manager._initializations["pending"].task.cancelled()
    server.connect.set()
    await manager.wait_for_startup()
    assert len((await build_driver_agent_tools(manager, {}))[0]) == 1


async def test_zero_grace_keeps_explicit_full_wait_compatibility(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    manager._optional_startup_grace = 0
    _, server = await add("pending")
    waiter = asyncio.create_task(build_driver_agent_tools(manager, {}))
    await asyncio.sleep(0.02)
    assert not waiter.done()
    server.connect.set()
    assert len((await asyncio.wait_for(waiter, 1))[0]) == 1


async def test_bound_call_waits_only_target_and_cancellation_is_shared(
    catalog_runtime,
):
    manager, servers, add = catalog_runtime
    await add("target", ready=True)
    tools, _ = await build_driver_agent_tools(manager, {})
    await add("unrelated")
    servers["target"] = target = _Server()
    await manager.reload_driver("target", wait=False)
    await asyncio.wait_for(target.connect_started.wait(), 1)
    first = asyncio.create_task(tools[0](text="first"))
    second = asyncio.create_task(tools[0](text="second"))
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    target.connect.set()
    result = await asyncio.wait_for(second, 1)
    assert result.state.value == "success"
    assert target.calls == [("echo", {"text": "second"})]
    assert not servers["unrelated"].connect.is_set()
    assert target.connect_count == 1


@pytest.mark.parametrize(
    "change",
    ["schema", "description", "endpoint", "disabled", "whitelist", "policy"],
)
async def test_bound_call_rechecks_live_definition_and_authority(
    catalog_runtime,
    change,
):
    manager, _, add = catalog_runtime
    card, server = await add("service", ready=True)
    tools, _ = await build_driver_agent_tools(manager, {})
    if change == "schema":
        server.schema_type = "number"
        manager._handlers["service"]._capability_cache = None
    elif change == "description":
        server.description = "Different operation"
        manager._handlers["service"]._capability_cache = None
    elif change == "policy":
        await manager.sync_driver_policy(
            replace(card, policy=DriverPolicy(default_effect="deny")),
        )
    elif change == "whitelist":
        await manager.card_store.save(replace(card, config={"tools": []}))
        await manager.refresh_driver("service")
    else:
        changed = (
            replace(card, enabled=False)
            if change == "disabled"
            else replace(card, endpoint={"command": "different"})
        )
        await manager.register_driver(changed)
    result = await tools[0](text="test")
    assert result.state.value == "error"
    assert server.calls == []


async def test_refresh_preserves_whitelist_and_previous_snapshot(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    _, first = await add("first")
    _, other = await add("other")
    toolkit = Toolkit(tools=[])
    context = {"subagent_allowed_tools": ["first__echo"]}
    await refresh_driver_agent_tools(toolkit, manager, context)
    first_schema = await toolkit.get_tool_schemas()
    assert first_schema == []
    first.connect.set()
    other.connect.set()
    await manager.wait_for_startup()
    await asyncio.gather(*manager._tool_catalog._tasks)
    assert await toolkit.get_tool_schemas() == first_schema
    await refresh_driver_agent_tools(toolkit, manager, context)
    schemas = await toolkit.get_tool_schemas()
    assert [item["function"]["name"] for item in schemas] == ["first__echo"]
    assert first_schema == []


async def test_catalog_filters_transient_scope_and_isolated_workspace(
    catalog_runtime,
    tmp_path,
):
    manager, _, add = catalog_runtime
    card, _ = await add("scoped", ready=True)
    await manager.delete_driver("scoped")
    await manager.replace_transient_drivers("scope-a", [card])
    assert (await build_driver_agent_tools(manager, {}))[0] == []
    from qwenpaw.drivers.constants import DRIVER_SCOPE_CONTEXT_KEY

    scoped, _ = await build_driver_agent_tools(
        manager,
        {DRIVER_SCOPE_CONTEXT_KEY: "scope-a"},
    )
    assert len(scoped) == 1
    other = DriverManager(tmp_path / "other", manager.credential_store)
    try:
        assert (await build_driver_agent_tools(other, {}))[0] == []
    finally:
        await other.shutdown_all()


async def test_model_input_refreshes_before_agent_scope_reads_schemas(
    catalog_runtime,
    monkeypatch,
):
    from qwenpaw.agents.react_agent import QwenPawAgent

    manager, _, add = catalog_runtime
    _, server = await add("late")
    agent = object.__new__(QwenPawAgent)
    agent._driver_manager = manager
    agent._request_context = {}
    agent.toolkit = Toolkit(tools=[])
    agent.state = SimpleNamespace(
        context=[],
        summary="",
        tool_context=SimpleNamespace(activated_groups=[]),
    )

    async def system_prompt():
        return "Test agent"

    monkeypatch.setattr(agent, "_get_system_prompt", system_prompt)
    first = await agent._prepare_model_input()
    assert first["tools"] == []
    server.connect.set()
    await manager.wait_for_startup()
    await asyncio.gather(*manager._tool_catalog._tasks)
    second = await agent._prepare_model_input()
    assert len(second["tools"]) == 1
    assert first["tools"] == []


async def test_expired_snapshot_is_not_used_during_slow_refresh(
    catalog_runtime,
    monkeypatch,
):
    manager, _, add = catalog_runtime
    _, server = await add("cached", ready=True)
    assert len((await build_driver_agent_tools(manager, {}))[0]) == 1
    server.listing.clear()
    manager._handlers["cached"]._capability_cache = None
    monkeypatch.setattr(tool_catalog, "_REFRESH_INTERVAL_SECONDS", 0)
    for entry in manager._tool_catalog._entries.values():
        entry.published_at -= 31 * 60
    tools, _ = await asyncio.wait_for(
        build_driver_agent_tools(manager, {}),
        0.35,
    )
    assert tools == []


async def test_reconfiguration_during_capture_discards_old_binding(
    catalog_runtime,
):
    manager, servers, add = catalog_runtime
    _, old = await add("service")
    old.listing.clear()
    old.connect.set()
    await manager.wait_for_startup()
    await asyncio.wait_for(old.listing_started.wait(), 1)
    capture = asyncio.create_task(build_driver_agent_tools(manager, {}))
    await asyncio.sleep(0)
    servers["service"] = new = _Server(schema_type="number")
    new.connect.set()
    await manager.reload_driver("service")
    old.listing.set()
    tools, _ = await capture
    assert tools == []
    current, _ = await build_driver_agent_tools(manager, {})
    assert current[0].input_schema["properties"]["text"]["type"] == "number"


async def test_metadata_change_drops_snapshot_and_stale_refresh(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    card, server = await add("service", ready=True)
    tools, _ = await build_driver_agent_tools(manager, {})
    assert len(tools) == 1
    server.listing.clear()
    manager._handlers["service"]._capability_cache = None
    await manager.card_store.save(replace(card, config={"tools": []}))
    await manager.refresh_driver("service")
    assert (await build_driver_agent_tools(manager, {}))[0] == []
    server.listing.set()
    await asyncio.gather(*manager._tool_catalog._tasks)
    assert (await build_driver_agent_tools(manager, {}))[0] == []


async def test_display_name_change_invalidates_cached_tool_names(
    catalog_runtime,
):
    manager, _, add = catalog_runtime
    card, _ = await add("service", ready=True)
    tools, _ = await build_driver_agent_tools(manager, {})
    await manager.card_store.save(
        replace(card, config={"display_name": "Renamed"}),
    )
    await manager.refresh_driver("service")
    refreshed, _ = await build_driver_agent_tools(manager, {})
    assert refreshed[0].name != tools[0].name
    assert (await tools[0](text="stale")).state.value == "error"


async def test_context_dependent_catalogs_do_not_share_descriptions(
    catalog_runtime,
    monkeypatch,
):
    manager, _, add = catalog_runtime
    await add("service", ready=True)
    tools = await manager.capture_tool_catalog({})
    handler = manager._handlers["service"]
    monkeypatch.setattr(handler, "capabilities_are_context_free", False)

    async def contextual_tools(request_context):
        return [replace(tools[0], description=request_context["subject"])]

    monkeypatch.setattr(handler, "list_capabilities", contextual_tools)
    first = await manager.capture_tool_catalog({"subject": "first"})
    second = await manager.capture_tool_catalog({"subject": "second"})
    cached = await manager.capture_tool_catalog({"subject": "first"})
    assert first[0].description == cached[0].description == "first"
    assert second[0].description == "second"
