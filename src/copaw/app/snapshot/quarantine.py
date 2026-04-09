# -*- coding: utf-8 -*-
"""ImportQuarantine: Disable active components in imported workspaces."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ImportQuarantine:
    """Applies post-import quiescent state to untrusted workspace files.

    All "active outbound" components are disabled by default:
    - Skills: enabled -> false
    - Cron jobs: enabled -> false
    - Channels: _imported_disabled -> true
    - MCP clients: _imported_disabled -> true
    """

    @staticmethod
    def quarantine(workspace_dir: Path) -> dict:
        """Apply quarantine to all config files. Returns summary."""
        summary = {}

        summary["skills"] = ImportQuarantine._disable_skills(workspace_dir)
        summary["jobs"] = ImportQuarantine._disable_jobs(workspace_dir)
        summary["channels"] = ImportQuarantine._disable_channels(
            workspace_dir,
        )
        summary["mcp"] = ImportQuarantine._disable_mcp(workspace_dir)

        logger.info("Quarantine applied to %s: %s", workspace_dir, summary)
        return summary

    @staticmethod
    def _disable_skills(workspace_dir: Path) -> int:
        """Disable all skills in skill.json. Returns count."""
        skill_json = workspace_dir / "skill.json"
        if not skill_json.is_file():
            return 0

        try:
            with open(skill_json, "r", encoding="utf-8") as f:
                data = json.load(f)

            count = 0
            if isinstance(data, dict):
                skills = data.get("skills", data)
                if isinstance(skills, list):
                    for skill in skills:
                        if isinstance(skill, dict):
                            skill["enabled"] = False
                            count += 1
                elif isinstance(skills, dict):
                    for name, skill in skills.items():
                        if isinstance(skill, dict):
                            skill["enabled"] = False
                            count += 1

            with open(skill_json, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            return count
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to quarantine skills: %s", e)
            return 0

    @staticmethod
    def _disable_jobs(workspace_dir: Path) -> int:
        """Disable all cron jobs in jobs.json. Returns count."""
        jobs_json = workspace_dir / "jobs.json"
        if not jobs_json.is_file():
            return 0

        try:
            with open(jobs_json, "r", encoding="utf-8") as f:
                data = json.load(f)

            count = 0
            jobs = data if isinstance(data, list) else data.get("jobs", [])
            for job in jobs:
                if isinstance(job, dict):
                    job["enabled"] = False
                    count += 1

            with open(jobs_json, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            return count
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to quarantine jobs: %s", e)
            return 0

    @staticmethod
    def _disable_channels(workspace_dir: Path) -> int:
        """Mark all channels as _imported_disabled in agent.json."""
        return ImportQuarantine._mark_agent_json_section(
            workspace_dir, "channels",
        )

    @staticmethod
    def _disable_mcp(workspace_dir: Path) -> int:
        """Mark all MCP configs as _imported_disabled in agent.json."""
        return ImportQuarantine._mark_agent_json_section(
            workspace_dir, "mcp",
        )

    @staticmethod
    def _mark_agent_json_section(
        workspace_dir: Path, section: str,
    ) -> int:
        """Add _imported_disabled to items in a section of agent.json."""
        agent_json = workspace_dir / "agent.json"
        if not agent_json.is_file():
            return 0

        try:
            with open(agent_json, "r", encoding="utf-8") as f:
                data = json.load(f)

            count = 0
            section_data = data.get(section)
            if isinstance(section_data, dict):
                for key, val in section_data.items():
                    if isinstance(val, dict):
                        val["_imported_disabled"] = True
                        count += 1
                    elif isinstance(val, bool) and val:
                        # e.g. channel: {enabled: true} shorthand
                        data[section][key] = {
                            "enabled": False,
                            "_imported_disabled": True,
                        }
                        count += 1

            with open(agent_json, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            return count
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to quarantine %s: %s", section, e)
            return 0
