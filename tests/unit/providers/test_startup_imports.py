# -*- coding: utf-8 -*-
"""Fresh-process checks for configuration and provider import boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _run_isolated(code: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    paths = [str(root / "src"), str(root / "packages/qwenpawmail-mcp/src")]
    env = os.environ.copy()
    env["QWENPAW_WORKING_DIR"] = str(tmp_path / "work")
    env["QWENPAW_SECRET_DIR"] = str(tmp_path / "secrets")
    env.pop("LANGFUSE_SECRET_KEY", None)
    prefix = f"import sys\nsys.path[:0] = {paths!r}\n"
    result = subprocess.run(
        [sys.executable, "-I", "-c", prefix + textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("experimental", [True, False])
def test_default_tool_config_does_not_import_workspaces_or_sdks(
    tmp_path: Path,
    experimental: bool,
) -> None:
    _run_isolated(
        f"""
        import json
        import os
        from pathlib import Path

        work = Path(os.environ["QWENPAW_WORKING_DIR"])
        work.mkdir(parents=True)
        (work / "config.json").write_text(
            json.dumps({{"browser": {{"experimental": {experimental!r}}}}}),
            encoding="utf-8",
        )
        from qwenpaw.config import load_config
        from qwenpaw.runtime.tool_registry import get_builtin_tool_funcs

        config = load_config()
        descriptors = {{fn._tool_descriptor.name: fn._tool_descriptor
                       for fn in get_builtin_tool_funcs()}}
        assert "browser" in config.tools.builtin_tools
        assert set(config.tools.builtin_tools) == set(descriptors)
        for name, definition in config.tools.builtin_tools.items():
            descriptor = descriptors[name]
            assert definition.enabled == descriptor.enabled_by_default
            assert definition.async_execution == descriptor.async_execution
            assert definition.description == (
                descriptor.ui.description or descriptor.description or ""
            )
            assert definition.display_to_user == descriptor.ui.display_to_user
        forbidden = {{"qwenpaw.app.multi_agent_manager",
                     "qwenpaw.app.workspace.workspace",
                     "qwenpaw.providers.provider_manager",
                     "qwenpaw.providers.provider_catalog",
                     "openai", "anthropic", "google.genai"}}
        assert not forbidden.intersection(sys.modules), (
            forbidden.intersection(sys.modules)
        )
        """,
        tmp_path,
    )


def test_provider_catalog_and_manager_do_not_load_sdks(tmp_path: Path) -> None:
    _run_isolated(
        """
        import asyncio
        import qwenpaw.providers as providers

        assert "qwenpaw.providers.provider_manager" not in sys.modules
        try:
            providers.missing_export
        except AttributeError:
            pass
        else:
            raise AssertionError("Unknown exports must raise AttributeError")

        from qwenpaw.providers import ProviderManager
        from qwenpaw.providers.provider_manager import (
            ProviderManager as Direct,
        )
        from qwenpaw.providers.provider_catalog import BUILTIN_PROVIDERS

        assert ProviderManager is Direct
        manager = ProviderManager()
        infos = asyncio.run(manager.list_provider_info())
        assert {info.id for info in infos} == {p.id for p in BUILTIN_PROVIDERS}
        for definition in BUILTIN_PROVIDERS:
            instance = manager.get_provider(definition.id)
            assert type(instance) is type(definition)
            assert instance is not definition
        from qwenpaw.providers.model_error_policy import classify_model_error
        assert not classify_model_error(ValueError("unrecognized")).retryable
        import qwenpaw.app._app
        assert not {"openai", "anthropic", "google.genai"}.intersection(
            sys.modules
        )
        """,
        tmp_path,
    )


@pytest.mark.parametrize(
    ("provider_id", "sdk"),
    [
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("gemini", "google.genai"),
    ],
)
def test_first_client_loads_only_its_sdk(
    tmp_path: Path,
    provider_id: str,
    sdk: str,
) -> None:
    _run_isolated(
        f"""
        import asyncio
        from qwenpaw.providers.provider_catalog import BUILTIN_PROVIDERS

        sdks = {{"openai", "anthropic", "google.genai"}}
        assert not sdks.intersection(sys.modules)
        provider = next(
            p for p in BUILTIN_PROVIDERS if p.id == {provider_id!r}
        )
        provider = provider.model_copy(deep=True)
        provider.api_key = "test-key"
        provider.custom_headers = {{"X-Test": "startup"}}
        client = provider._client(timeout=2)
        assert {sdk!r} in sys.modules
        assert not (sdks - {{{sdk!r}}}).intersection(sys.modules)
        if {provider_id!r} == "gemini":
            asyncio.run(client.aio.aclose())
            client.close()
        else:
            asyncio.run(provider._close_client(client))
        """,
        tmp_path,
    )
