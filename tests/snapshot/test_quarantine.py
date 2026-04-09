# -*- coding: utf-8 -*-
"""Tests for ImportQuarantine."""
import json
import tempfile
from pathlib import Path

import pytest

from copaw.app.snapshot.quarantine import ImportQuarantine


@pytest.fixture
def imported_workspace():
    """Create a workspace dir simulating an imported package."""
    with tempfile.TemporaryDirectory(prefix="test_q_") as d:
        root = Path(d)

        # agent.json with channels and MCP
        config = {
            "name": "imported-agent",
            "channels": {
                "dingtalk": {"enabled": True, "client_id": "xxx"},
                "discord": {"enabled": True, "bot_token": "yyy"},
            },
            "mcp": {
                "server1": {"url": "http://example.com"},
                "server2": {"url": "http://other.com"},
            },
        }
        (root / "agent.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8",
        )

        # skill.json
        skills = {
            "skills": [
                {"name": "skill1", "enabled": True},
                {"name": "skill2", "enabled": True},
            ],
        }
        (root / "skill.json").write_text(
            json.dumps(skills), encoding="utf-8",
        )

        # jobs.json
        jobs = [
            {"id": "job1", "cron": "* * * * *", "enabled": True},
            {"id": "job2", "cron": "0 * * * *", "enabled": True},
        ]
        (root / "jobs.json").write_text(
            json.dumps(jobs), encoding="utf-8",
        )

        yield root


def test_quarantine_disables_skills(imported_workspace):
    """All skills should be disabled."""
    summary = ImportQuarantine.quarantine(imported_workspace)
    assert summary["skills"] == 2

    data = json.loads(
        (imported_workspace / "skill.json").read_text(encoding="utf-8"),
    )
    for skill in data["skills"]:
        assert skill["enabled"] is False


def test_quarantine_disables_jobs(imported_workspace):
    """All cron jobs should be disabled."""
    summary = ImportQuarantine.quarantine(imported_workspace)
    assert summary["jobs"] == 2

    data = json.loads(
        (imported_workspace / "jobs.json").read_text(encoding="utf-8"),
    )
    for job in data:
        assert job["enabled"] is False


def test_quarantine_disables_channels(imported_workspace):
    """All channels should be marked as imported_disabled."""
    summary = ImportQuarantine.quarantine(imported_workspace)
    assert summary["channels"] == 2

    data = json.loads(
        (imported_workspace / "agent.json").read_text(encoding="utf-8"),
    )
    for ch_name, ch_conf in data["channels"].items():
        assert ch_conf.get("_imported_disabled") is True


def test_quarantine_disables_mcp(imported_workspace):
    """All MCP configs should be marked as imported_disabled."""
    summary = ImportQuarantine.quarantine(imported_workspace)
    assert summary["mcp"] == 2

    data = json.loads(
        (imported_workspace / "agent.json").read_text(encoding="utf-8"),
    )
    for mcp_name, mcp_conf in data["mcp"].items():
        assert mcp_conf.get("_imported_disabled") is True
