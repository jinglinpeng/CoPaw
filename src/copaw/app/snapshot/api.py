# -*- coding: utf-8 -*-
"""REST API router for snapshot operations."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .models import (
    CreateSnapshotRequest,
    ImportResult,
    RestoreSnapshotRequest,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _parse_agent_mapping_json(
    raw: Optional[str],
    field_name: str,
) -> Optional[dict[str, str]]:
    """Parse optional JSON object old_id -> new_id. None/blank means unset."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be valid JSON object",
        ) from e
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a JSON object",
        )
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} keys and values must be strings",
            )
        out[k] = v
    return out


def _get_snapshot_manager(request: Request):
    """Retrieve SnapshotManager from app state."""
    manager = getattr(request.app.state, "snapshot_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=500,
            detail="SnapshotManager not initialized",
        )
    return manager


@router.post(
    "",
    response_model=SnapshotInfo,
    summary="Create a snapshot",
)
async def create_snapshot(
    request: Request,
    body: CreateSnapshotRequest,
) -> SnapshotInfo:
    """Create a snapshot of one or more agent workspaces."""
    sm = _get_snapshot_manager(request)
    try:
        return await sm.create(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to create snapshot")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "",
    response_model=list[SnapshotInfo],
    summary="List all snapshots",
)
async def list_snapshots(request: Request) -> list[SnapshotInfo]:
    """List all locally stored snapshots."""
    sm = _get_snapshot_manager(request)
    return await sm.list_snapshots()


@router.get(
    "/{snapshot_id}",
    response_model=SnapshotInfo,
    summary="Get snapshot details",
)
async def get_snapshot(
    request: Request, snapshot_id: str,
) -> SnapshotInfo:
    """Get details for a specific snapshot."""
    sm = _get_snapshot_manager(request)
    info = await sm.get_snapshot(snapshot_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return info


@router.delete(
    "/{snapshot_id}",
    summary="Delete a snapshot",
)
async def delete_snapshot(
    request: Request, snapshot_id: str,
) -> dict:
    """Delete a snapshot ZIP file."""
    sm = _get_snapshot_manager(request)
    ok = await sm.delete_snapshot(snapshot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"success": True, "snapshot_id": snapshot_id}


@router.post(
    "/{snapshot_id}/restore",
    summary="Restore a snapshot",
)
async def restore_snapshot(
    request: Request,
    snapshot_id: str,
    body: RestoreSnapshotRequest,
) -> dict:
    """Restore a snapshot to an agent workspace."""
    sm = _get_snapshot_manager(request)
    try:
        return await sm.restore(snapshot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Restore failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/{snapshot_id}/export",
    summary="Export snapshot as downloadable ZIP",
    responses={200: {"content": {"application/zip": {}}}},
)
async def export_snapshot(
    request: Request,
    snapshot_id: str,
    include_secrets: bool = False,
) -> FileResponse:
    """Export a snapshot as a distributable ZIP file."""
    sm = _get_snapshot_manager(request)
    try:
        export_path = await sm.export_snapshot(
            snapshot_id, include_secrets=include_secrets,
        )
        return FileResponse(
            path=str(export_path),
            media_type="application/zip",
            filename=export_path.name,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/import",
    response_model=ImportResult,
    summary="Import an external snapshot",
)
async def import_snapshot(
    request: Request,
    file: UploadFile = File(..., description="Snapshot ZIP to import"),
    agent_id: Optional[str] = None,
    force: bool = False,
    agent_mappings: Optional[str] = Form(
        None,
        description=(
            'Optional JSON object: {"source_agent_id":"target_agent_id",...}. '
            "When set (including {}), every agent in the manifest is imported; "
            "omitted keys default to same-name mapping. Mutually exclusive with "
            "agent_id."
        ),
    ),
) -> ImportResult:
    """Import an external snapshot ZIP.

    All components (skills, jobs, channels, MCP) are quarantined by default.
    """
    sm = _get_snapshot_manager(request)

    parsed_mappings = _parse_agent_mapping_json(
        agent_mappings, "agent_mappings"
    )

    # Save uploaded file to temp location
    tmp = None
    try:
        suffix = ".zip"
        tmp = Path(tempfile.mktemp(suffix=suffix, prefix="copaw_import_"))
        with open(tmp, "wb") as f:
            content = await file.read()
            f.write(content)

        result = await sm.import_snapshot(
            tmp,
            target_agent_id=agent_id,
            force=force,
            agent_mappings=parsed_mappings,
        )

        # Optionally save to local snapshots
        await sm.save_import_to_local(tmp)

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Import failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if tmp and tmp.is_file():
            tmp.unlink(missing_ok=True)
