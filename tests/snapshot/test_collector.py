# -*- coding: utf-8 -*-
"""Tests for StateCollector."""
import json
import tempfile
from pathlib import Path

import pytest

from copaw.app.snapshot.collector import StateCollector


@pytest.fixture
def workspace_dir():
    """Create a mock workspace directory."""
    with tempfile.TemporaryDirectory(prefix="test_ws_") as d:
        root = Path(d)

        (root / "agent.json").write_text(
            json.dumps({"name": "test"}), encoding="utf-8",
        )
        (root / "chats.json").write_text("[]", encoding="utf-8")
        (root / "jobs.json").write_text("[]", encoding="utf-8")
        (root / "skill.json").write_text("{}", encoding="utf-8")
        (root / "AGENTS.md").write_text("# Agents", encoding="utf-8")
        (root / "custom_prompt.md").write_text("# Custom", encoding="utf-8")

        sessions = root / "sessions"
        sessions.mkdir()
        (sessions / "s1.json").write_text("{}", encoding="utf-8")
        (sessions / "s2.json").write_text("{}", encoding="utf-8")

        skills = root / "skills" / "skill1"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# Skill 1", encoding="utf-8")

        memory = root / "memory"
        memory.mkdir()
        (memory / "notes.md").write_text("# Notes", encoding="utf-8")

        yield root


@pytest.fixture
def staging_dir():
    with tempfile.TemporaryDirectory(prefix="test_stage_") as d:
        yield Path(d)


def test_collect_workspace(workspace_dir, staging_dir):
    """Collect all workspace files."""
    count = StateCollector.collect_workspace(
        workspace_dir, "default", staging_dir,
    )

    dest = staging_dir / "workspaces" / "default"
    assert dest.is_dir()
    assert (dest / "agent.json").is_file()
    assert (dest / "chats.json").is_file()
    assert (dest / "AGENTS.md").is_file()
    assert (dest / "custom_prompt.md").is_file()
    assert (dest / "sessions" / "s1.json").is_file()
    assert (dest / "skills" / "skill1" / "SKILL.md").is_file()
    assert (dest / "memory" / "notes.md").is_file()
    assert count > 0


def test_collect_workspace_exclude_sessions(workspace_dir, staging_dir):
    """Exclude sessions from collection."""
    StateCollector.collect_workspace(
        workspace_dir, "default", staging_dir,
        exclude_sessions=True,
    )

    dest = staging_dir / "workspaces" / "default"
    assert not (dest / "sessions").is_dir()
    assert (dest / "agent.json").is_file()


def test_collect_workspace_exclude_memory(workspace_dir, staging_dir):
    """Exclude memory from collection."""
    StateCollector.collect_workspace(
        workspace_dir, "default", staging_dir,
        exclude_memory=True,
    )

    dest = staging_dir / "workspaces" / "default"
    assert not (dest / "memory").is_dir()
    assert (dest / "agent.json").is_file()


def test_collect_global(staging_dir):
    """Collect global config files."""
    with tempfile.TemporaryDirectory(prefix="test_wd_") as wd:
        working_dir = Path(wd)
        (working_dir / "config.json").write_text("{}", encoding="utf-8")
        (working_dir / "settings.json").write_text("{}", encoding="utf-8")

        sp = working_dir / "skill_pool" / "shared_skill"
        sp.mkdir(parents=True)
        (sp / "SKILL.md").write_text("# Shared", encoding="utf-8")

        count = StateCollector.collect_global(working_dir, staging_dir)

        dest = staging_dir / "global"
        assert (dest / "config.json").is_file()
        assert (dest / "settings.json").is_file()
        assert (dest / "skill_pool" / "shared_skill" / "SKILL.md").is_file()
        assert count > 0


def test_estimate_size(workspace_dir):
    """Estimate snapshot size."""
    with tempfile.TemporaryDirectory(prefix="test_wd_") as wd:
        size = StateCollector.estimate_size(
            workspace_dirs=[workspace_dir],
            working_dir=Path(wd),
        )
        assert size > 0
