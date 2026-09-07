# -*- coding: utf-8 -*-
"""Channel registry: built-in + plugin-registered channels."""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Collection
from typing import TYPE_CHECKING

from .base import BaseChannel

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BUILTIN_SPECS: dict[str, tuple[str, str]] = {
    "imessage": (".imessage", "IMessageChannel"),
    "discord": (".discord_", "DiscordChannel"),
    "dingtalk": (".dingtalk", "DingTalkChannel"),
    "feishu": (".feishu", "FeishuChannel"),
    "qq": (".qq", "QQChannel"),
    "telegram": (".telegram", "TelegramChannel"),
    "mattermost": (".mattermost", "MattermostChannel"),
    "mqtt": (".mqtt", "MQTTChannel"),
    "console": (".console", "ConsoleChannel"),
    "matrix": (".matrix", "MatrixChannel"),
    "slack": (".slack", "SlackChannel"),
    "voice": (".voice", "VoiceChannel"),
    "sip": (".sip", "SIPChannel"),
    "wecom": (".wecom", "WecomChannel"),
    "xiaoyi": (".xiaoyi", "XiaoYiChannel"),
    "yuanbao": (".yuanbao", "YuanbaoChannel"),
    "wechat": (".wechat", "WeChatChannel"),
    "onebot": (".onebot", "OneBotChannel"),
}

# Required channels must load; failures are raised, not skipped.
_REQUIRED_CHANNEL_KEYS: frozenset[str] = frozenset({"console"})

_BUILTIN_CHANNEL_CACHE: dict[str, type[BaseChannel] | None] = {}
_BUILTIN_CHANNEL_CACHE_LOCK = threading.Lock()


def _load_builtin_channels(
    channel_keys: Collection[str],
) -> dict[str, type[BaseChannel] | None]:
    """Load built-in channels safely.

    A single optional dependency failure should not break CLI startup.
    """
    out: dict[str, type[BaseChannel] | None] = {}
    for key, (module_name, class_name) in _BUILTIN_SPECS.items():
        if key not in channel_keys:
            continue
        try:
            mod = importlib.import_module(module_name, package=__package__)
            cls = getattr(mod, class_name)
            if not (
                isinstance(cls, type)
                and issubclass(cls, BaseChannel)
                and cls is not BaseChannel
            ):
                raise TypeError(
                    f"{module_name}.{class_name} is not a BaseChannel subtype",
                )
        except Exception:
            if key in _REQUIRED_CHANNEL_KEYS:
                logger.error(
                    'failed to load required built-in channel "%s"',
                    key,
                    exc_info=True,
                )
                raise
            logger.debug(
                "built-in channel unavailable: %s",
                key,
                exc_info=True,
            )
            out[key] = None
            continue
        out[key] = cls
    return out


def _get_cached_builtin_channels(
    channel_keys: Collection[str] | None = None,
) -> dict[str, type[BaseChannel]]:
    """Load requested built-ins once, preserving registry order."""
    keys = _BUILTIN_SPECS.keys() if channel_keys is None else channel_keys
    with _BUILTIN_CHANNEL_CACHE_LOCK:
        missing = set(keys) - _BUILTIN_CHANNEL_CACHE.keys()
        _BUILTIN_CHANNEL_CACHE.update(_load_builtin_channels(missing))
        return {
            key: channel
            for key in _BUILTIN_SPECS
            if key in keys
            and (channel := _BUILTIN_CHANNEL_CACHE.get(key)) is not None
        }


def clear_builtin_channel_cache() -> None:
    """Reset built-in channel cache. Primarily for tests."""
    with _BUILTIN_CHANNEL_CACHE_LOCK:
        _BUILTIN_CHANNEL_CACHE.clear()


BUILTIN_CHANNEL_KEYS = frozenset(_BUILTIN_SPECS.keys())


def _get_plugin_channels() -> dict[str, type[BaseChannel]]:
    """Return channel classes registered via the plugin system."""
    try:
        from ...plugins.registry import PluginRegistry

        registry = PluginRegistry()
        return {
            key: reg.channel_class
            for key, reg in registry.get_registered_channels().items()
        }
    except ImportError:
        logger.debug("plugin channel discovery skipped (not installed)")
        return {}
    except Exception:
        logger.warning(
            "plugin channel discovery failed",
            exc_info=True,
        )
        return {}


def get_channel_registry(
    channel_keys: Collection[str] | None = None,
) -> dict[str, type[BaseChannel]]:
    """Resolve selected channels, or discover all when no keys are given."""
    out = _get_cached_builtin_channels(channel_keys)
    for key, ch_cls in _get_plugin_channels().items():
        if channel_keys is not None and key not in channel_keys:
            continue
        if key in out:
            logger.warning(
                "Plugin channel '%s' skipped: key already exists in "
                "built-in channels",
                key,
            )
            continue
        out[key] = ch_cls
    return out
