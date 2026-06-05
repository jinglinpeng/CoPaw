# -*- coding: utf-8 -*-
"""Bundled Python environment utilities for frozen/packaged builds.

When QwenPaw is packaged with PyInstaller for Tauri, a standalone
Python embedded distribution is shipped alongside the executable in
``python-embed/``.  This module provides helpers to locate that
interpreter so plugins can install dependencies at runtime and spawn
subprocesses (e.g. the desktop pet) without polluting the user's
system Python.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache: ``None`` = not searched yet, ``""`` = searched but not found.
_cached_executable: str | None = None
_searched = False


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


def get_bundled_python_dir() -> Path | None:
    """Return the ``python-embed`` directory shipped with the package.

    In a PyInstaller *onedir* layout the directory structure is::

        qwenpaw-backend/
        ├── qwenpaw-backend.exe
        ├── _internal/          ← sys._MEIPASS
        └── python-embed/
            ├── python.exe
            └── ...

    Returns ``None`` when not in a frozen environment or when the
    directory does not exist.
    """
    if not _is_frozen():
        return None

    # In onedir mode sys._MEIPASS → <collect>/_internal
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    exe_dir = Path(sys.executable).parent

    candidates = [
        exe_dir / "python-embed",  # next to the exe
        meipass.parent / "python-embed",  # sibling of _internal
        meipass / "python-embed",  # inside _internal
    ]

    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    for candidate in candidates:
        if (candidate / exe_name).is_file():
            return candidate.resolve()

    return None


def get_bundled_python_executable() -> str | None:
    """Return the full path to the bundled Python interpreter.

    The result is cached after the first successful (or unsuccessful)
    lookup so repeated calls are free.
    """
    global _cached_executable, _searched
    if _searched:
        return _cached_executable or None

    _searched = True
    embed_dir = get_bundled_python_dir()
    if embed_dir is None:
        logger.debug("No bundled Python directory found")
        _cached_executable = ""
        return None

    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    exe_path = embed_dir / exe_name
    if exe_path.is_file():
        _cached_executable = str(exe_path)
        logger.info("Found bundled Python: %s", _cached_executable)
        return _cached_executable

    logger.debug("Bundled Python exe not found at %s", exe_path)
    _cached_executable = ""
    return None


def get_bundled_site_packages() -> Path | None:
    """Return the ``Lib/site-packages`` inside the bundled Python.

    Returns ``None`` if the bundled Python is absent or pip has never
    installed anything (the directory is created by pip on first use).
    """
    embed_dir = get_bundled_python_dir()
    if embed_dir is None:
        return None
    site_packages = embed_dir / "Lib" / "site-packages"
    return site_packages if site_packages.is_dir() else None


def get_bundled_plugins_path() -> Path | None:
    """Return the ``bundled-plugins`` directory shipped with the package.

    In a PyInstaller *onedir* layout the directory structure is::

        qwenpaw-backend/
        ├── qwenpaw-backend.exe
        ├── _internal/
        ├── python-embed/
        └── bundled-plugins/
            ├── qwenpaw-pet/
            ├── qwen-image-tool/
            └── cloudpaw/

    These are the built-in plugins that ship with each release.  On
    startup the plugin loader compares their versions to the user's
    installed copies and upgrades automatically.

    Returns ``None`` when not in a frozen environment or when the
    directory does not exist.
    """
    if not _is_frozen():
        return None

    meipass = Path(getattr(sys, "_MEIPASS", ""))
    exe_dir = Path(sys.executable).parent

    candidates = [
        exe_dir / "bundled-plugins",
        meipass.parent / "bundled-plugins",
        meipass / "bundled-plugins",
    ]

    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()

    return None
