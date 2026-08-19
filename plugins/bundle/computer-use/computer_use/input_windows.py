# -*- coding: utf-8 -*-
"""Windows model-facing input contract."""

from typing import Any, Literal, get_args

from .input_contract import (
    BASE_KEY_ALIASES,
    COMMON_BASE_KEY_NAMES,
    KEYPAD_DIGITS,
    KEYPAD_OPERATIONS,
    KEY_CHARACTERS,
    SCROLL_LIMIT,
    describe_keys,
    key_parts,
)

MouseButton = Literal["left", "right", "middle"]
MOUSE_BUTTONS = frozenset(get_args(MouseButton))

_SPECIAL_KEY_NAMES = (
    "CTRL",
    "ALT",
    "SHIFT",
    "WIN",
    "RWIN",
    "INSERT",
    "CAPSLOCK",
    "NUMLOCK",
    "SCROLLLOCK",
    "PRINTSCREEN",
    "PAUSE",
    "APPS",
)
_FUNCTION_KEYS = tuple(f"F{index}" for index in range(1, 25))
_CANONICAL_NAMED_KEYS = (
    *_SPECIAL_KEY_NAMES,
    *COMMON_BASE_KEY_NAMES,
    *KEYPAD_OPERATIONS,
)
_KEYS = (
    KEY_CHARACTERS
    | frozenset(_CANONICAL_NAMED_KEYS)
    | frozenset(_FUNCTION_KEYS)
    | frozenset(KEYPAD_DIGITS)
)
_ALIASES = {
    **BASE_KEY_ALIASES,
    "CONTROL": "CTRL",
    "SUPER": "WIN",
    "META": "WIN",
    "LWIN": "WIN",
    "INS": "INSERT",
    "PGUP": "PAGEUP",
    "PGDN": "PAGEDOWN",
    "PRTSC": "PRINTSCREEN",
    "BREAK": "PAUSE",
    "MENU": "APPS",
    "CONTEXTMENU": "APPS",
}

KEY_DESCRIPTION = (
    "One to four Windows key names joined by ``+``. Use A-Z, 0-9, "
    f"{describe_keys(_CANONICAL_NAMED_KEYS)}, F1-F24, or "
    "NUMPAD0-NUMPAD9."
)
SCROLL_DESCRIPTION = (
    f"A non-zero Windows scroll amount from -{SCROLL_LIMIT} through "
    f"{SCROLL_LIMIT}, measured in wheel-delta units (120 is one standard "
    "detent). Positive scrolls down; negative scrolls up."
)


def normalize_key(value: Any) -> str:
    """Normalize one chord against the Windows native key vocabulary."""
    parts = [_ALIASES.get(part, part) for part in key_parts(value, 4)]
    unsupported = next((part for part in parts if part not in _KEYS), None)
    if unsupported:
        raise ValueError(f"Unsupported Windows key: {unsupported}.")
    return "+".join(parts)
