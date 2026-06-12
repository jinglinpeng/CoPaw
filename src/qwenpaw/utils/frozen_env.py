# -*- coding: utf-8 -*-
"""Frozen environment detection utilities.

Centralises the check for PyInstaller / Tauri frozen bundles so that
every module uses the same logic and the pattern is maintained in one
place.
"""
import sys


def is_frozen_environment() -> bool:
    """Return True when running inside a PyInstaller / frozen bundle.

    In a frozen environment ``sys.executable`` points to the bundled
    exe rather than a regular Python interpreter.  ``sys.frozen`` is
    set by PyInstaller, and ``sys._MEIPASS`` is the temporary
    extraction directory.
    """
    return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")
