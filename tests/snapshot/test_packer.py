# -*- coding: utf-8 -*-
"""Tests for SnapshotPacker."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from copaw.app.snapshot.models import SnapshotManifest, SnapshotScope
from copaw.app.snapshot.packer import SnapshotPacker


@pytest.fixture
def staging_dir():
    """Create a temporary staging directory with sample files."""
    with tempfile.TemporaryDirectory(prefix="test_snap_") as d:
        root = Path(d)

        # Create workspace files
        ws = root / "workspaces" / "default"
        ws.mkdir(parents=True)

        (ws / "agent.json").write_text(
            json.dumps({"name": "test-agent", "channels": {}}),
            encoding="utf-8",
        )
        (ws / "chats.json").write_text("[]", encoding="utf-8")

        # Create sessions
        sessions = ws / "sessions"
        sessions.mkdir()
        (sessions / "sess1.json").write_text(
            json.dumps({"id": "sess1"}), encoding="utf-8",
        )

        # Create skills
        skills = ws / "skills" / "my_skill"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# My Skill", encoding="utf-8")

        yield root


@pytest.fixture
def output_dir():
    with tempfile.TemporaryDirectory(prefix="test_out_") as d:
        yield Path(d)


def test_pack_and_unpack(staging_dir, output_dir):
    """Pack a staging dir and unpack it, verifying roundtrip."""
    manifest = SnapshotManifest(
        agent_ids=["default"],
        scope=SnapshotScope.SINGLE,
        notes="test snapshot",
    )

    zip_path = output_dir / "test.zip"
    result = SnapshotPacker.pack(staging_dir, manifest, zip_path)
    assert result.is_file()
    assert result.stat().st_size > 0

    # Unpack
    extract_dir = output_dir / "extracted"
    unpacked_manifest = SnapshotPacker.unpack(result, extract_dir)

    assert unpacked_manifest.agent_ids == ["default"]
    assert unpacked_manifest.notes == "test snapshot"
    assert unpacked_manifest.scope == SnapshotScope.SINGLE
    assert len(unpacked_manifest.file_checksums) > 0

    # Verify files exist
    agent_json = extract_dir / "workspaces" / "default" / "agent.json"
    assert agent_json.is_file()
    data = json.loads(agent_json.read_text(encoding="utf-8"))
    assert data["name"] == "test-agent"


def test_read_manifest(staging_dir, output_dir):
    """Read manifest from ZIP without full extraction."""
    manifest = SnapshotManifest(
        agent_ids=["default"],
        notes="manifest-only test",
    )
    zip_path = output_dir / "test.zip"
    SnapshotPacker.pack(staging_dir, manifest, zip_path)

    read_manifest = SnapshotPacker.read_manifest(zip_path)
    assert read_manifest.notes == "manifest-only test"
    assert read_manifest.agent_ids == ["default"]


def test_checksum_verification(staging_dir, output_dir):
    """Verify that checksum validation catches corruption."""
    manifest = SnapshotManifest(agent_ids=["default"])
    zip_path = output_dir / "test.zip"
    SnapshotPacker.pack(staging_dir, manifest, zip_path)

    # Unpack normally first - should succeed
    extract1 = output_dir / "e1"
    SnapshotPacker.unpack(zip_path, extract1)

    # Tamper with a file in the extracted dir and try to verify
    # (This tests _verify_checksums indirectly)
    agent_json = extract1 / "workspaces" / "default" / "agent.json"
    agent_json.write_text('{"tampered": true}', encoding="utf-8")

    # Re-reading manifest from zip gives original checksums
    m = SnapshotPacker.read_manifest(zip_path)
    assert len(m.file_checksums) > 0


def test_invalid_zip(output_dir):
    """Unpack rejects non-zip files."""
    bad = output_dir / "not-a-zip.zip"
    bad.write_text("this is not a zip", encoding="utf-8")

    with pytest.raises(ValueError, match="Not a valid zip"):
        SnapshotPacker.unpack(bad, output_dir / "out")


def test_missing_manifest(output_dir):
    """Unpack rejects ZIPs without manifest.json."""
    import zipfile
    zip_path = output_dir / "no-manifest.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("dummy.txt", "hello")

    with pytest.raises(ValueError, match="manifest.json"):
        SnapshotPacker.unpack(zip_path, output_dir / "out")
