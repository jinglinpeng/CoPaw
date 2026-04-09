# -*- coding: utf-8 -*-
"""Tests for SecretSanitizer."""
import json
import tempfile
from pathlib import Path

import pytest

from copaw.app.snapshot.sanitizer import SecretSanitizer, REDACTED


@pytest.fixture
def workspace_staging():
    """Create a staging dir with agent.json containing secrets."""
    with tempfile.TemporaryDirectory(prefix="test_san_") as d:
        root = Path(d)
        ws = root / "workspaces" / "default"
        ws.mkdir(parents=True)

        config = {
            "name": "test-agent",
            "channels": {
                "dingtalk": {
                    "enabled": True,
                    "client_id": "visible-id",
                    "client_secret": "super-secret-123",
                },
                "discord": {
                    "enabled": True,
                    "bot_token": "discord-token-xyz",
                },
            },
            "system_prompt": "You are helpful",
        }
        (ws / "agent.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8",
        )

        # Also create a secrets dir
        secrets = root / "secrets"
        secrets.mkdir()
        (secrets / "envs.json").write_text(
            json.dumps({"API_KEY": "secret"}), encoding="utf-8",
        )

        yield root


def test_sanitize_workspace(workspace_staging):
    """Secrets in agent.json are redacted."""
    ws = workspace_staging / "workspaces" / "default"
    count = SecretSanitizer.sanitize_workspace(ws)
    assert count == 1

    data = json.loads((ws / "agent.json").read_text(encoding="utf-8"))
    assert data["channels"]["dingtalk"]["client_secret"] == REDACTED
    assert data["channels"]["discord"]["bot_token"] == REDACTED
    # Non-secret values preserved
    assert data["channels"]["dingtalk"]["client_id"] == "visible-id"
    assert data["system_prompt"] == "You are helpful"


def test_sanitize_staging_removes_secrets_dir(workspace_staging):
    """sanitize_staging removes the secrets/ directory."""
    count = SecretSanitizer.sanitize_staging(workspace_staging)
    assert count >= 1
    assert not (workspace_staging / "secrets").is_dir()
