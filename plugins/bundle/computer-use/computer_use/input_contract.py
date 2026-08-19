# -*- coding: utf-8 -*-
"""Shared invariants for platform-specific Computer Use input contracts."""

from typing import Any, Literal

ClickCount = Literal[1, 2, 3]
# Ten standard Windows wheel detents and one bounded macOS pixel gesture.
# Larger moves remain expressible as fresh-observation scroll actions.
SCROLL_LIMIT = 1200

KEY_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
KEYPAD_DIGITS = tuple(f"NUMPAD{index}" for index in range(10))
KEYPAD_OPERATIONS = (
    "DECIMAL",
    "MULTIPLY",
    "ADD",
    "SUBTRACT",
    "DIVIDE",
)
COMMON_BASE_KEY_NAMES = (
    "ENTER",
    "TAB",
    "ESC",
    "SPACE",
    "BACKSPACE",
    "DELETE",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "HOME",
    "END",
    "PAGEUP",
    "PAGEDOWN",
)
BASE_KEY_ALIASES = {
    "RETURN": "ENTER",
    "ESCAPE": "ESC",
    "DEL": "DELETE",
    "LEFTARROW": "LEFT",
    "RIGHTARROW": "RIGHT",
    "UPARROW": "UP",
    "DOWNARROW": "DOWN",
}


def key_parts(value: Any, maximum: int) -> list[str]:
    """Split and normalize a bounded key chord."""
    if not isinstance(value, str):
        raise ValueError("key must be a string.")
    parts = [part.strip().upper() for part in value.split("+")]
    if not 1 <= len(parts) <= maximum or any(not part for part in parts):
        raise ValueError(
            f"key must contain one to {maximum} non-empty key names.",
        )
    return parts


def describe_keys(names: tuple[str, ...]) -> str:
    """Render canonical names for the model-facing parameter description."""
    return ", ".join(f"``{name}``" for name in names)


def normalize_scroll_delta(value: Any) -> int:
    """Validate the shared signed, positive-down scroll contract."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("scroll requires integer delta_y.")
    if value == 0 or not -SCROLL_LIMIT <= value <= SCROLL_LIMIT:
        raise ValueError(
            f"delta_y must be non-zero and between -{SCROLL_LIMIT} and "
            f"{SCROLL_LIMIT}.",
        )
    return value
