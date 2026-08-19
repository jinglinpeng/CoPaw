# -*- coding: utf-8 -*-
"""macOS model-facing input contract."""

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

MouseButton = Literal["left", "right"]
MOUSE_BUTTONS = frozenset(get_args(MouseButton))

_MODIFIER_NAMES = ("CMD", "CTRL", "SHIFT", "OPTION")
_MODIFIERS = frozenset(_MODIFIER_NAMES)
_BASE_NAMED_KEYS = (
    *COMMON_BASE_KEY_NAMES,
    "HELP",
    *KEYPAD_OPERATIONS,
)
_FUNCTION_KEYS = tuple(f"F{index}" for index in range(1, 21))
_BASE_KEYS = (
    KEY_CHARACTERS
    | frozenset(_BASE_NAMED_KEYS)
    | frozenset(_FUNCTION_KEYS)
    | frozenset(KEYPAD_DIGITS)
)
_MODIFIER_ALIASES = {
    "COMMAND": "CMD",
    "META": "CMD",
    "SUPER": "CMD",
    "WIN": "CMD",
    "CONTROL": "CTRL",
    "ALT": "OPTION",
    "OPT": "OPTION",
}

KEY_DESCRIPTION = (
    "One macOS base key, optionally preceded by distinct "
    f"{describe_keys(_MODIFIER_NAMES)} modifiers joined by ``+``. "
    f"Base keys are A-Z, 0-9, {describe_keys(_BASE_NAMED_KEYS)}, "
    "F1-F20, or NUMPAD0-NUMPAD9."
)
SCROLL_DESCRIPTION = (
    f"A non-zero macOS pixel distance from -{SCROLL_LIMIT} through "
    f"{SCROLL_LIMIT}. Positive scrolls down; negative scrolls up."
)


def normalize_key(value: Any) -> str:
    """Normalize one chord against the macOS native key vocabulary."""
    parts = key_parts(value, 5)
    modifiers = [_MODIFIER_ALIASES.get(part, part) for part in parts[:-1]]
    base = BASE_KEY_ALIASES.get(parts[-1], parts[-1])
    if base in _MODIFIERS:
        raise ValueError("macOS key chord must end with a non-modifier key.")
    if any(modifier not in _MODIFIERS for modifier in modifiers):
        raise ValueError(
            "macOS modifiers must use CMD, CTRL, SHIFT, or OPTION and "
            "precede the base key.",
        )
    if len(modifiers) != len(set(modifiers)):
        raise ValueError("macOS key chord cannot repeat a modifier.")
    if base not in _BASE_KEYS:
        raise ValueError(f"Unsupported macOS key: {base}.")
    return "+".join([*modifiers, base])
