# -*- coding: utf-8 -*-
from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_url(url: str) -> bool:
    """Return True when *url* targets a loopback / localhost address.

    On Windows, httpx reads the system proxy from the registry
    (HKCU\\...\\Internet Settings) but does not parse ProxyOverride bypass
    rules.  Requests to loopback addresses therefore get forwarded to the
    system proxy and fail with HTTP 502.  Callers should set
    trust_env=False when this function returns True.
    """
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS
