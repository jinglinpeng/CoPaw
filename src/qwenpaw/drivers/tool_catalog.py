# -*- coding: utf-8 -*-
"""Process-local tool snapshots and their owned background refreshes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter

from .capabilities import DriverCapability
from .contracts import DriverCard
from .handler import DriverHandler

logger = logging.getLogger(__name__)
_CACHE_TTL_SECONDS = 30 * 60
_REFRESH_INTERVAL_SECONDS = 10.0
_MAX_IDLE_ENTRIES = 32


@dataclass
class _CatalogEntry:
    card: DriverCard
    tools: list[DriverCapability] | None = None
    published_at: float = 0.0
    attempted_at: float = 0.0
    task: asyncio.Task[None] | None = None
    error: str = ""


class DriverToolCatalog:
    """Cache descriptions, never connections or authorization decisions."""

    def __init__(self) -> None:
        self._entries: OrderedDict[
            tuple[DriverHandler, str],
            _CatalogEntry,
        ] = OrderedDict()
        self._tasks: dict[asyncio.Task[None], DriverHandler] = {}
        self._closed = False

    @staticmethod
    def _context(handler: DriverHandler, request_context: dict | None) -> dict:
        if handler.capabilities_are_context_free:
            return {}
        return deepcopy(request_context or {})

    def _entry(
        self,
        handler: DriverHandler,
        request_context: dict | None,
    ) -> _CatalogEntry:
        context = self._context(handler, request_context)
        key = (handler, json.dumps(context, sort_keys=True, default=str))
        entry = self._entries.get(key)
        if entry is None or entry.card != handler.card:
            if entry is not None and entry.task is not None:
                entry.task.cancel()
            entry = _CatalogEntry(card=deepcopy(handler.card))
            self._entries[key] = entry
        self._entries.move_to_end(key)
        now = perf_counter()
        if (
            not self._closed
            and (entry.task is None or entry.task.done())
            and (
                entry.task is None
                or now - entry.attempted_at >= _REFRESH_INTERVAL_SECONDS
            )
        ):
            entry.attempted_at = now
            entry.error = ""
            entry.task = asyncio.create_task(
                self._refresh(entry, handler, context),
                name=f"driver-tool-catalog:{handler.name}",
            )
            self._tasks[entry.task] = handler
            entry.task.add_done_callback(self._tasks.pop)
        # Keep all in-flight work owned; only idle descriptions are evicted.
        idle = [
            key
            for key, value in self._entries.items()
            if value.task is None or value.task.done()
        ]
        for old_key in idle[:-_MAX_IDLE_ENTRIES]:
            self._entries.pop(old_key)
        return entry

    @staticmethod
    async def _refresh(
        entry: _CatalogEntry,
        handler: DriverHandler,
        context: dict,
    ) -> None:
        try:
            tools = await handler.list_capabilities(request_context=context)
            if entry.card == handler.card:
                entry.tools = deepcopy(tools)
                entry.published_at = perf_counter()
        except Exception as exc:
            entry.error = (
                "catalog_timeout"
                if isinstance(exc, TimeoutError)
                else "catalog_failed"
            )
            logger.warning(
                "Failed to refresh tool catalog for Driver '%s'",
                handler.name,
                exc_info=True,
            )

    def prime(self, handler: DriverHandler) -> None:
        if handler.capabilities_are_context_free:
            self._entry(handler, None)

    @staticmethod
    def _available(entry: _CatalogEntry) -> bool:
        return (
            entry.tools is not None
            and perf_counter() - entry.published_at <= _CACHE_TTL_SECONDS
        )

    def has_snapshot(self, handler: DriverHandler, context: dict) -> bool:
        return self._available(self._entry(handler, context))

    async def prepare(self, handler: DriverHandler, context: dict) -> str:
        """Await the shared catalog task and report visible tool readiness."""
        entry = self._entry(handler, context)
        if not self._available(entry) and entry.task is not None:
            await asyncio.shield(entry.task)
        if entry.card != handler.card:
            return "changed"
        if not self._available(entry):
            return entry.error or "catalog_failed"
        allowed = context.get("subagent_allowed_tools")
        return (
            "ready"
            if any(
                tool.enabled
                and tool.exposure.as_tool
                and (
                    not isinstance(allowed, list)
                    or tool.exposure.tool_name in allowed
                )
                for tool in entry.tools or ()
            )
            else "empty"
        )

    async def capture(
        self,
        handlers: list[DriverHandler],
        context: dict,
        deadline: float | None,
    ) -> list[DriverCapability]:
        entries = [
            (handler, self._entry(handler, context)) for handler in handlers
        ]
        pending = {
            entry.task
            for _, entry in entries
            if entry.task is not None and not self._available(entry)
        }
        if pending:
            # Zero budget still permits local/cache queries to complete.
            # asyncio.wait never cancels a shared refresh when a caller leaves.
            await asyncio.wait(
                pending,
                timeout=None
                if deadline is None
                else max(0, deadline - perf_counter()),
            )
        return sorted(
            [
                deepcopy(tool)
                for handler, entry in entries
                if self._available(entry) and entry.card == handler.card
                for tool in entry.tools or ()
            ],
            key=lambda tool: tool.capability_id,
        )

    def forget(self, handler: DriverHandler) -> None:
        for key in list(self._entries):
            if key[0] is handler:
                self._entries.pop(key)
        for task, owner in self._tasks.copy().items():
            if owner is handler and not task.cancelling():
                task.cancel()

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()
