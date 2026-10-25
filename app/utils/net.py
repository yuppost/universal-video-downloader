"""HTTPS helper: urllib with the system certificates *plus* certifi's bundle.

A PyInstaller build on some Linux distributions cannot find the system CA folder, which makes every HTTPS download
fail with CERTIFICATE_VERIFY_FAILED. Adding certifi's bundle (shipped with the app) fixes that without turning
verification off.
"""

from __future__ import annotations

import ssl
import urllib.request
from typing import Optional

_ctx: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    global _ctx
    if _ctx is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - certifi optional
            pass
        _ctx = ctx
    return _ctx


def urlopen(req, timeout: float = 30):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())  # noqa: S310 - callers check the scheme
