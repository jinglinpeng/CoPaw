# -*- coding: utf-8 -*-
"""StateCollector: Collects workspace files into a staging directory."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Files/dirs that belong to a workspace
WORKSPACE_FILES = {
    "agent.json",
    "chats.json",
    "jobs.json",
    "skill.json",
    "AGENTS.md",
    "SOUL.md",
    "PROFILE.md",
}

WORKSPACE_DIRS = {
    "sessions",
    "skills",
    "memory",
}

# Global config files to include when --include-global
GLOBAL_FILES = {
    "config.json",
    "settings.json",
}

GLOBAL_DIRS = {
    "skill_pool",
}


class StateCollector:
    """Collects files from workspace(s) into a staging directory."""

    @staticmethod
    def collect_workspace(
        workspace_dir: Path,
        agent_id: str,
        staging_dir: Path,
        *,
        exclude_sessions: bool = False,
        exclude_memory: bool = False,
    ) -> int:
        """Copy workspace files into staging_dir/workspaces/{agent_id}/.

        Returns number of files copied.
        """
        dest = staging_dir / "workspaces" / agent_id
        dest.mkdir(parents=True, exist_ok=True)
        count = 0

        # Copy known files
        for fname in WORKSPACE_FILES:
            src = workspace_dir / fname
            if src.is_file():
                shutil.copy2(src, dest / fname)
                count += 1

        # Copy any extra .md files referenced by system_prompt_files
        for md_file in workspace_dir.glob("*.md"):
            if md_file.name not in WORKSPACE_FILES:
                shutil.copy2(md_file, dest / md_file.name)
                count += 1

        # Copy directories
        skip_dirs: Set[str] = set()
        if exclude_sessions:
            skip_dirs.add("sessions")
        if exclude_memory:
            skip_dirs.add("memory")

        for dirname in WORKSPACE_DIRS:
            if dirname in skip_dirs:
                continue
            src_dir = workspace_dir / dirname
            if src_dir.is_dir():
                dest_sub = dest / dirname
                shutil.copytree(
                    src_dir,
                    dest_sub,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".DS_Store",
                    ),
                )
                count += sum(1 for _ in dest_sub.rglob("*") if _.is_file())

        logger.info(
            "Collected %d files from workspace %s", count, agent_id,
        )
        return count

    @staticmethod
    def collect_global(
        working_dir: Path,
        staging_dir: Path,
    ) -> int:
        """Copy global config files into staging_dir/global/.

        Returns number of files copied.
        """
        dest = staging_dir / "global"
        dest.mkdir(parents=True, exist_ok=True)
        count = 0

        for fname in GLOBAL_FILES:
            src = working_dir / fname
            if src.is_file():
                shutil.copy2(src, dest / fname)
                count += 1

        for dirname in GLOBAL_DIRS:
            src_dir = working_dir / dirname
            if src_dir.is_dir():
                dest_sub = dest / dirname
                shutil.copytree(
                    src_dir,
                    dest_sub,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                count += sum(1 for _ in dest_sub.rglob("*") if _.is_file())

        logger.info("Collected %d global files", count)
        return count

    @staticmethod
    def collect_secrets(
        secret_dir: Path,
        staging_dir: Path,
    ) -> int:
        """Copy secret files into staging_dir/secrets/.

        Returns number of files copied.
        """
        dest = staging_dir / "secrets"
        dest.mkdir(parents=True, exist_ok=True)
        count = 0

        if not secret_dir.is_dir():
            return 0

        for item in secret_dir.iterdir():
            if item.name.startswith("."):
                continue
            target = dest / item.name
            if item.is_file():
                shutil.copy2(item, target)
                count += 1
            elif item.is_dir():
                shutil.copytree(
                    item, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                count += sum(1 for _ in target.rglob("*") if _.is_file())

        logger.info("Collected %d secret files", count)
        return count

    @staticmethod
    def estimate_size(
        workspace_dirs: list[Path],
        working_dir: Path,
        *,
        include_global: bool = False,
        include_secrets: bool = False,
        secret_dir: Optional[Path] = None,
        exclude_sessions: bool = False,
        exclude_memory: bool = False,
    ) -> int:
        """Estimate snapshot size in bytes without copying."""
        total = 0

        for ws_dir in workspace_dirs:
            if not ws_dir.is_dir():
                continue
            for fname in WORKSPACE_FILES:
                f = ws_dir / fname
                if f.is_file():
                    total += f.stat().st_size
            for md_file in ws_dir.glob("*.md"):
                if md_file.name not in WORKSPACE_FILES:
                    total += md_file.stat().st_size

            skip = set()
            if exclude_sessions:
                skip.add("sessions")
            if exclude_memory:
                skip.add("memory")
            for dirname in WORKSPACE_DIRS:
                if dirname in skip:
                    continue
                d = ws_dir / dirname
                if d.is_dir():
                    total += sum(
                        f.stat().st_size for f in d.rglob("*") if f.is_file()
                    )

        if include_global:
            for fname in GLOBAL_FILES:
                f = working_dir / fname
                if f.is_file():
                    total += f.stat().st_size
            for dirname in GLOBAL_DIRS:
                d = working_dir / dirname
                if d.is_dir():
                    total += sum(
                        f.stat().st_size
                        for f in d.rglob("*") if f.is_file()
                    )

        if include_secrets and secret_dir and secret_dir.is_dir():
            total += sum(
                f.stat().st_size
                for f in secret_dir.rglob("*") if f.is_file()
            )

        return total
