# -*- coding: utf-8 -*-
"""Public-facing plugin endpoints for the frontend.

These routes are intentionally served without authentication so that
unauthenticated page loads (e.g. a customised login page) can fetch the
plugin list and load plugin JS/CSS bundles.

Only read operations are exposed here.  All plugin management operations
(install, upload, uninstall, reload) remain under /api/plugins/ and
require a valid Bearer token.
"""

from fastapi import APIRouter, HTTPException, Request

from .plugins import (
    _list_plugins_from_disk,
    serve_plugin_ui_file,
)

router = APIRouter(prefix="/frontend_plugin", tags=["frontend-plugin"])


@router.get(
    "",
    summary="List plugins (public)",
    description=(
        "Return all loaded plugins with frontend metadata. "
        "This endpoint is public so the frontend can load plugin bundles "
        "before the user has authenticated."
    ),
)
async def list_frontend_plugins(request: Request):
    """Return every plugin that has a frontend entry point.

    Only fields required by the frontend loader are included.  Sensitive
    management data (install paths, etc.) is not exposed here.
    """
    # The loader is published before the background phases finish. An empty
    # or partial 200 response would make the Console permanently skip plugins.
    ready = getattr(request.app.state, "frontend_plugins_ready", None)
    if ready is not None and not ready.is_set():
        raise HTTPException(
            503,
            {
                "code": "FRONTEND_PLUGINS_STARTING",
                "message": "Frontend plugins are still initializing",
            },
            headers={"Retry-After": "1"},
        )
    if getattr(request.app.state, "frontend_plugins_startup_failed", False):
        raise HTTPException(
            503,
            {
                "code": "FRONTEND_PLUGINS_FAILED",
                "message": "Frontend plugin startup failed; check the backend logs and retry after recovery",
            },
        )

    loader = getattr(request.app.state, "plugin_loader", None)

    if loader is None:
        return _list_plugins_from_disk()

    result = []
    for _plugin_id, record in loader.get_all_loaded_plugins().items():
        manifest = record.manifest
        result.append(
            {
                "id": manifest.id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "enabled": record.enabled,
                "loaded": True,
                "plugin_type": manifest.plugin_type,
                "frontend_entry": manifest.entry.frontend,
            },
        )

    return result


@router.get(
    "/{plugin_id}/files/{file_path:path}",
    summary="Serve plugin static file (public)",
    description=(
        "Serve a static asset (JS, CSS, images) from a plugin's directory. "
        "Public so plugin bundles can be loaded on the unauthenticated "
        "login page."
    ),
)
async def serve_frontend_plugin_file(
    plugin_id: str,
    file_path: str,
    request: Request,
):
    """Delegate to the authenticated static-file handler in plugins.py.

    Path-traversal protection is handled inside serve_plugin_ui_file.
    """
    return await serve_plugin_ui_file(plugin_id, file_path, request)
