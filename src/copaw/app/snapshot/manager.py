# -*- coding: utf-8 -*-
"""SnapshotManager: Top-level coordinator for all snapshot operations."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..multi_agent_manager import (
    snapshot_agent_operation_key,
    snapshot_export_operation_key,
)
from .collector import StateCollector
from .crypto import decrypt_to_plaintext, encrypt_plaintext, is_encrypted_package
from .health import HealthChecker
from .models import (
    CreateSnapshotRequest,
    ImportResult,
    PerAgentImportOutcome,
    RestoreMode,
    RestoreSnapshotRequest,
    SnapshotInfo,
    SnapshotManifest,
    SnapshotScope,
)
from .packer import SnapshotPacker
from .quarantine import ImportQuarantine
from .restorer import SnapshotRestorer
from .sanitizer import SecretSanitizer

if TYPE_CHECKING:
    from ..multi_agent_manager import MultiAgentManager

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Coordinates snapshot create/restore/export/import operations.

    Serializes per-agent work via MultiAgentManager operation locks so
    snapshot/restore/import aligns with reload/stop on the same agent.
    """

    def __init__(
        self,
        working_dir: Path,
        secret_dir: Path,
        multi_agent_manager: "MultiAgentManager",
    ):
        self._working_dir = working_dir
        self._secret_dir = secret_dir
        self._manager = multi_agent_manager
        self._snapshots_dir = working_dir / "snapshots"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._restorer = SnapshotRestorer(working_dir)

    # ------------------------------------------------------------------ #
    #  CREATE
    # ------------------------------------------------------------------ #

    async def create(self, req: CreateSnapshotRequest) -> SnapshotInfo:
        """Create a snapshot of one or more agents."""
        agent_ids = self._resolve_agent_ids(req)
        if not agent_ids:
            raise ValueError("No agents specified for snapshot")

        scope = req.scope
        if len(agent_ids) == 1 and scope == SnapshotScope.SINGLE:
            pass
        elif scope == SnapshotScope.ALL:
            req.include_global = True

        keys = [snapshot_agent_operation_key(aid) for aid in agent_ids]
        async with self._manager.hold_operation_locks(keys):
            return await asyncio.to_thread(
                self._create_sync, agent_ids, req,
            )

    def _create_sync(
        self,
        agent_ids: List[str],
        req: CreateSnapshotRequest,
    ) -> SnapshotInfo:
        """Synchronous snapshot creation."""
        staging = Path(tempfile.mkdtemp(prefix="copaw_snap_"))
        try:
            # Collect workspace files
            for agent_id in agent_ids:
                ws_dir = self._get_workspace_dir(agent_id)
                if ws_dir and ws_dir.is_dir():
                    StateCollector.collect_workspace(
                        ws_dir,
                        agent_id,
                        staging,
                        exclude_sessions=req.exclude_sessions,
                        exclude_memory=req.exclude_memory,
                    )

            # Collect global config if requested
            if req.include_global:
                StateCollector.collect_global(self._working_dir, staging)

            # Collect secrets if requested
            if req.include_secrets:
                StateCollector.collect_secrets(self._secret_dir, staging)

            # Build manifest
            manifest = SnapshotManifest(
                agent_ids=agent_ids,
                scope=req.scope,
                includes_secrets=req.include_secrets,
                includes_global=req.include_global,
                notes=req.note,
            )

            # Generate filename and pack
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            scope_label = manifest.to_filename_scope()
            filename = f"copaw-snapshot-{scope_label}-{ts}.zip"
            output_path = self._snapshots_dir / filename

            SnapshotPacker.pack(staging, manifest, output_path)

            return SnapshotInfo(
                snapshot_id=output_path.stem,
                filename=filename,
                scope=req.scope,
                agent_ids=agent_ids,
                created_at=manifest.created_at,
                size_bytes=output_path.stat().st_size,
                includes_secrets=req.include_secrets,
                includes_global=req.include_global,
                notes=req.note,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------------------ #
    #  LIST / GET / DELETE
    # ------------------------------------------------------------------ #

    async def list_snapshots(self) -> List[SnapshotInfo]:
        """List all local snapshots."""
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> List[SnapshotInfo]:
        results = []
        for zp in sorted(
            self._snapshots_dir.glob("*.zip"), reverse=True,
        ):
            try:
                manifest = SnapshotPacker.read_manifest(zp)
                results.append(SnapshotInfo(
                    snapshot_id=zp.stem,
                    filename=zp.name,
                    scope=manifest.scope,
                    agent_ids=manifest.agent_ids,
                    created_at=manifest.created_at,
                    size_bytes=zp.stat().st_size,
                    includes_secrets=manifest.includes_secrets,
                    includes_global=manifest.includes_global,
                    notes=manifest.notes,
                ))
            except Exception as e:
                logger.warning("Skipping invalid snapshot %s: %s", zp.name, e)
        return results

    async def get_snapshot(self, snapshot_id: str) -> Optional[SnapshotInfo]:
        """Get info for a specific snapshot."""
        snapshots = await self.list_snapshots()
        for s in snapshots:
            if s.snapshot_id == snapshot_id:
                return s
        return None

    async def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot ZIP file."""
        zp = self._snapshots_dir / f"{snapshot_id}.zip"
        if zp.is_file():
            zp.unlink()
            logger.info("Deleted snapshot: %s", snapshot_id)
            return True
        return False

    def get_snapshot_path(self, snapshot_id: str) -> Optional[Path]:
        """Get filesystem path for a snapshot."""
        zp = self._snapshots_dir / f"{snapshot_id}.zip"
        return zp if zp.is_file() else None

    # ------------------------------------------------------------------ #
    #  RESTORE
    # ------------------------------------------------------------------ #

    async def restore(
        self,
        snapshot_id: str,
        req: RestoreSnapshotRequest,
    ) -> dict:
        """Restore a snapshot to an agent workspace.

        Supports in-place restore and clone-to-new-agent.
        """
        snap_path = self.get_snapshot_path(snapshot_id)
        if not snap_path:
            raise ValueError(f"Snapshot not found: {snapshot_id}")

        target_agent_id = req.agent_id
        if req.mode == RestoreMode.CLONE:
            if not req.new_agent_id:
                raise ValueError("new_agent_id required for clone mode")
            target_agent_id = req.new_agent_id

        async with self._manager.hold_operation_locks(
            [snapshot_agent_operation_key(target_agent_id)],
        ):
            return await self._restore_impl(
                snap_path,
                req.agent_id,
                target_agent_id,
                req.mode,
                operation_lock_held=True,
            )

    async def _restore_impl(
        self,
        snap_path: Path,
        source_agent_id: str,
        target_agent_id: str,
        mode: RestoreMode,
        *,
        operation_lock_held: bool = False,
    ) -> dict:
        """Internal restore implementation."""
        from ...config.utils import load_config, save_config

        config = load_config()

        if mode == RestoreMode.IN_PLACE:
            if target_agent_id not in config.agents.profiles:
                raise ValueError(
                    f"Agent '{target_agent_id}' not found for restore"
                )
            ws_dir = Path(
                config.agents.profiles[target_agent_id].workspace_dir,
            )

            # Stop workspace if running
            if self._manager.is_agent_loaded(target_agent_id):
                await self._manager.stop_agent(
                    target_agent_id,
                    operation_lock_held=operation_lock_held,
                )

            # Phase 1: Prepare
            state = await asyncio.to_thread(
                self._restorer.prepare,
                snap_path, source_agent_id, ws_dir,
            )

            # Phase 2: Apply
            state = await asyncio.to_thread(self._restorer.apply, state)

            # Phase 3: Verify - restart workspace
            try:
                await self._manager.get_agent(
                    target_agent_id,
                    operation_lock_held=operation_lock_held,
                )
                self._restorer.mark_verified(state)
                return {
                    "success": True,
                    "agent_id": target_agent_id,
                    "mode": "in_place",
                    "message": f"已恢复到快照 {snap_path.stem}",
                }
            except Exception as e:
                logger.error("Verify failed, rolling back: %s", e)
                rollback_ok = await asyncio.to_thread(
                    self._restorer.rollback, state,
                )
                # Try to restart with backup
                if rollback_ok:
                    try:
                        await self._manager.get_agent(
                            target_agent_id,
                            operation_lock_held=operation_lock_held,
                        )
                    except Exception:
                        pass
                raise ValueError(f"恢复验证失败，已回滚: {e}") from e

        else:
            # Clone mode: extract to a new workspace
            ws_base = self._working_dir / "workspaces"
            new_ws_dir = ws_base / target_agent_id
            if new_ws_dir.is_dir():
                raise ValueError(
                    f"Agent '{target_agent_id}' already exists"
                )

            staging = Path(tempfile.mkdtemp(prefix="copaw_clone_"))
            try:
                manifest = await asyncio.to_thread(
                    SnapshotPacker.unpack, snap_path, staging,
                )

                # Find the source workspace in staging
                ws_content = staging / "workspaces" / source_agent_id
                if not ws_content.is_dir():
                    ws_base_staging = staging / "workspaces"
                    if ws_base_staging.is_dir():
                        candidates = [
                            d for d in ws_base_staging.iterdir()
                            if d.is_dir()
                        ]
                        if candidates:
                            ws_content = candidates[0]

                if not ws_content.is_dir():
                    raise ValueError(
                        f"No workspace for '{source_agent_id}' in snapshot"
                    )

                # Copy to new workspace directory
                shutil.copytree(ws_content, new_ws_dir)

                # Register the new agent in config
                from ...config.config import AgentProfileRef
                config = load_config()
                config.agents.profiles[target_agent_id] = AgentProfileRef(
                    id=target_agent_id,
                    workspace_dir=str(new_ws_dir),
                    enabled=True,
                )
                save_config(config)

                # Start the new workspace
                await self._manager.get_agent(
                    target_agent_id,
                    operation_lock_held=operation_lock_held,
                )

                return {
                    "success": True,
                    "agent_id": target_agent_id,
                    "mode": "clone",
                    "message": f"已从快照克隆为新 Agent '{target_agent_id}'",
                }
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------------------ #
    #  EXPORT
    # ------------------------------------------------------------------ #

    async def export_snapshot(
        self,
        snapshot_id: str,
        *,
        include_secrets: bool = False,
        password: Optional[str] = None,
    ) -> Path:
        """Export a snapshot as a distributable ZIP.

        By default strips secrets from the exported file.
        When ``password`` is set, the ZIP bytes are encrypted (file may still
        use ``.zip`` extension).

        Returns path to the export file.
        """
        snap_path = self.get_snapshot_path(snapshot_id)
        if not snap_path:
            raise ValueError(f"Snapshot not found: {snapshot_id}")

        lock = self._manager.get_operation_lock(
            snapshot_export_operation_key(snapshot_id),
        )
        async with lock:
            return await asyncio.to_thread(
                self._export_sync, snap_path, include_secrets, password,
            )

    def _export_sync(
        self,
        snap_path: Path,
        include_secrets: bool,
        password: Optional[str] = None,
    ) -> Path:
        """Create sanitized export copy; optionally AES-GCM encrypt output."""
        pw = (password or "").strip() or None

        if include_secrets:
            plain_path = snap_path
        else:
            staging = Path(tempfile.mkdtemp(prefix="copaw_export_"))
            try:
                manifest = SnapshotPacker.unpack(
                    snap_path, staging, validate=False,
                )
                SecretSanitizer.sanitize_staging(staging)
                manifest.includes_secrets = False

                ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                export_name = (
                    f"copaw-export-{manifest.to_filename_scope()}-{ts}.zip"
                )
                export_path = self._snapshots_dir / export_name

                SnapshotPacker.pack(staging, manifest, export_path)
                plain_path = export_path
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        if not pw:
            return plain_path

        try:
            data = plain_path.read_bytes()
            enc = encrypt_plaintext(data, pw)
            manifest = SnapshotPacker.read_manifest(snap_path)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            enc_name = f"copaw-export-{manifest.to_filename_scope()}-{ts}.zip"
            enc_path = self._snapshots_dir / enc_name
            enc_path.write_bytes(enc)
            return enc_path
        finally:
            if not include_secrets and plain_path != snap_path:
                plain_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    #  IMPORT
    # ------------------------------------------------------------------ #

    @staticmethod
    def _plan_import(
        manifest: SnapshotManifest,
        target_agent_id: Optional[str],
        agent_mappings: Optional[Dict[str, str]],
    ) -> List[Tuple[str, str]]:
        """Return ordered (source_agent_id, target_agent_id) pairs."""
        if target_agent_id is not None and agent_mappings is not None:
            raise ValueError(
                "Cannot specify both agent_id (single-target override) and "
                "multi-agent mapping. Use agent_id alone to import only the "
                "first manifest agent under that target id, or use "
                "agent_mappings (form field) to import every agent listed "
                "in the manifest.",
            )
        if not manifest.agent_ids:
            raise ValueError("Snapshot contains no agents")

        if agent_mappings is None:
            # Legacy: only the first agent in manifest order is imported.
            src = manifest.agent_ids[0]
            tgt = target_agent_id or src
            return [(src, tgt)]

        for key in agent_mappings:
            if key not in manifest.agent_ids:
                raise ValueError(
                    f"Import mapping references unknown source agent_id "
                    f"'{key}' (not in snapshot manifest)",
                )

        pairs = [
            (sid, agent_mappings.get(sid, sid)) for sid in manifest.agent_ids
        ]
        targets = [t for _, t in pairs]
        if len(set(targets)) != len(targets):
            raise ValueError(
                "Duplicate target agent_id: multiple sources map to the same "
                "target",
            )
        return pairs

    def _preflight_import_targets(
        self,
        plan: List[Tuple[str, str]],
        force: bool,
    ) -> None:
        """Fail fast before unpack/import when targets would conflict (no force)."""
        if force:
            return
        from ...config.utils import load_config

        config = load_config()
        ws_base = self._working_dir / "workspaces"
        for _src, tgt in plan:
            if tgt in config.agents.profiles:
                raise ValueError(
                    f"Cannot import: target agent '{tgt}' already exists. "
                    f"Use force=True to replace, or choose a different target id.",
                )
            if (ws_base / tgt).is_dir():
                raise ValueError(
                    f"Cannot import: workspace directory for '{tgt}' already "
                    f"exists. Use force=True or remove the directory.",
                )

    @staticmethod
    def _find_workspace_in_import_staging(
        staging: Path,
        source_agent_id: str,
        *,
        strict_workspace_paths: bool,
    ) -> Path:
        """Locate workspace directory inside unpacked staging."""
        ws_content = staging / "workspaces" / source_agent_id
        if ws_content.is_dir():
            return ws_content
        if strict_workspace_paths:
            raise ValueError(
                f"No workspace for source agent '{source_agent_id}' in "
                f"import package",
            )
        ws_base = staging / "workspaces"
        if ws_base.is_dir():
            candidates = [d for d in ws_base.iterdir() if d.is_dir()]
            if candidates:
                return candidates[0]
        raise ValueError("No workspace found in import package")

    async def import_snapshot(
        self,
        file_path: Path,
        *,
        target_agent_id: Optional[str] = None,
        force: bool = False,
        agent_mappings: Optional[Dict[str, str]] = None,
        password: Optional[str] = None,
    ) -> ImportResult:
        """Import an external snapshot ZIP (always untrusted).

        When ``agent_mappings`` is omitted, imports only the **first** manifest
        agent (``manifest.agent_ids[0]``); target id is ``target_agent_id`` if
        set, otherwise the source id. When ``agent_mappings`` is set (including
        ``{}``), imports **every** manifest agent in order; each maps to
        ``agent_mappings.get(source, source)`` unless ``target_agent_id`` is also
        set (then error).

        Encrypted export blobs (see ``crypto``) require the correct
        ``password``.
        """
        if not file_path.is_file():
            raise ValueError(f"File not found: {file_path}")

        raw = await asyncio.to_thread(file_path.read_bytes)
        work_path = file_path
        decrypted_tmp: Optional[Path] = None
        try:
            if is_encrypted_package(raw):
                if not (password or "").strip():
                    raise ValueError(
                        "此快照已加密，导入时请提供 password 参数",
                    )

                def _dec() -> bytes:
                    return decrypt_to_plaintext(raw, password.strip())

                plain = await asyncio.to_thread(_dec)

                def _write_decrypted() -> Path:
                    with tempfile.NamedTemporaryFile(
                        suffix=".zip",
                        prefix="copaw_import_dec_",
                        delete=False,
                    ) as tf:
                        tf.write(plain)
                    return Path(tf.name)

                decrypted_tmp = await asyncio.to_thread(_write_decrypted)
                work_path = decrypted_tmp

            manifest = await asyncio.to_thread(
                SnapshotPacker.read_manifest, work_path,
            )
            plan = self._plan_import(
                manifest, target_agent_id, agent_mappings,
            )
            self._preflight_import_targets(plan, force)
            strict_paths = agent_mappings is not None
            lock_keys = [
                snapshot_agent_operation_key(tgt) for _, tgt in plan
            ]

            async with self._manager.hold_operation_locks(lock_keys):
                return await self._import_execute(
                    work_path,
                    plan,
                    force,
                    strict_workspace_paths=strict_paths,
                    operation_lock_held=True,
                )
        finally:
            if decrypted_tmp is not None and decrypted_tmp.is_file():
                decrypted_tmp.unlink(missing_ok=True)

    async def _import_one_agent_from_staging(
        self,
        staging: Path,
        source_agent_id: str,
        agent_id: str,
        force: bool,
        *,
        strict_workspace_paths: bool,
        operation_lock_held: bool = False,
    ) -> PerAgentImportOutcome:
        """Import a single agent from already-unpacked staging."""
        from ...config.utils import load_config, save_config

        config = load_config()
        if agent_id in config.agents.profiles and not force:
            raise ValueError(
                f"Agent '{agent_id}' already exists. "
                f"Use force=True or choose a different ID.",
            )

        ws_content = self._find_workspace_in_import_staging(
            staging,
            source_agent_id,
            strict_workspace_paths=strict_workspace_paths,
        )

        summary = await asyncio.to_thread(
            ImportQuarantine.quarantine, ws_content,
        )

        ws_base = self._working_dir / "workspaces"
        ws_base.mkdir(parents=True, exist_ok=True)
        target_ws_dir = ws_base / agent_id

        if target_ws_dir.is_dir():
            if force:
                if self._manager.is_agent_loaded(agent_id):
                    await self._manager.stop_agent(
                        agent_id,
                        operation_lock_held=operation_lock_held,
                    )
                backup_ts = datetime.now(timezone.utc).strftime(
                    "%Y%m%d%H%M%S",
                )
                backup_path = Path(
                    str(target_ws_dir) + f".backup.{backup_ts}",
                )
                target_ws_dir.rename(backup_path)
            else:
                raise ValueError(
                    f"Workspace directory already exists: {target_ws_dir}",
                )

        shutil.copytree(ws_content, target_ws_dir)

        from ...config.config import AgentProfileRef

        config = load_config()
        config.agents.profiles[agent_id] = AgentProfileRef(
            id=agent_id,
            workspace_dir=str(target_ws_dir),
            enabled=True,
        )
        save_config(config)

        status, todos = await asyncio.to_thread(
            HealthChecker.check, target_ws_dir, agent_id,
        )

        file_summary = {
            "agent_config": "✓" if (target_ws_dir / "agent.json").is_file() else "✗",
            "skills_quarantined": str(summary.get("skills", 0)),
            "jobs_quarantined": str(summary.get("jobs", 0)),
            "channels_quarantined": str(summary.get("channels", 0)),
            "mcp_quarantined": str(summary.get("mcp", 0)),
        }

        sessions_dir = target_ws_dir / "sessions"
        if sessions_dir.is_dir():
            session_count = sum(1 for f in sessions_dir.glob("*.json"))
            file_summary["sessions"] = str(session_count)

        try:
            await self._manager.get_agent(
                agent_id,
                operation_lock_held=operation_lock_held,
            )
        except Exception as e:
            logger.warning("Failed to start imported agent %s: %s", agent_id, e)

        return PerAgentImportOutcome(
            source_agent_id=source_agent_id,
            target_agent_id=agent_id,
            status=status,
            file_summary=file_summary,
            todos=todos,
        )

    async def _import_execute(
        self,
        file_path: Path,
        plan: List[Tuple[str, str]],
        force: bool,
        *,
        strict_workspace_paths: bool,
        operation_lock_held: bool = False,
    ) -> ImportResult:
        """Unpack once and import each planned source→target pair."""
        staging = Path(tempfile.mkdtemp(prefix="copaw_import_"))
        try:
            await asyncio.to_thread(
                SnapshotPacker.unpack, file_path, staging, validate=True,
            )
            outcomes: List[PerAgentImportOutcome] = []
            for source_agent_id, agent_id in plan:
                outcome = await self._import_one_agent_from_staging(
                    staging,
                    source_agent_id,
                    agent_id,
                    force,
                    strict_workspace_paths=strict_workspace_paths,
                    operation_lock_held=operation_lock_held,
                )
                outcomes.append(outcome)

            first = outcomes[0]
            summary = dict(first.file_summary)
            summary["agents_imported"] = str(len(outcomes))
            if len(outcomes) > 1:
                summary["import_mapping"] = ";".join(
                    f"{o.source_agent_id}->{o.target_agent_id}"
                    for o in outcomes
                )
            return ImportResult(
                agent_id=first.target_agent_id,
                status=first.status,
                file_summary=summary,
                todos=first.todos,
                agent_outcomes=outcomes,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # Also save a copy to local snapshots
    async def save_import_to_local(self, file_path: Path) -> None:
        """Save imported ZIP to local snapshots dir for future restore."""
        dest = self._snapshots_dir / file_path.name
        if not dest.is_file():
            await asyncio.to_thread(shutil.copy2, file_path, dest)

    # ------------------------------------------------------------------ #
    #  CRASH RECOVERY
    # ------------------------------------------------------------------ #

    async def check_crash_recovery(self) -> list[str]:
        """Check and recover from any interrupted restore operations.

        Should be called during application startup.
        Returns list of recovery actions taken.
        """
        actions = []
        pending = self._restorer.list_pending_restores()
        for agent_id in pending:
            action = await asyncio.to_thread(
                self._restorer.recover, agent_id,
            )
            if action:
                actions.append(f"{agent_id}: {action}")
                logger.info("Crash recovery for %s: %s", agent_id, action)
        return actions

    # ------------------------------------------------------------------ #
    #  HELPERS
    # ------------------------------------------------------------------ #

    def _resolve_agent_ids(self, req: CreateSnapshotRequest) -> List[str]:
        """Resolve the list of agent IDs from a create request."""
        from ...config.utils import load_config

        if req.scope == SnapshotScope.ALL:
            config = load_config()
            return list(config.agents.profiles.keys())

        if req.agent_ids:
            return req.agent_ids

        # Default to active agent
        config = load_config()
        active = config.agents.active_agent or "default"
        return [active]

    def _get_workspace_dir(self, agent_id: str) -> Optional[Path]:
        """Get workspace directory for an agent from config."""
        from ...config.utils import load_config

        config = load_config()
        profile = config.agents.profiles.get(agent_id)
        if profile:
            return Path(profile.workspace_dir)
        return None
