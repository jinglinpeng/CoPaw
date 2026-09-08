# -*- coding: utf-8 -*-
"""Provider management — models, registry + persistent store."""

from typing import TYPE_CHECKING

from .provider import Provider, ProviderInfo, ModelInfo

if TYPE_CHECKING:
    from .provider_manager import ProviderManager

__all__ = [
    "ModelInfo",
    "Provider",
    "ProviderManager",
    "ProviderInfo",
]


def __getattr__(name: str):
    """Load provider management only when its public entry point is used."""
    if name == "ProviderManager":
        from .provider_manager import ProviderManager

        return ProviderManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
