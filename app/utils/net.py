"""HTTPS helper: urllib with the system certificates *plus* certifi's bundle.

Adding certifi's bundle (shipped with the app) avoids CERTIFICATE_VERIFY_FAILED on PCs with an incomplete certificate
store, without turning verification off.
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


def github_latest_tag(owner_repo: str, timeout: float = 8):
    """(tag, release page url) of a repo's latest release WITHOUT the REST API.

    The API allows only 60 requests per hour per IP address (shared by everyone behind the same ISP/NAT), after which
    it answers 403. github.com/<repo>/releases/latest simply redirects to .../releases/tag/<tag> and is not limited.
    """
    import re
    req = urllib.request.Request(f"https://github.com/{owner_repo.strip('/')}/releases/latest",
                                 headers={"User-Agent": "UniversalVideoStudio"})
    with urlopen(req, timeout=timeout) as r:
        final = r.geturl()
    m = re.search(r"/releases/tag/([^/?#]+)$", final)
    return (m.group(1), final) if m else (None, final)
