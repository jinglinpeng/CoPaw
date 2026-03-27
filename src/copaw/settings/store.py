# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from ..constant import WORKING_DIR

_SETTINGS_FILE = WORKING_DIR / "settings.json"
_VALID_LANGUAGES = {"en", "zh", "ru", "ja"}
_DEFAULT_LANGUAGE = "en"


class Settings(TypedDict):
    language: str


def _normalize_language(language: str) -> str:
    lang = (language or "").strip().lower()
    if lang not in _VALID_LANGUAGES:
        raise ValueError(
            f"Invalid language '{language}'. Must be one of: "
            f"{', '.join(sorted(_VALID_LANGUAGES))}",
        )
    return lang


def load_settings() -> Settings:
    if not _SETTINGS_FILE.exists():
        return {"language": _DEFAULT_LANGUAGE}

    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"language": _DEFAULT_LANGUAGE}

    language = data.get("language", _DEFAULT_LANGUAGE)
    try:
        language = _normalize_language(language)
    except ValueError:
        language = _DEFAULT_LANGUAGE

    return {"language": language}


def save_settings(settings: Settings) -> None:
    language = _normalize_language(settings.get("language", _DEFAULT_LANGUAGE))
    payload: Settings = {"language": language}

    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = Path(f"{_SETTINGS_FILE}.tmp")
    tmp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_file.replace(_SETTINGS_FILE)


def get_language() -> str:
    return load_settings()["language"]


def set_language(language: str) -> str:
    normalized = _normalize_language(language)
    save_settings({"language": normalized})
    return normalized
