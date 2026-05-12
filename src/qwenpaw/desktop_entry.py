# -*- coding: utf-8 -*-
"""Desktop entry point for Tauri sidecar auto-init + start backend."""
import os
import sys

DESKTOP_CORS_ORIGINS = (
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
)


def _ensure_desktop_cors_origins() -> None:
    origins = [
        origin.strip()
        for origin in os.environ.get("QWENPAW_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    for origin in DESKTOP_CORS_ORIGINS:
        if origin not in origins:
            origins.append(origin)
    os.environ["QWENPAW_CORS_ORIGINS"] = ",".join(origins)


os.environ.setdefault("QWENPAW_DESKTOP_APP", "1")
_ensure_desktop_cors_origins()


def main() -> None:
    from qwenpaw.cli.main import cli
    from qwenpaw.constant import WORKING_DIR

    port = os.environ.get("QWENPAW_DESKTOP_PORT", "8088")

    # Auto-initialize if no config exists
    config_path = WORKING_DIR / "config.json"
    if not config_path.exists():
        sys.argv = ["qwenpaw", "init", "--defaults", "--accept-security"]
        try:
            # pylint: disable-next=no-value-for-parameter
            cli(standalone_mode=False)
        except SystemExit:
            pass

    # Start the backend server
    sys.argv = [
        "qwenpaw",
        "app",
        "--host",
        "127.0.0.1",
        "--port",
        port,
        "--no-write-last-api",
    ]
    # pylint: disable-next=no-value-for-parameter
    cli()


if __name__ == "__main__":
    main()
