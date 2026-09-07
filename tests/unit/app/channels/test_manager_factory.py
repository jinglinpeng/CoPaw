# -*- coding: utf-8 -*-
"""Configuration factories must not import disabled channel SDKs."""
# pylint: disable=protected-access,redefined-outer-name

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qwenpaw.app.channels import registry
from qwenpaw.app.channels.base import BaseChannel
from qwenpaw.app.channels.manager import ChannelManager
from qwenpaw.config.config import Config


@pytest.fixture(autouse=True)
def isolated_channels(monkeypatch):
    monkeypatch.delenv("QWENPAW_ENABLED_CHANNELS", raising=False)
    monkeypatch.delenv("QWENPAW_DISABLED_CHANNELS", raising=False)
    monkeypatch.setattr(registry, "_get_plugin_channels", lambda: {})
    registry.clear_builtin_channel_cache()
    yield
    registry.clear_builtin_channel_cache()


def test_console_only_skips_disabled_imports(monkeypatch, tmp_path):
    real_import = registry.importlib.import_module
    imported_channels = []

    def import_channel(module, package=None):
        if package == registry.__package__:
            imported_channels.append(module)
            assert module == ".console", "disabled channel was imported"
        return real_import(module, package=package)

    monkeypatch.setattr(registry.importlib, "import_module", import_channel)
    manager = ChannelManager.from_config(
        Mock(), Config(), workspace_dir=tmp_path
    )
    assert [channel.channel for channel in manager.channels] == ["console"]
    assert imported_channels == [".console"]


def test_later_workspace_can_enable_another_channel(monkeypatch):
    class StubChannel(BaseChannel):
        @classmethod
        def from_config(cls, process, config):
            return SimpleNamespace(config=config, process=process)

    importer = Mock(
        return_value=SimpleNamespace(
            ConsoleChannel=StubChannel,
            SlackChannel=StubChannel,
        ),
    )
    monkeypatch.setattr(registry.importlib, "import_module", importer)
    first = ChannelManager.from_config(Mock(), Config())
    assert len(first.channels) == 1
    config = Config()
    config.channels.slack.enabled = True
    second = ChannelManager.from_config(Mock(), config)
    assert len(second.channels) == 2
    assert [call.args[0] for call in importer.call_args_list] == [
        ".console",
        ".slack",
    ]


def test_extra_plugin_config_keeps_defaults_and_factory_arguments(monkeypatch):
    class PluginChannel(BaseChannel):
        @classmethod
        def from_config(cls, process, config):
            return SimpleNamespace(config=config, process=process)

    monkeypatch.setattr(
        registry,
        "_get_plugin_channels",
        lambda: {"custom": PluginChannel},
    )
    config = Config.model_validate(
        {
            "channels": {
                "console": {"enabled": False},
                "custom": {"enabled": True, "custom_setting": "retained"},
            },
        },
    )
    process = Mock()
    manager = ChannelManager.from_config(process, config)
    # Config validation keeps console enabled alongside plugin channels.
    assert len(manager.channels) == 2
    channel = manager.channels[1]
    assert channel.process is process
    assert channel.config.custom_setting == "retained"
    assert channel.config.bot_prefix == ""


def test_no_enabled_channels_skips_all_imports(monkeypatch):
    importer = Mock(side_effect=AssertionError("unexpected channel import"))
    monkeypatch.setattr(registry.importlib, "import_module", importer)
    config = Config()
    config.channels.console.enabled = False
    assert ChannelManager.from_config(Mock(), config).channels == []
    importer.assert_not_called()
