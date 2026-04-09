# -*- coding: utf-8 -*-
"""SnapshotRestorer: Three-phase state machine for restoring snapshots."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import RestorePhase, RestoreState
from .packer import SnapshotPacker

logger = logging.getLogger(__name__)


class SnapshotRestorer:
    """Implements the three-phase restore state machine.

    Phase 1 (PREPARE): validate, extract to staging
    Phase 2 (APPLY):   stop workspace, rename dirs
    Phase 3 (VERIFY):  start workspace, verify

    State is persisted at {WORKING_DIR}/_restore_state/{agent_id}.json
    for crash recovery.
    """

    def __init__(self, working_dir: Path):
        self._working_dir = working_dir
        self._state_dir = working_dir / "_restore_state"
        self._staging_base = working_dir / "_restore_staging"

    def _state_path(self, agent_id: str) -> Path:
        return self._state_dir / f"{agent_id}.json"

    def _save_state(self, state: RestoreState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(state.agent_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(state.model_dump_json(indent=2))

    def _load_state(self, agent_id: str) -> Optional[RestoreState]:
        path = self._state_path(agent_id)
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return RestoreState.model_validate_json(f.read())
        except Exception as e:
            logger.warning("Failed to load restore state for %s: %s", agent_id, e)
            return None

    def _clear_state(self, agent_id: str) -> None:
        path = self._state_path(agent_id)
        if path.is_file():
            path.unlink()

    def has_pending_restore(self, agent_id: str) -> bool:
        return self._state_path(agent_id).is_file()

    def list_pending_restores(self) -> list[str]:
        """List agent IDs with pending restore states."""
        if not self._state_dir.is_dir():
            return []
        return [
            p.stem for p in self._state_dir.glob("*.json")
        ]

    # ---- Phase 1: PREPARE ----

    def prepare(
        self,
        snapshot_path: Path,
        agent_id: str,
        workspace_dir: Path,
    ) -> RestoreState:
        """Extract snapshot to staging and validate.

        Returns RestoreState with phase=PREPARED.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        staging_dir = self._staging_base / agent_id
        backup_dir_str = str(workspace_dir) + f".backup.{ts}"

        state = RestoreState(
            phase=RestorePhase.PREPARING,
            agent_id=agent_id,
            workspace_dir=str(workspace_dir),
            backup_dir=backup_dir_str,
            staging_dir=str(staging_dir),
            snapshot_id=snapshot_path.stem,
        )
        self._save_state(state)

        # Clean previous staging if exists
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir)

        # Extract and validate
        manifest = SnapshotPacker.unpack(
            snapshot_path, staging_dir, validate=True,
        )

        # For single-agent restore, the workspace content is under
        # workspaces/{agent_id}/ in the staging dir.
        ws_content = staging_dir / "workspaces" / agent_id
        if not ws_content.is_dir():
            # Try finding the first workspace dir
            ws_base = staging_dir / "workspaces"
            if ws_base.is_dir():
                candidates = [d for d in ws_base.iterdir() if d.is_dir()]
                if len(candidates) == 1:
                    ws_content = candidates[0]

        if not ws_content.is_dir():
            raise ValueError(
                f"No workspace found for agent '{agent_id}' in snapshot"
            )

        state.last_completed_step = "staging_extracted"
        state.phase = RestorePhase.PREPARED
        self._save_state(state)

        logger.info(
            "Restore prepared for %s: staging at %s", agent_id, staging_dir,
        )
        return state

    # ---- Phase 2: APPLY ----

    def apply(self, state: RestoreState) -> RestoreState:
        """Rename directories to swap workspace content.

        Steps:
        1. Rename workspace_dir -> backup_dir
        2. Move staging workspace content -> workspace_dir
        """
        workspace_dir = Path(state.workspace_dir)
        backup_dir = Path(state.backup_dir)
        staging_dir = Path(state.staging_dir)

        state.phase = RestorePhase.APPLYING
        self._save_state(state)

        # Find the workspace content within staging
        ws_content = self._find_workspace_content(
            staging_dir, state.agent_id,
        )

        # Step 1: Rename current workspace -> backup
        if workspace_dir.is_dir():
            workspace_dir.rename(backup_dir)
            state.last_completed_step = "workspace_renamed_to_backup"
            self._save_state(state)
            logger.info("Renamed workspace to backup: %s", backup_dir)

        # Step 2: Move staging workspace content -> workspace_dir
        ws_content.rename(workspace_dir)
        state.last_completed_step = "staging_renamed_to_workspace"
        state.phase = RestorePhase.APPLIED
        self._save_state(state)
        logger.info("Staging content moved to workspace: %s", workspace_dir)

        # Clean up remaining staging dirs
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)

        return state

    # ---- Phase 3: VERIFY ----

    def mark_verified(self, state: RestoreState) -> None:
        """Mark restore as complete and clean up state file."""
        self._clear_state(state.agent_id)
        logger.info("Restore verified and complete for %s", state.agent_id)

    def rollback(self, state: RestoreState) -> bool:
        """Roll back to backup if verify failed.

        Returns True if rollback succeeded.
        """
        workspace_dir = Path(state.workspace_dir)
        backup_dir = Path(state.backup_dir)

        if not backup_dir.is_dir():
            logger.error(
                "Cannot rollback: backup dir missing: %s", backup_dir,
            )
            return False

        try:
            if workspace_dir.is_dir():
                shutil.rmtree(workspace_dir)
            backup_dir.rename(workspace_dir)
            self._clear_state(state.agent_id)
            logger.info("Rolled back to backup for %s", state.agent_id)
            return True
        except Exception as e:
            logger.error("Rollback failed for %s: %s", state.agent_id, e)
            return False

    # ---- Crash Recovery ----

    def recover(self, agent_id: str) -> Optional[str]:
        """Check for incomplete restore and recover.

        Returns action taken or None if no recovery needed.
        """
        state = self._load_state(agent_id)
        if state is None:
            return None

        step = state.last_completed_step
        workspace_dir = Path(state.workspace_dir)
        backup_dir = Path(state.backup_dir)
        staging_dir = Path(state.staging_dir)

        logger.info(
            "Recovering restore for %s at step '%s'", agent_id, step,
        )

        if step in ("", "checksum_verified", "staging_extracted"):
            # Safe: workspace untouched, just clean staging
            if staging_dir.is_dir():
                shutil.rmtree(staging_dir, ignore_errors=True)
            self._clear_state(agent_id)
            return "cleaned_staging"

        if step == "workspace_stopped":
            # Workspace was stopped but dir still exists
            self._clear_state(agent_id)
            if staging_dir.is_dir():
                shutil.rmtree(staging_dir, ignore_errors=True)
            return "cleaned_staging_after_stop"

        if step == "workspace_renamed_to_backup":
            # workspace_dir gone, staging still there
            # Move staging content to workspace_dir
            ws_content = self._find_workspace_content(
                staging_dir, agent_id,
            )
            if ws_content and ws_content.is_dir():
                ws_content.rename(workspace_dir)
                if staging_dir.is_dir():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                self._clear_state(agent_id)
                return "completed_from_staging"
            else:
                # Staging broken, restore from backup
                if backup_dir.is_dir():
                    backup_dir.rename(workspace_dir)
                self._clear_state(agent_id)
                return "restored_from_backup"

        if step in ("staging_renamed_to_workspace", "workspace_started"):
            # New workspace is in place, just need to verify
            self._clear_state(agent_id)
            return "verify_needed"

        # Unknown step, clean up
        self._clear_state(agent_id)
        return "unknown_step_cleared"

    def _find_workspace_content(
        self, staging_dir: Path, agent_id: str,
    ) -> Optional[Path]:
        """Locate workspace content within staging directory."""
        # Direct path
        direct = staging_dir / "workspaces" / agent_id
        if direct.is_dir():
            return direct

        # Single workspace fallback
        ws_base = staging_dir / "workspaces"
        if ws_base.is_dir():
            candidates = [d for d in ws_base.iterdir() if d.is_dir()]
            if len(candidates) == 1:
                return candidates[0]

        return None
