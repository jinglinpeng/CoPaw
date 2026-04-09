# -*- coding: utf-8 -*-
"""Tests for SnapshotRestorer."""
import json
import tempfile
from pathlib import Path

import pytest

from copaw.app.snapshot.models import SnapshotManifest, SnapshotScope
from copaw.app.snapshot.packer import SnapshotPacker
from copaw.app.snapshot.restorer import SnapshotRestorer


@pytest.fixture
def env():
    """Set up test environment with working_dir, workspace, and snapshot."""
    with tempfile.TemporaryDirectory(prefix="test_restore_") as d:
        root = Path(d)
        working_dir = root / "working"
        working_dir.mkdir()

        # Create original workspace
        ws_dir = working_dir / "workspaces" / "default"
        ws_dir.mkdir(parents=True)
        (ws_dir / "agent.json").write_text(
            json.dumps({"version": "original"}), encoding="utf-8",
        )
        (ws_dir / "chats.json").write_text("[]", encoding="utf-8")

        # Create a snapshot with different content
        staging = root / "snap_staging"
        snap_ws = staging / "workspaces" / "default"
        snap_ws.mkdir(parents=True)
        (snap_ws / "agent.json").write_text(
            json.dumps({"version": "snapshot"}), encoding="utf-8",
        )
        (snap_ws / "chats.json").write_text(
            '[{"id": "from_snap"}]', encoding="utf-8",
        )

        manifest = SnapshotManifest(
            agent_ids=["default"],
            scope=SnapshotScope.SINGLE,
        )
        snap_dir = working_dir / "snapshots"
        snap_dir.mkdir()
        snap_path = snap_dir / "test-snap.zip"
        SnapshotPacker.pack(staging, manifest, snap_path)

        yield {
            "working_dir": working_dir,
            "ws_dir": ws_dir,
            "snap_path": snap_path,
        }


def test_prepare(env):
    """Prepare phase extracts snapshot to staging."""
    restorer = SnapshotRestorer(env["working_dir"])
    state = restorer.prepare(
        env["snap_path"], "default", env["ws_dir"],
    )

    assert state.phase.value == "prepared"
    assert state.last_completed_step == "staging_extracted"
    assert restorer.has_pending_restore("default")

    # Staging dir should exist
    staging_dir = Path(state.staging_dir)
    assert staging_dir.is_dir()


def test_apply(env):
    """Apply phase swaps directories."""
    restorer = SnapshotRestorer(env["working_dir"])
    state = restorer.prepare(
        env["snap_path"], "default", env["ws_dir"],
    )

    state = restorer.apply(state)
    assert state.phase.value == "applied"
    assert state.last_completed_step == "staging_renamed_to_workspace"

    # Workspace should now have snapshot content
    data = json.loads(
        (env["ws_dir"] / "agent.json").read_text(encoding="utf-8"),
    )
    assert data["version"] == "snapshot"

    # Backup should exist
    backup_dir = Path(state.backup_dir)
    assert backup_dir.is_dir()
    backup_data = json.loads(
        (backup_dir / "agent.json").read_text(encoding="utf-8"),
    )
    assert backup_data["version"] == "original"


def test_rollback(env):
    """Rollback restores from backup."""
    restorer = SnapshotRestorer(env["working_dir"])
    state = restorer.prepare(
        env["snap_path"], "default", env["ws_dir"],
    )
    state = restorer.apply(state)

    # Rollback
    ok = restorer.rollback(state)
    assert ok

    # Original content restored
    data = json.loads(
        (env["ws_dir"] / "agent.json").read_text(encoding="utf-8"),
    )
    assert data["version"] == "original"


def test_crash_recovery_staging_extracted(env):
    """Recovery from crash after staging_extracted."""
    restorer = SnapshotRestorer(env["working_dir"])
    state = restorer.prepare(
        env["snap_path"], "default", env["ws_dir"],
    )

    # Simulate crash at staging_extracted
    action = restorer.recover("default")
    assert action == "cleaned_staging"
    assert not restorer.has_pending_restore("default")


def test_crash_recovery_workspace_renamed(env):
    """Recovery from crash after workspace_renamed_to_backup."""
    restorer = SnapshotRestorer(env["working_dir"])
    state = restorer.prepare(
        env["snap_path"], "default", env["ws_dir"],
    )

    # Manually simulate the rename step
    import shutil
    backup_dir = Path(state.backup_dir)
    staging_dir = Path(state.staging_dir)
    ws_dir = env["ws_dir"]

    ws_dir.rename(backup_dir)
    state.last_completed_step = "workspace_renamed_to_backup"
    restorer._save_state(state)

    # Recover
    action = restorer.recover("default")
    assert action == "completed_from_staging"
    assert ws_dir.is_dir()
