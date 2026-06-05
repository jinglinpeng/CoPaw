# -*- coding: utf-8 -*-
"""
QwenPaw Plugin Manager page object.

Wraps all interactions on the Plugin Manager page for E2E testing.
"""
from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any
from playwright.sync_api import Page, Locator, expect, TimeoutError

from pages.base_page import BasePage
from config.settings import config

logger = logging.getLogger(__name__)


class PluginsPage(BasePage):
    """
    Plugin Manager page object.

    Covers:
    - Plugin list display and status verification
    - Plugin loader readiness check
    - Plugin install / uninstall operations
    - Plugin loaded/unloaded status assertions
    """

    PAGE_TITLE = "QwenPaw Console"
    PAGE_URL = f"{config.base_url}/plugin-manager"

    # ========== Selectors ==========

    # Ant Design table that holds the plugin list
    PLUGIN_TABLE_SELECTOR = ".ant-table, .qwenpaw-table"
    PLUGIN_ROW_SELECTOR = ".ant-table-row, .qwenpaw-table-row"

    # Status tags rendered by usePluginColumns
    STATUS_LOADED_TAG = '.ant-tag-success, .ant-tag:has-text("Running"), .ant-tag:has-text("Loaded")'
    STATUS_UNLOADED_TAG = '.ant-tag-default:has-text("Not loaded"), .ant-tag-default:has-text("Unloaded")'

    # Action buttons
    UNINSTALL_BTN_SELECTOR = 'button[class*="ant-btn-text"][class*="ant-btn-dangerous"]'

    # Install dialog
    INSTALL_BTN_SELECTOR = (
        'button:has-text("Install"), '
        'button:has-text("安装插件")'
    )

    # Error / loading states
    LOADING_INDICATOR = ".ant-spin, .ant-skeleton"
    ERROR_ALERT = ".ant-alert-error"

    # ========== Navigation ==========

    def open(self) -> "PluginsPage":
        """Navigate to the Plugin Manager page."""
        logger.info("Opening Plugin Manager page")
        self.goto()
        self.wait_for_page_loaded()
        return self

    def wait_for_page_loaded(self, timeout: Optional[int] = None) -> "PluginsPage":
        """Wait until the plugin table or empty state is visible."""
        timeout = timeout or self.timeout
        self.page.wait_for_load_state("domcontentloaded")
        # Wait for either the table or the page body to settle
        self.page.wait_for_timeout(2000)
        return self

    # ========== Plugin list methods ==========

    def get_plugin_rows(self) -> List[Locator]:
        """Return all plugin table rows."""
        rows = self.page.locator(self.PLUGIN_ROW_SELECTOR).all()
        logger.info(f"Found {len(rows)} plugin row(s)")
        return rows

    def get_plugin_names(self) -> List[str]:
        """Extract plugin names from the table."""
        rows = self.get_plugin_rows()
        names = []
        for row in rows:
            try:
                name_el = row.locator("td").first.locator("strong, b").first
                names.append(name_el.inner_text(timeout=3000))
            except Exception:
                pass
        return names

    def has_loaded_plugins(self) -> bool:
        """Check if any plugin shows 'Loaded' / 'Running' status."""
        try:
            return self.page.locator(self.STATUS_LOADED_TAG).count() > 0
        except Exception:
            return False

    def count_loaded_plugins(self) -> int:
        """Return the number of plugins with loaded status."""
        return self.page.locator(self.STATUS_LOADED_TAG).count()

    def count_unloaded_plugins(self) -> int:
        """Return the number of plugins with unloaded status."""
        return self.page.locator(self.STATUS_UNLOADED_TAG).count()

    def is_plugin_table_visible(self) -> bool:
        """Check if the plugin table is rendered."""
        try:
            return self.page.locator(self.PLUGIN_TABLE_SELECTOR).first.is_visible(timeout=5000)
        except Exception:
            return False

    # ========== API-level helpers ==========

    def fetch_plugins_via_api(self) -> List[Dict[str, Any]]:
        """Call GET /api/plugins directly and return the JSON list."""
        response = self.page.request.get(f"{config.base_url}/api/plugins")
        assert response.ok, f"GET /api/plugins failed: {response.status}"
        return response.json()

    def fetch_plugin_loader_status(self) -> Dict[str, Any]:
        """Call GET /api/plugins and check if plugins are loaded (not just disk-scanned)."""
        plugins = self.fetch_plugins_via_api()
        loaded_count = sum(1 for p in plugins if p.get("loaded"))
        total_count = len(plugins)
        return {
            "total": total_count,
            "loaded": loaded_count,
            "all_loaded": loaded_count == total_count and total_count > 0,
            "loader_ready": any(p.get("loaded") for p in plugins),
            "plugins": plugins,
        }
