# -*- coding: utf-8 -*-
"""HealthChecker: Post-import runability check and todo list generation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from .models import AgentStatus, TodoItem

logger = logging.getLogger(__name__)


class HealthChecker:
    """Checks imported workspace for runability and generates a todo list."""

    @staticmethod
    def check(workspace_dir: Path, agent_id: str) -> tuple[AgentStatus, list[TodoItem]]:
        """Run health checks on an imported workspace.

        Returns (status, todo_list).
        """
        todos: List[TodoItem] = []

        # Check agent.json exists
        agent_json = workspace_dir / "agent.json"
        if not agent_json.is_file():
            todos.append(TodoItem(
                severity="required",
                message="agent.json 不存在",
                action="请检查导入包完整性",
            ))
            return AgentStatus.NEEDS_SETUP, todos

        try:
            with open(agent_json, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError):
            todos.append(TodoItem(
                severity="required",
                message="agent.json 格式错误",
                action="请检查文件内容",
            ))
            return AgentStatus.NEEDS_SETUP, todos

        # Check channels for _imported_disabled
        channels = config.get("channels", {})
        disabled_channels = []
        if isinstance(channels, dict):
            for name, ch_conf in channels.items():
                if isinstance(ch_conf, dict) and ch_conf.get(
                    "_imported_disabled"
                ):
                    disabled_channels.append(name)

        if disabled_channels:
            todos.append(TodoItem(
                severity="suggested",
                message=f"{len(disabled_channels)} 个渠道待重新认证",
                action="在 Console -> Channels 中配置",
            ))

        # Check MCP for _imported_disabled
        mcp = config.get("mcp", {})
        disabled_mcp = []
        if isinstance(mcp, dict):
            for name, mcp_conf in mcp.items():
                if isinstance(mcp_conf, dict) and mcp_conf.get(
                    "_imported_disabled"
                ):
                    disabled_mcp.append(name)

        if disabled_mcp:
            todos.append(TodoItem(
                severity="suggested",
                message=f"{len(disabled_mcp)} 个 MCP 服务待确认连接",
                action="在 Console -> MCP 中确认",
            ))

        # Check skills
        skill_json = workspace_dir / "skill.json"
        disabled_skills = 0
        if skill_json.is_file():
            try:
                with open(skill_json, "r", encoding="utf-8") as f:
                    skill_data = json.load(f)
                if isinstance(skill_data, dict):
                    skills = skill_data.get("skills", skill_data)
                    if isinstance(skills, list):
                        disabled_skills = sum(
                            1 for s in skills
                            if isinstance(s, dict) and not s.get("enabled", True)
                        )
                    elif isinstance(skills, dict):
                        disabled_skills = sum(
                            1 for s in skills.values()
                            if isinstance(s, dict) and not s.get("enabled", True)
                        )
            except (json.JSONDecodeError, OSError):
                pass

        if disabled_skills > 0:
            todos.append(TodoItem(
                severity="suggested",
                message=f"{disabled_skills} 个技能待审核启用",
                action="在 Console -> Skills 中逐个审核",
            ))

        # Check jobs
        jobs_json = workspace_dir / "jobs.json"
        disabled_jobs = 0
        if jobs_json.is_file():
            try:
                with open(jobs_json, "r", encoding="utf-8") as f:
                    jobs_data = json.load(f)
                jobs_list = (
                    jobs_data if isinstance(jobs_data, list)
                    else jobs_data.get("jobs", [])
                )
                disabled_jobs = sum(
                    1 for j in jobs_list
                    if isinstance(j, dict) and not j.get("enabled", True)
                )
            except (json.JSONDecodeError, OSError):
                pass

        if disabled_jobs > 0:
            todos.append(TodoItem(
                severity="suggested",
                message=f"{disabled_jobs} 个定时任务已暂停",
                action="在 Console -> Cron Jobs 中审核启用",
            ))

        # Determine status
        has_required = any(t.severity == "required" for t in todos)
        has_suggested = any(t.severity == "suggested" for t in todos)

        if has_required:
            status = AgentStatus.NEEDS_SETUP
        elif has_suggested:
            status = AgentStatus.NEEDS_REVIEW
        else:
            status = AgentStatus.READY

        return status, todos
