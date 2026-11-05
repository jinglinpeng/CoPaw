# -*- coding: utf-8 -*-
"""Do not publish a partial frontend inventory during background startup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers.frontend_plugin import list_frontend_plugins


@pytest.mark.asyncio
async def test_starting_loader_does_not_report_empty_success():
    loader = Mock()
    loader.get_all_loaded_plugins.return_value = {}
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                frontend_plugins_ready=asyncio.Event(),
                plugin_loader=loader,
            ),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await list_frontend_plugins(request)
    assert exc.value.status_code == 503
    loader.get_all_loaded_plugins.assert_not_called()


@pytest.mark.asyncio
async def test_ready_inventory_includes_frontend_plugin():
    ready = asyncio.Event()
    ready.set()
    manifest = SimpleNamespace(
        id="panel",
        name="Panel",
        version="1",
        description="",
        author="test",
        plugin_type="frontend",
        entry=SimpleNamespace(frontend="dist/index.js"),
    )
    loader = Mock()
    loader.get_all_loaded_plugins.return_value = {
        "panel": SimpleNamespace(manifest=manifest, enabled=True),
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                frontend_plugins_ready=ready,
                plugin_loader=loader,
            ),
        ),
    )
    result = await list_frontend_plugins(request)
    assert len(result) == 1
    assert result[0]["id"] == "panel"
    assert result[0]["frontend_entry"] == "dist/index.js"


@pytest.mark.asyncio
async def test_completed_empty_installation_is_valid():
    ready = asyncio.Event()
    ready.set()
    loader = Mock()
    loader.get_all_loaded_plugins.return_value = {}
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                frontend_plugins_ready=ready,
                plugin_loader=loader,
            ),
        ),
    )
    assert await list_frontend_plugins(request) == []
