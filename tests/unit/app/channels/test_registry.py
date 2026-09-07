# -*- coding: utf-8 -*-
"""Channel discovery stays complete after incremental startup loading."""
# pylint: disable=protected-access,redefined-outer-name

from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qwenpaw.app.channels import registry
from qwenpaw.app.channels.base import BaseChannel


class StubChannel(BaseChannel):
    """Only used as a registry class, never instantiated."""


@pytest.fixture
def imports(monkeypatch):
    registry.clear_builtin_channel_cache()
    monkeypatch.setattr(registry, "_get_plugin_channels", lambda: {})
    modules = {
        module: SimpleNamespace(**{name: StubChannel})
        for module, name in registry._BUILTIN_SPECS.values()
    }
    importer = Mock(side_effect=lambda module, package: modules[module])
    monkeypatch.setattr(registry.importlib, "import_module", importer)
    yield importer
    registry.clear_builtin_channel_cache()


def test_subset_then_full_discovery_loads_each_builtin_once(imports):
    assert registry.get_channel_registry({"console"}) == {
        "console": StubChannel,
    }
    imports.assert_called_once_with(".console", package=registry.__package__)

    assert registry.get_channel_registry({"slack"}) == {"slack": StubChannel}
    discovered = registry.get_channel_registry()
    assert list(discovered) == list(registry._BUILTIN_SPECS)
    assert Counter(call.args[0] for call in imports.call_args_list) == {
        module: 1 for module, _ in registry._BUILTIN_SPECS.values()
    }
    discovered.clear()
    assert registry.get_channel_registry({"console"})


def test_empty_selection_imports_nothing(imports):
    assert registry.get_channel_registry(set()) == {}
    imports.assert_not_called()


@pytest.mark.parametrize("failure", [ImportError("missing SDK"), TypeError()])
def test_optional_failure_is_cached_until_reset(imports, failure):
    imports.side_effect = failure
    assert registry.get_channel_registry({"slack"}) == {}
    assert registry.get_channel_registry({"slack"}) == {}
    assert imports.call_count == 1

    registry.clear_builtin_channel_cache()
    imports.side_effect = None
    imports.return_value = SimpleNamespace(SlackChannel=StubChannel)
    assert registry.get_channel_registry({"slack"}) == {"slack": StubChannel}
    assert imports.call_count == 2


def test_required_failure_is_raised_and_can_be_retried(imports):
    imports.side_effect = ImportError("console unavailable")
    with pytest.raises(ImportError, match="console unavailable"):
        registry.get_channel_registry({"console"})
    imports.side_effect = None
    imports.return_value = SimpleNamespace(ConsoleChannel=StubChannel)
    assert registry.get_channel_registry({"console"}) == {
        "console": StubChannel,
    }


def test_plugin_selection_and_builtin_collision(imports, monkeypatch, caplog):
    class PluginChannel(StubChannel):
        pass

    monkeypatch.setattr(
        registry,
        "_get_plugin_channels",
        lambda: {"custom": PluginChannel, "console": PluginChannel},
    )
    assert registry.get_channel_registry({"custom"}) == {
        "custom": PluginChannel,
    }
    imports.assert_not_called()
    assert registry.get_channel_registry({"console"}) == {
        "console": StubChannel,
    }
    assert "key already exists" in caplog.text
    assert registry.get_channel_registry()["custom"] is PluginChannel


def test_plugin_can_fill_unavailable_optional_builtin(imports, monkeypatch):
    imports.side_effect = ImportError("missing SDK")
    monkeypatch.setattr(
        registry,
        "_get_plugin_channels",
        lambda: {"slack": StubChannel},
    )
    assert registry.get_channel_registry({"slack"}) == {"slack": StubChannel}
