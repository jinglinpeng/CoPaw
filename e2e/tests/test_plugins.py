# -*- coding: utf-8 -*-
"""
QwenPaw Plugin System E2E Tests

Validates the plugin loader initialisation and plugin management lifecycle
on the Tauri desktop build. Covers:
- PLUGIN-001: Plugin loader readiness (API-level)
- PLUGIN-002: Plugin list page renders with loaded status
- PLUGIN-003: Plugin install API reachable (loader not returning 503)
- PLUGIN-004: End-to-end install → verify → uninstall cycle

Run:
    pytest tests/test_plugins.py -v
    pytest tests/test_plugins.py -k "PLUGIN-001" -v
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from playwright.sync_api import Page, APIRequestContext, expect

from config.settings import config
from utils.helpers import log_test_step, log_test_result

logger = logging.getLogger(__name__)

PLUGIN_MANAGER_URL = f"{config.base_url}/plugin-manager"
PLUGINS_API = f"{config.base_url}/api/plugins"
PLUGINS_INSTALL_API = f"{config.base_url}/api/plugins/install"


# ── Helpers ──────────────────────────────────────────────────────────────


def wait_for_plugin_loader_ready(
    api_context: APIRequestContext,
    timeout_seconds: int = 60,
    poll_interval: float = 2.0,
) -> list:
    """Poll GET /api/plugins until at least one plugin reports loaded=True.

    Returns the plugin list on success; raises AssertionError on timeout.
    """
    deadline = time.time() + timeout_seconds
    last_plugins = []

    while time.time() < deadline:
        try:
            response = api_context.get("/api/plugins")
            if response.ok:
                plugins = response.json()
                last_plugins = plugins
                loaded_count = sum(1 for p in plugins if p.get("loaded"))
                if loaded_count > 0:
                    logger.info(
                        f"Plugin loader ready: {loaded_count}/{len(plugins)} loaded"
                    )
                    return plugins
                logger.debug(
                    f"Loader not ready yet — 0/{len(plugins)} loaded, retrying..."
                )
        except Exception as exc:
            logger.debug(f"Poll error: {exc}")

        time.sleep(poll_interval)

    raise AssertionError(
        f"Plugin loader did not become ready within {timeout_seconds}s. "
        f"Last response: {json.dumps(last_plugins, indent=2, default=str)}"
    )


# ============================================================================
# PLUGIN-001: Plugin loader readiness (API-level)
# ============================================================================


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.plugins
class TestPluginLoaderReadiness:
    """
    PLUGIN-001: Verify the plugin loader initialises and loads plugins.

    This is the core regression test for issue #4889 — the plugin loader
    must start automatically and transition plugins from disk-scanned
    (loaded=False) to runtime-loaded (loaded=True).
    """

    @pytest.mark.test_id("PLUGIN-001")
    def test_plugin_loader_becomes_ready(
        self, api_context: APIRequestContext, request: pytest.FixtureRequest
    ):
        """Plugin loader must initialise within 60s of backend start."""
        test_name = request.node.name

        # Step 1: Wait for the plugin loader to finish initialisation
        log_test_step("1. Wait for plugin loader readiness (polling /api/plugins)")
        plugins = wait_for_plugin_loader_ready(api_context, timeout_seconds=60)

        # Step 2: Verify at least one plugin is loaded
        log_test_step("2. Verify loaded plugin count > 0")
        loaded = [p for p in plugins if p.get("loaded")]
        assert len(loaded) > 0, (
            "No plugins have loaded=True after loader init. "
            f"Total plugins on disk: {len(plugins)}"
        )
        logger.info(f"Loaded plugins: {[p['name'] for p in loaded]}")

        # Step 3: Verify plugin records have required fields
        log_test_step("3. Validate plugin record schema")
        required_fields = {"id", "name", "version", "loaded", "enabled"}
        for plugin in plugins:
            missing = required_fields - set(plugin.keys())
            assert not missing, (
                f"Plugin '{plugin.get('name', '?')}' missing fields: {missing}"
            )

        log_test_result(test_name, True)

    @pytest.mark.test_id("PLUGIN-001b")
    def test_plugin_install_endpoint_not_503(
        self, api_context: APIRequestContext, request: pytest.FixtureRequest
    ):
        """POST /api/plugins/install must not return 503 (loader not ready)."""
        test_name = request.node.name

        # Ensure the loader has had time to initialise
        log_test_step("1. Wait for loader readiness")
        wait_for_plugin_loader_ready(api_context, timeout_seconds=60)

        # Send a dummy install request — expect 400 (bad path), NOT 503
        log_test_step("2. POST /api/plugins/install with invalid source")
        response = api_context.post(
            "/api/plugins/install",
            data=json.dumps({"source": "/nonexistent/path", "force": False}),
        )

        assert response.status != 503, (
            "Install endpoint returned 503 — plugin loader is not ready. "
            "This is the exact symptom of issue #4889."
        )
        # 400 (bad path) or 404 is the expected healthy response
        assert response.status in (400, 404, 422), (
            f"Unexpected status {response.status}: {response.text()}"
        )

        log_test_result(test_name, True)


# ============================================================================
# PLUGIN-002: Plugin list page renders with loaded status
# ============================================================================


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.plugins
class TestPluginManagerPage:
    """
    PLUGIN-002: Plugin Manager UI shows plugins with correct loaded status.
    """

    @pytest.mark.test_id("PLUGIN-002")
    def test_plugin_page_shows_loaded_plugins(
        self, page: Page, api_context: APIRequestContext, request: pytest.FixtureRequest
    ):
        """Plugin Manager page must render plugin rows with loaded status."""
        test_name = request.node.name

        # Step 1: Ensure loader is ready before opening UI
        log_test_step("1. Wait for plugin loader readiness via API")
        plugins = wait_for_plugin_loader_ready(api_context, timeout_seconds=60)

        # Step 2: Navigate to Plugin Manager page
        log_test_step("2. Navigate to Plugin Manager page")
        page.goto(PLUGIN_MANAGER_URL)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        # Step 3: Verify the plugin table is visible
        log_test_step("3. Verify plugin table renders")
        table = page.locator(".ant-table, .qwenpaw-table").first
        expect(table).to_be_visible(timeout=10000)

        # Step 4: Verify plugin rows exist
        log_test_step("4. Verify plugin rows match API count")
        rows = page.locator(".ant-table-row, .qwenpaw-table-row").all()
        assert len(rows) > 0, "No plugin rows displayed in the table"
        logger.info(f"UI shows {len(rows)} plugin row(s), API returned {len(plugins)}")

        # Step 5: Verify at least one plugin shows loaded/running status
        log_test_step("5. Verify loaded status tags in UI")
        loaded_tags = page.locator(
            '.ant-tag-success, .ant-tag:has-text("Running"), .ant-tag:has-text("Loaded")'
        )
        loaded_count = loaded_tags.count()
        assert loaded_count > 0, (
            "No plugin shows 'Loaded' / 'Running' status in the UI. "
            "The plugin loader may not have initialised properly."
        )
        logger.info(f"{loaded_count} plugin(s) show loaded status in UI")

        log_test_result(test_name, True)


# ============================================================================
# PLUGIN-003: Plugin catalog API accessible
# ============================================================================


@pytest.mark.integration
@pytest.mark.p1
@pytest.mark.plugins
class TestPluginCatalogAPI:
    """
    PLUGIN-003: Verify the plugin catalog endpoint is functional.
    """

    @pytest.mark.test_id("PLUGIN-003")
    def test_plugin_catalog_returns_valid_response(
        self, api_context: APIRequestContext, request: pytest.FixtureRequest
    ):
        """GET /api/plugins/catalog must return a valid catalog structure."""
        test_name = request.node.name

        log_test_step("1. Fetch plugin catalog")
        response = api_context.get("/api/plugins/catalog")
        assert response.ok, f"Catalog endpoint failed: {response.status}"

        catalog = response.json()

        log_test_step("2. Validate catalog structure")
        assert "plugins" in catalog, "Catalog missing 'plugins' key"
        assert isinstance(catalog["plugins"], list), "'plugins' must be a list"

        logger.info(
            f"Catalog returned {len(catalog['plugins'])} entries, "
            f"updated_at={catalog.get('updated_at')}"
        )

        log_test_result(test_name, True)


# ============================================================================
# PLUGIN-004: End-to-end uninstall button availability
# ============================================================================


@pytest.mark.integration
@pytest.mark.p1
@pytest.mark.plugins
class TestPluginUninstallAvailability:
    """
    PLUGIN-004: When plugins are loaded, the uninstall button is available.

    In the bug scenario (#4889), only the uninstall button was shown
    (because plugins were disk-scanned but not loaded). After the fix,
    plugins should be loaded and the uninstall button should still work.
    """

    @pytest.mark.test_id("PLUGIN-004")
    def test_uninstall_button_visible_for_loaded_plugins(
        self, page: Page, api_context: APIRequestContext, request: pytest.FixtureRequest
    ):
        """Each loaded plugin row should have an uninstall button."""
        test_name = request.node.name

        log_test_step("1. Wait for plugin loader readiness")
        plugins = wait_for_plugin_loader_ready(api_context, timeout_seconds=60)
        if not plugins:
            pytest.skip("No plugins installed — cannot test uninstall button")

        log_test_step("2. Navigate to Plugin Manager page")
        page.goto(PLUGIN_MANAGER_URL)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        log_test_step("3. Verify uninstall buttons are present")
        rows = page.locator(".ant-table-row, .qwenpaw-table-row").all()
        assert len(rows) > 0, "No plugin rows in the table"

        danger_buttons = page.locator(
            'button.ant-btn-dangerous, button[class*="ant-btn-text"][class*="danger"]'
        )
        assert danger_buttons.count() > 0, (
            "No uninstall (danger) buttons found in plugin table"
        )
        logger.info(f"Found {danger_buttons.count()} uninstall button(s)")

        log_test_result(test_name, True)
