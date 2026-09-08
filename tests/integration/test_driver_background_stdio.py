# -*- coding: utf-8 -*-
"""Background startup with a real stdio process and MCP transport."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import psutil
import pytest

from qwenpaw.drivers.adapters.agentscope_tool import build_driver_agent_tools
from qwenpaw.drivers.contracts import DriverCard
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.handlers.mcp import MCPDriverHandler
from qwenpaw.drivers.handlers.mcp_stateful_client import StdIOStatefulClient
from qwenpaw.drivers.manager import DriverManager


@pytest.mark.integration
@pytest.mark.p1
@pytest.mark.parametrize("outcome", ["ready", "cancel", "timeout"])
async def test_background_stdio_process_is_reaped(
    tmp_path, monkeypatch, outcome
):
    pid_file = tmp_path / "pid"
    release = tmp_path / "release"
    server = Path(__file__).parents[1] / "fixtures/mcp/stdio_echo_server.py"
    # Delay before the MCP handshake; exercise the real transport and its
    # subprocess cleanup rather than replacing the client with a fake.
    script = (
        "import os, pathlib, runpy, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.02)\n"
        "runpy.run_path(sys.argv[3], run_name='__main__')\n"
    )
    manager = DriverManager(
        tmp_path / "drivers",
        AsyncCredentialStore(tmp_path / "credentials.yaml"),
    )
    manager.register_handler_type("mcp", MCPDriverHandler)
    await manager.card_store.save(
        DriverCard(
            name="echo",
            protocol="mcp",
            endpoint={
                "transport": "stdio",
                "command": sys.executable,
                "args": [
                    "-c",
                    script,
                    str(pid_file),
                    str(release),
                    str(server),
                ],
            },
            config={"tools": ["echo"]},
        )
    )
    process = None
    if outcome == "timeout":
        original_connect = StdIOStatefulClient.connect

        async def short_connect(client):
            await original_connect(client, timeout=2)

        monkeypatch.setattr(StdIOStatefulClient, "connect", short_connect)
    manager.start_background()
    try:

        async def started():
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(started(), 5)
        process = psutil.Process(int(pid_file.read_text()))
        assert manager.get_driver_status("echo") == "connecting"
        if outcome == "ready":
            release.touch()
            tools, _ = await asyncio.wait_for(
                build_driver_agent_tools(manager, {}), 15
            )
            assert len(tools) == 1
            assert (
                tools[0].input_schema["properties"]["text"]["type"] == "string"
            )
            assert manager.get_driver_status("echo") == "active"
        elif outcome == "timeout":
            await asyncio.wait_for(manager.wait_for_startup(), 10)
            assert manager.get_driver_status("echo") == "error"
        await asyncio.wait_for(manager.shutdown_all(), 15)
        assert not process.is_running()
    finally:
        await manager.shutdown_all()
        # Keep the test's own child from surviving an assertion failure.
        if process is not None and process.is_running():
            process.kill()
            await asyncio.to_thread(process.wait, 5)
