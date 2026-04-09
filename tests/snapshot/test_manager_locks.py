# -*- coding: utf-8 -*-
"""Tests for SnapshotManager / MultiAgentManager operation lock integration."""
import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from copaw.app.multi_agent_manager import (
    MultiAgentManager,
    snapshot_agent_operation_key,
)
from copaw.app.snapshot.manager import SnapshotManager
from copaw.app.snapshot.models import CreateSnapshotRequest, SnapshotScope


class RecordingMultiAgentManager(MultiAgentManager):
    """Records keys passed into hold_operation_locks (before sorting)."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_lock_calls: list[list[str]] = []

    @asynccontextmanager
    async def hold_operation_locks(self, keys):  # type: ignore[override]
        self.hold_lock_calls.append(list(keys))
        async with super().hold_operation_locks(keys):
            yield


@pytest.mark.asyncio
async def test_create_uses_manager_hold_operation_locks_with_agent_keys() -> None:
    with tempfile.TemporaryDirectory(prefix="copaw_wd_") as wd, tempfile.TemporaryDirectory(
        prefix="copaw_sec_",
    ) as sd:
        working = Path(wd)
        secret = Path(sd)
        mam = RecordingMultiAgentManager()
        sm = SnapshotManager(working, secret, mam)
        req = CreateSnapshotRequest(
            scope=SnapshotScope.SINGLE,
            agent_ids=["zebra", "apple"],
        )
        await sm.create(req)

        assert len(mam.hold_lock_calls) == 1
        passed = mam.hold_lock_calls[0]
        assert set(passed) == {
            snapshot_agent_operation_key("zebra"),
            snapshot_agent_operation_key("apple"),
        }


@pytest.mark.asyncio
async def test_hold_operation_locks_concurrent_inverse_order_no_deadlock() -> None:
    """Two tasks lock the same two agents in opposite key orders; must finish."""
    mam = MultiAgentManager()
    k_a = snapshot_agent_operation_key("a")
    k_b = snapshot_agent_operation_key("b")
    results: list[int] = []

    async def task1() -> None:
        async with mam.hold_operation_locks([k_b, k_a]):
            await asyncio.sleep(0.02)
            results.append(1)

    async def task2() -> None:
        await asyncio.sleep(0.005)
        async with mam.hold_operation_locks([k_a, k_b]):
            await asyncio.sleep(0.02)
            results.append(2)

    await asyncio.wait_for(asyncio.gather(task1(), task2()), timeout=3.0)
    assert sorted(results) == [1, 2]
