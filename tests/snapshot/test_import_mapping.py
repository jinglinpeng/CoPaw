# -*- coding: utf-8 -*-
"""Tests for multi-agent snapshot import mapping (P0-2)."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from copaw.app.multi_agent_manager import MultiAgentManager
from copaw.app.snapshot.api import router as snapshot_router
from copaw.app.snapshot.manager import SnapshotManager
from copaw.app.snapshot.models import (
    AgentStatus,
    ImportResult,
    SnapshotManifest,
    SnapshotScope,
)
from copaw.app.snapshot.packer import SnapshotPacker


class StubMAM(MultiAgentManager):
    """Avoid full workspace startup during import tests."""

    async def get_agent(self, agent_id, *, operation_lock_held=False):  # type: ignore[override]
        return None  # type: ignore[return-value]

    async def stop_agent(self, agent_id, *, operation_lock_held=False):  # type: ignore[override]
        return False


class StubSnapshotApiManager:
    """Capture /import API arguments without touching real IO flow."""

    def __init__(self):
        self.last_call = None

    async def import_snapshot(
        self,
        file_path: Path,
        *,
        target_agent_id=None,
        force=False,
        agent_mappings=None,
    ) -> ImportResult:
        self.last_call = {
            "file_path": file_path,
            "target_agent_id": target_agent_id,
            "force": force,
            "agent_mappings": agent_mappings,
        }
        return ImportResult(
            agent_id=target_agent_id or "imported",
            status=AgentStatus.READY,
        )

    async def save_import_to_local(self, file_path: Path) -> None:
        return None


@pytest.fixture
def snapshot_api_client():
    app = FastAPI()
    app.include_router(snapshot_router, prefix="/api")
    manager = StubSnapshotApiManager()
    app.state.snapshot_manager = manager
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, manager


@pytest.mark.asyncio
async def test_api_import_accepts_form_agent_mappings(snapshot_api_client):
    api_client, stub_manager = snapshot_api_client
    async with api_client:
        resp = await api_client.post(
            "/api/snapshots/import",
            files={"file": ("snapshot.zip", b"dummy-bytes", "application/zip")},
            data={"agent_mappings": json.dumps({"alpha": "imp-a"})},
        )

    assert resp.status_code == 200
    assert stub_manager.last_call is not None
    assert stub_manager.last_call["agent_mappings"] == {"alpha": "imp-a"}


@pytest.mark.asyncio
async def test_api_import_rejects_invalid_mapping_json(snapshot_api_client):
    api_client, stub_manager = snapshot_api_client
    async with api_client:
        resp = await api_client.post(
            "/api/snapshots/import",
            files={"file": ("snapshot.zip", b"dummy-bytes", "application/zip")},
            data={"agent_mappings": "{invalid"},
        )

    assert resp.status_code == 400
    assert "agent_mappings must be valid JSON object" in resp.json()["detail"]
    assert stub_manager.last_call is None


@pytest.mark.asyncio
async def test_api_import_ignores_legacy_query_mapping(snapshot_api_client):
    api_client, stub_manager = snapshot_api_client
    async with api_client:
        resp = await api_client.post(
            "/api/snapshots/import",
            params={"agent_id_map": json.dumps({"alpha": "query-target"})},
            files={"file": ("snapshot.zip", b"dummy-bytes", "application/zip")},
        )

    assert resp.status_code == 200
    assert stub_manager.last_call is not None
    assert stub_manager.last_call["agent_mappings"] is None


def test_plan_import_legacy_first_agent_only():
    m = SnapshotManifest(agent_ids=["a", "b"], scope=SnapshotScope.SELECTED)
    plan = SnapshotManager._plan_import(m, None, None)
    assert plan == [("a", "a")]

    plan2 = SnapshotManager._plan_import(m, "z", None)
    assert plan2 == [("a", "z")]


def test_plan_import_multi_default_same_name():
    m = SnapshotManifest(agent_ids=["a", "b"], scope=SnapshotScope.SELECTED)
    plan = SnapshotManager._plan_import(m, None, {})
    assert plan == [("a", "a"), ("b", "b")]

    plan2 = SnapshotManager._plan_import(m, None, {"a": "x"})
    assert plan2 == [("a", "x"), ("b", "b")]


def test_plan_import_duplicate_target_raises():
    m = SnapshotManifest(agent_ids=["a", "b"], scope=SnapshotScope.SELECTED)
    with pytest.raises(ValueError, match="Duplicate target"):
        SnapshotManager._plan_import(m, None, {"a": "x", "b": "x"})


def test_plan_import_unknown_source_raises():
    m = SnapshotManifest(agent_ids=["a"], scope=SnapshotScope.SINGLE)
    with pytest.raises(ValueError, match="unknown source"):
        SnapshotManager._plan_import(m, None, {"nosuch": "x"})


def test_plan_import_agent_id_and_mappings_mutually_exclusive():
    m = SnapshotManifest(agent_ids=["a"], scope=SnapshotScope.SINGLE)
    with pytest.raises(ValueError, match="Cannot specify both"):
        SnapshotManager._plan_import(m, "z", {})


@pytest.mark.asyncio
async def test_import_multi_maps_and_locks_targets(monkeypatch, tmp_path):
    monkeypatch.setattr("copaw.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("copaw.config.config.WORKING_DIR", tmp_path)

    secret = tmp_path / ".secret"
    secret.mkdir()
    mam = StubMAM()
    sm = SnapshotManager(tmp_path, secret, mam)

    staging = tmp_path / "mk"
    for aid in ("alpha", "beta"):
        ws = staging / "workspaces" / aid
        ws.mkdir(parents=True)
        (ws / "agent.json").write_text(
            json.dumps({"name": aid, "channels": {}}),
            encoding="utf-8",
        )

    manifest = SnapshotManifest(
        agent_ids=["alpha", "beta"],
        scope=SnapshotScope.SELECTED,
    )
    zip_path = tmp_path / "two.zip"
    SnapshotPacker.pack(staging, manifest, zip_path)

    result = await sm.import_snapshot(
        zip_path,
        force=False,
        agent_mappings={"alpha": "imp-a", "beta": "imp-b"},
    )

    assert len(result.agent_outcomes) == 2
    assert {o.target_agent_id for o in result.agent_outcomes} == {"imp-a", "imp-b"}
    assert result.agent_id == "imp-a"

    from copaw.config.utils import load_config

    cfg = load_config()
    assert "imp-a" in cfg.agents.profiles
    assert "imp-b" in cfg.agents.profiles
    assert (tmp_path / "workspaces" / "imp-a" / "agent.json").is_file()
    assert (tmp_path / "workspaces" / "imp-b" / "agent.json").is_file()
    assert result.file_summary.get("agents_imported") == "2"
    assert "alpha->imp-a" in (result.file_summary.get("import_mapping") or "")


@pytest.mark.asyncio
async def test_import_legacy_still_only_first_when_multiple_in_zip(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("copaw.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("copaw.config.config.WORKING_DIR", tmp_path)

    secret = tmp_path / ".secret"
    secret.mkdir()
    mam = StubMAM()
    sm = SnapshotManager(tmp_path, secret, mam)

    staging = tmp_path / "mk2"
    for aid in ("first", "second"):
        ws = staging / "workspaces" / aid
        ws.mkdir(parents=True)
        (ws / "agent.json").write_text(
            json.dumps({"name": aid, "channels": {}}),
            encoding="utf-8",
        )

    manifest = SnapshotManifest(
        agent_ids=["first", "second"],
        scope=SnapshotScope.SELECTED,
    )
    zip_path = tmp_path / "legacy.zip"
    SnapshotPacker.pack(staging, manifest, zip_path)

    result = await sm.import_snapshot(zip_path, force=False, agent_mappings=None)

    assert len(result.agent_outcomes) == 1
    assert result.agent_outcomes[0].source_agent_id == "first"
    from copaw.config.utils import load_config

    cfg = load_config()
    assert "first" in cfg.agents.profiles
    assert "second" not in cfg.agents.profiles
    assert not (tmp_path / "workspaces" / "second").exists()
    assert result.file_summary.get("agents_imported") == "1"
    assert "import_mapping" not in result.file_summary


@pytest.mark.asyncio
async def test_import_multi_preflight_aborts_before_any_agent(monkeypatch, tmp_path):
    """Second target already exists → fail before importing the first."""
    monkeypatch.setattr("copaw.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("copaw.config.config.WORKING_DIR", tmp_path)

    secret = tmp_path / ".secret"
    secret.mkdir()
    mam = StubMAM()
    sm = SnapshotManager(tmp_path, secret, mam)

    staging = tmp_path / "mk3"
    for aid in ("alpha", "beta"):
        ws = staging / "workspaces" / aid
        ws.mkdir(parents=True)
        (ws / "agent.json").write_text(
            json.dumps({"name": aid, "channels": {}}),
            encoding="utf-8",
        )

    manifest = SnapshotManifest(
        agent_ids=["alpha", "beta"],
        scope=SnapshotScope.SELECTED,
    )
    zip_path = tmp_path / "conflict.zip"
    SnapshotPacker.pack(staging, manifest, zip_path)

    from copaw.config.config import AgentProfileRef
    from copaw.config.utils import load_config, save_config

    cfg = load_config()
    blocked = tmp_path / "workspaces" / "imp-b"
    blocked.mkdir(parents=True)
    cfg.agents.profiles["imp-b"] = AgentProfileRef(
        id="imp-b",
        workspace_dir=str(blocked),
        enabled=True,
    )
    save_config(cfg)

    with pytest.raises(ValueError, match="target agent 'imp-b' already exists"):
        await sm.import_snapshot(
            zip_path,
            force=False,
            agent_mappings={"alpha": "imp-a", "beta": "imp-b"},
        )

    cfg2 = load_config()
    assert "imp-a" not in cfg2.agents.profiles
    assert not (tmp_path / "workspaces" / "imp-a").exists()
