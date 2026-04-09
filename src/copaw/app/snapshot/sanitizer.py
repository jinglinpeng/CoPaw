# -*- coding: utf-8 -*-
"""SecretSanitizer: Strip or redact sensitive data from export packages."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

REDACTED = "<REDACTED>"

# Keys in agent.json channel configs that contain secrets
SECRET_KEYS: Set[str] = {
    "client_secret",
    "bot_token",
    "app_secret",
    "encrypt_key",
    "verification_token",
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
}


def _is_secret_key(key: str) -> bool:
    """Check if a config key likely holds a secret value."""
    k = key.lower()
    return any(sk in k for sk in SECRET_KEYS)


def _redact_dict(d: dict) -> dict:
    """Recursively redact secret values in a dict."""
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _redact_dict(v)
        elif isinstance(v, list):
            result[k] = [
                _redact_dict(item) if isinstance(item, dict) else item
                for item in v
            ]
        elif _is_secret_key(k) and isinstance(v, str) and v:
            result[k] = REDACTED
        else:
            result[k] = v
    return result


class SecretSanitizer:
    """Strips sensitive data from snapshot files for safe export."""

    @staticmethod
    def sanitize_workspace(workspace_staging_dir: Path) -> int:
        """Redact secrets in agent.json within a staging directory.

        Returns number of files sanitized.
        """
        count = 0
        agent_json = workspace_staging_dir / "agent.json"
        if agent_json.is_file():
            try:
                with open(agent_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sanitized = _redact_dict(data)
                with open(agent_json, "w", encoding="utf-8") as f:
                    json.dump(sanitized, f, indent=2, ensure_ascii=False)
                count += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to sanitize agent.json: %s", e)

        return count

    @staticmethod
    def sanitize_staging(staging_dir: Path) -> int:
        """Sanitize all workspaces and secrets in a staging directory."""
        count = 0

        # Sanitize each workspace's agent.json
        ws_dir = staging_dir / "workspaces"
        if ws_dir.is_dir():
            for agent_dir in ws_dir.iterdir():
                if agent_dir.is_dir():
                    count += SecretSanitizer.sanitize_workspace(agent_dir)

        # Remove entire secrets directory if present
        secrets_dir = staging_dir / "secrets"
        if secrets_dir.is_dir():
            import shutil
            shutil.rmtree(secrets_dir)
            logger.info("Removed secrets directory from export")
            count += 1

        return count
