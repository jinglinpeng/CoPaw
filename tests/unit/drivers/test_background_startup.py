# -*- coding: utf-8 -*-
"""Readiness and ownership checks for background Driver initialization."""
from __future__ import annotations

# pylint: disable=protected-access
import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.driver_config_service import DriverConfigService
from qwenpaw.app.mcp.config_service import MCPConfigService
from qwenpaw.drivers.adapters.agentscope_tool import build_driver_agent_tools
from qwenpaw.drivers.capabilities import (
    CapabilityExposure,
    DriverCapability,
    DriverInvocation,
    DriverInvocationResult,
    format_capability_id,
)
from qwenpaw.drivers.contracts import DriverCard
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.errors import DriverNotReadyError, DriverRuntimeError
from qwenpaw.drivers.handler import DriverHandler
from qwenpaw.drivers.manager import DriverManager
from qwenpaw.drivers.policy_types import DriverPolicy, PolicyRule


@dataclass
class _Connection:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    close_release: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0
    closed: int = 0
    fail: bool = False
    ignore_cancel: bool = False
    hold_cleanup: bool = False


@pytest.fixture(name="runtime")
def manager_runtime(tmp_path: Path):
    connections: dict[str, _Connection] = {}

    class Handler(DriverHandler):
        async def _setup(self):
            connection = connections[self.card.endpoint["version"]]
            connection.count += 1
            connection.started.set()
            try:
                await connection.release.wait()
            except asyncio.CancelledError:
                if not connection.ignore_cancel:
                    raise
                await connection.release.wait()
            if connection.fail:
                raise TimeoutError("connection timeout")

        async def _teardown(self):
            connection = connections[self.card.endpoint["version"]]
            connection.closed += 1
            connection.closing.set()
            if connection.hold_cleanup:
                await connection.close_release.wait()

        async def list_capabilities(self, request_context=None):
            del request_context
            return [
                DriverCapability(
                    capability_id=format_capability_id(
                        "mcp",
                        self.name,
                        "tool",
                        "invoke",
                        "echo",
                    ),
                    driver_name=self.name,
                    protocol="mcp",
                    kind="tool",
                    action="invoke",
                    name="echo",
                    description=self.card.config.get(
                        "description",
                        "Echo input",
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                    enabled=self.card.config.get("tools") != [],
                    exposure=CapabilityExposure(
                        as_tool=True,
                        tool_name=f"{self.name}__echo",
                    ),
                ),
            ]

        async def invoke_capability(self, invocation):
            return DriverInvocationResult(ok=True, value=invocation.payload)

    manager = DriverManager(
        tmp_path / "drivers",
        AsyncCredentialStore(tmp_path / "credentials.yaml"),
    )
    manager.register_handler_type("mcp", Handler)

    def card(name="service", version="old", **kwargs):
        connections.setdefault(version, _Connection())
        return DriverCard(
            name=name,
            protocol="mcp",
            endpoint={
                "transport": "stdio",
                "command": "test",
                "version": version,
            },
            **kwargs,
        )

    return manager, connections, card


async def _entered(connection):
    await asyncio.wait_for(connection.started.wait(), 2)


async def test_background_start_shares_connections_and_preserves_first_tools(
    runtime,
):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    manager.start_background()
    manager.start_background()
    await _entered(connections["old"])
    assert manager.get_driver_status("service") == "connecting"
    with pytest.raises(DriverNotReadyError):
        await manager.list_driver_capabilities("service")
    result = await manager.invoke_capability(
        DriverInvocation(
            capability_id=format_capability_id(
                "mcp",
                "service",
                "tool",
                "invoke",
                "echo",
            ),
            payload={},
        ),
    )
    assert not result.ok and result.error_type == "driver_not_ready"
    first = asyncio.create_task(build_driver_agent_tools(manager, {}))
    second = asyncio.create_task(build_driver_agent_tools(manager, {}))
    try:
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not manager._startup_task.cancelled()
        connections["old"].release.set()
        tools, hints = await asyncio.wait_for(second, 2)
        assert [tool.name for tool in tools] == ["service__echo"]
        assert tools[0].description == "Echo input"
        assert tools[0].input_schema["properties"] == {
            "text": {"type": "string"},
        }
        assert hints
        assert connections["old"].count == 1
        assert manager.get_driver_status("service") == "active"
    finally:
        await manager.shutdown_all()


async def test_tool_wait_includes_discovery_before_tasks_exist(
    runtime,
    monkeypatch,
):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    release_scan = asyncio.Event()
    entered_scan = asyncio.Event()
    original = manager.card_store.list_paths

    async def list_paths():
        entered_scan.set()
        await release_scan.wait()
        return await original()

    monkeypatch.setattr(manager.card_store, "list_paths", list_paths)
    manager.start_background()
    await asyncio.wait_for(entered_scan.wait(), 2)
    waiter = asyncio.create_task(build_driver_agent_tools(manager, {}))
    try:
        await asyncio.sleep(0)
        assert not manager._initializations and not waiter.done()
        assert manager.get_driver_status("service") == "connecting"
        release_scan.set()
        await _entered(connections["old"])
        assert not waiter.done()
        connections["old"].release.set()
        tools, _ = await asyncio.wait_for(waiter, 2)
        assert len(tools) == 1
    finally:
        release_scan.set()
        await manager.shutdown_all()


async def test_partial_failure_settles_and_keeps_healthy_tools(runtime):
    manager, connections, card = runtime
    for name in ("good", "bad"):
        await manager.card_store.save(card(name, name))
        connections[name].release.set()
    connections["bad"].fail = True
    try:
        await manager.start()
        tools, _ = await build_driver_agent_tools(manager, {})
        assert [tool.name for tool in tools] == ["good__echo"]
        assert manager.get_driver_status("bad") == "error"
        assert connections["bad"].closed == 1
    finally:
        await manager.shutdown_all()


@pytest.mark.parametrize(
    "mutation",
    ["register", "reload", "refresh", "delete", "disable"],
)
async def test_late_old_connection_cannot_publish_after_mutation(
    runtime,
    mutation,
):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    connections["old"].ignore_cancel = True
    manager.start_background()
    await _entered(connections["old"])
    replacement = card(version="new", enabled=mutation != "disable")
    connections["new"].release.set()
    if mutation in ("reload", "refresh"):
        await manager.card_store.save(replacement)
        operation = getattr(manager, f"{mutation}_driver")("service")
    elif mutation == "delete":
        operation = manager.delete_driver("service")
    else:
        operation = manager.register_driver(replacement)
    update = asyncio.create_task(operation)
    try:
        # Wait until the old attempt has actually been replaced or unpublished.
        for _ in range(200):
            current = manager._initializations.get("service")
            if current is None or current.card.endpoint["version"] == "new":
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("mutation did not replace the pending attempt")
        connections["old"].release.set()
        await asyncio.wait_for(update, 2)
        await asyncio.wait_for(manager.wait_for_startup(), 2)
        assert connections["old"].closed == 1
        if mutation in ("delete", "disable"):
            assert not manager._handlers
        else:
            assert (
                manager._handlers["service"].card.endpoint["version"] == "new"
            )
        if mutation == "delete":
            assert await manager.card_store.stored_path("service") is None
    finally:
        connections["old"].release.set()
        await manager.shutdown_all()


async def test_pending_metadata_and_policy_changes_do_not_reconnect(runtime):
    manager, connections, card = runtime
    original = card()
    await manager.card_store.save(original)
    manager.start_background()
    await _entered(connections["old"])
    latest = replace(original, config={"tools": [], "description": "updated"})
    await manager.card_store.save(latest)
    await manager.refresh_driver("service")
    latest.policy = DriverPolicy(
        rules=[PolicyRule(subject="user:alice", effect="allow")],
    )
    await manager.sync_driver_policy(latest)
    connections["old"].release.set()
    try:
        await manager.wait_for_startup()
        handler = manager._handlers["service"]
        assert handler.card.config == latest.config
        assert handler.card.policy == latest.policy
        assert connections["old"].count == 1
        tools, _ = await build_driver_agent_tools(manager, {})
        assert not tools
    finally:
        await manager.shutdown_all()


async def test_failed_reload_keeps_old_active_handler(runtime):
    manager, connections, card = runtime
    original = card()
    connections["old"].release.set()
    await manager.register_driver(original)
    await manager.card_store.save(card(version="new"))
    connections["new"].fail = True
    connections["new"].release.set()
    try:
        with pytest.raises(TimeoutError):
            await manager.reload_driver("service")
        assert manager._handlers["service"].card.endpoint["version"] == "old"
        assert manager.get_driver_status("service") == "active"
        assert connections["new"].closed == 1
        assert connections["old"].closed == 0
    finally:
        await manager.shutdown_all()


async def test_disable_unpublishes_active_tools_before_slow_cleanup(runtime):
    manager, connections, card = runtime
    original = card()
    connections["old"].release.set()
    await manager.register_driver(original)
    connections["old"].hold_cleanup = True
    try:
        await manager.register_driver(
            replace(original, enabled=False),
            wait=False,
        )
        await asyncio.wait_for(connections["old"].closing.wait(), 2)
        assert await manager.list_capabilities() == []
        assert (await manager.list_drivers())[0].status == "disabled"
        connections["old"].close_release.set()
        await manager.wait_for_startup()
        assert connections["old"].closed == 1
    finally:
        connections["old"].close_release.set()
        await manager.shutdown_all()


async def test_shutdown_owns_cancelled_waiter_and_connection_cleanup(runtime):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    connections["old"].hold_cleanup = True
    manager.start_background()
    await _entered(connections["old"])
    shutdown = asyncio.create_task(manager.shutdown_all())
    await asyncio.wait_for(connections["old"].closing.wait(), 2)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    with pytest.raises(DriverRuntimeError):
        manager.start_background()
    with pytest.raises(DriverRuntimeError):
        await manager.register_driver(card(version="new"))
    connections["old"].close_release.set()
    await asyncio.wait_for(manager.shutdown_all(), 2)
    assert not manager._handlers
    assert not manager._initializations
    assert not manager._initialization_tasks
    assert not manager._cleanup_tasks
    assert connections["old"].closed == 1


async def test_late_completion_after_shutdown_is_cleaned_without_publication(
    runtime,
):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    connections["old"].ignore_cancel = True
    manager.start_background()
    await _entered(connections["old"])
    shutdown = asyncio.create_task(manager.shutdown_all())
    await asyncio.sleep(0)
    connections["old"].release.set()
    await asyncio.wait_for(shutdown, 2)
    assert not manager._handlers
    assert connections["old"].closed == 1


async def test_pending_persistent_name_rejects_transient_scope(runtime):
    manager, connections, card = runtime
    await manager.card_store.save(card())
    manager.start_background()
    await _entered(connections["old"])
    transient = card(version="temporary")
    connections["temporary"].release.set()
    try:
        with pytest.raises(ValueError, match="already exist"):
            await manager.replace_transient_drivers("scope", [transient])
        assert connections["temporary"].closed == 1
        connections["old"].release.set()
        await manager.wait_for_startup()
        assert manager._handlers["service"].card.endpoint["version"] == "old"
    finally:
        await manager.shutdown_all()


async def test_api_save_queues_owned_connection_and_returns_pending_status(
    runtime,
    tmp_path,
):
    manager, connections, card = runtime
    workspace = SimpleNamespace(workspace_dir=tmp_path, driver_manager=manager)
    config = DriverConfigService(workspace)
    mcp = MCPConfigService(workspace)
    try:
        await asyncio.wait_for(config.save_card(card()), 2)
        await _entered(connections["old"])
        clients = await mcp.list_clients()
        assert clients[0].runtime_status == "connecting"
        with pytest.raises(HTTPException) as error:
            await mcp.list_tools("service")
        assert error.value.status_code == 503
        connections["old"].release.set()
        await manager.wait_for_startup()
        assert (await mcp.list_clients())[0].runtime_status == "active"
        assert [tool.name for tool in await mcp.list_tools("service")] == [
            "echo",
        ]
        stored = await manager.card_store.load_path(
            await manager.card_store.stored_path("service"),
        )
        assert "runtime_status" not in stored.config
    finally:
        await manager.shutdown_all()


async def test_cancelled_config_request_does_not_cancel_owned_connection(
    runtime,
):
    manager, connections, card = runtime
    request = asyncio.create_task(manager.register_driver(card()))
    await _entered(connections["old"])
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    try:
        assert connections["old"].closed == 0
        connections["old"].release.set()
        await manager.wait_for_startup()
        assert manager.get_driver_status("service") == "active"
    finally:
        await manager.shutdown_all()


async def test_same_name_in_another_workspace_is_independent(
    runtime,
    tmp_path,
):
    manager, connections, card = runtime
    other = DriverManager(
        tmp_path / "other/drivers",
        AsyncCredentialStore(tmp_path / "other/credentials.yaml"),
    )
    other.register_handler_type("mcp", manager._handler_types["mcp"])
    await manager.card_store.save(card())
    await other.card_store.save(card(version="other"))
    manager.start_background()
    other.start_background()
    await _entered(connections["old"])
    await _entered(connections["other"])
    try:
        await manager.shutdown_all()
        assert connections["other"].closed == 0
        assert other.get_driver_status("service") == "connecting"
        connections["other"].release.set()
        tools, _ = await build_driver_agent_tools(other, {})
        assert [tool.name for tool in tools] == ["service__echo"]
    finally:
        await manager.shutdown_all()
        await other.shutdown_all()


async def test_shutdown_immediately_after_queuing_leaves_no_pending_task(
    runtime,
):
    manager, _, card = runtime
    await manager.register_driver(card(), wait=False)
    await manager.shutdown_all()
    assert not manager._initializations
    assert not manager._initialization_tasks
    assert not manager._cleanup_tasks


async def test_shutdown_also_reaps_inflight_transient_connection(runtime):
    manager, connections, card = runtime
    request = asyncio.create_task(
        manager.replace_transient_drivers("session", [card()]),
    )
    await _entered(connections["old"])
    await asyncio.wait_for(manager.shutdown_all(), 2)
    with pytest.raises(asyncio.CancelledError):
        await request
    assert connections["old"].closed == 1
    assert not manager._handlers
    assert not manager._initialization_tasks
    assert not manager._cleanup_tasks


async def test_discovery_failure_is_reported_and_does_not_become_empty_tools(
    runtime,
    monkeypatch,
):
    manager, _, _ = runtime

    async def failed_scan():
        raise OSError("card storage unavailable")

    monkeypatch.setattr(manager.card_store, "list_paths", failed_scan)
    manager.start_background()
    try:
        with pytest.raises(OSError, match="storage unavailable"):
            await build_driver_agent_tools(manager, {})
        assert manager.get_driver_status("service") == "error"
    finally:
        await manager.shutdown_all()


async def test_watcher_applies_metadata_to_pending_connection_without_restart(
    runtime,
):
    from qwenpaw.app.driver_config_watcher import DriverConfigWatcher

    manager, connections, card = runtime
    original = card()
    await manager.card_store.save(original)
    manager.start_background()
    await _entered(connections["old"])
    watcher = DriverConfigWatcher(manager, manager.cards_dir)
    await watcher.start()
    try:
        await manager.card_store.save(
            replace(original, config={"description": "from watcher"}),
        )
        await watcher._check_once()
        await watcher.stop()
        assert connections["old"].closed == 0
        connections["old"].release.set()
        tools, _ = await build_driver_agent_tools(manager, {})
        assert tools[0].description == "from watcher"
        assert connections["old"].count == 1
    finally:
        await watcher.stop()
        await manager.shutdown_all()


async def test_workspace_factory_allows_later_services_before_mcp_ready(
    runtime,
    tmp_path,
    monkeypatch,
):
    from qwenpaw.app.workspace.service_factories import create_driver_service
    from qwenpaw.app.workspace.service_manager import (
        ServiceDescriptor,
        ServiceManager,
    )

    manager, connections, card = runtime
    handler_type = manager._handler_types["mcp"]
    await manager.card_store.save(card())
    workspace = SimpleNamespace(
        agent_id="test",
        workspace_dir=tmp_path,
        _config=SimpleNamespace(),
    )
    services = ServiceManager(workspace)
    order = []

    async def migrate(ws, runtime_manager):
        assert ws is workspace
        assert services.services["driver_manager"] is runtime_manager
        order.append("migration")

    async def later_service(ws, _, publish):
        assert ws is workspace
        order.append("channel/cron")
        publish(SimpleNamespace())

    monkeypatch.setattr(
        "qwenpaw.drivers.handlers.MCPDriverHandler",
        handler_type,
    )
    monkeypatch.setattr(
        "qwenpaw.drivers.adapters.mcp_legacy_config."
        "migrate_legacy_mcp_if_needed",
        migrate,
    )
    services.register(
        ServiceDescriptor(
            name="driver_manager",
            post_init=create_driver_service,
            stop_method="shutdown_all",
            priority=20,
        ),
    )
    services.register(
        ServiceDescriptor(name="later", post_init=later_service, priority=30),
    )
    try:
        await asyncio.wait_for(services.start_all(), 2)
        await _entered(connections["old"])
        assert order == ["migration", "channel/cron"]
        driver = services.services["driver_manager"]
        assert driver.get_driver_status("service") == "connecting"
        connections["old"].release.set()
        await driver.wait_for_startup()
        assert driver.get_driver_status("service") == "active"
    finally:
        await services.stop_all()
