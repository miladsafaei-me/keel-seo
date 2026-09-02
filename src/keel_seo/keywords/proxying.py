"""The seam to keel-crawler's proxy rotation.

Rotating over public proxies is not an SEO capability — it is crawler plumbing,
useful to anything that fetches. It therefore lives in **keel-crawler**
(:mod:`keel_crawler.proxy.pool`), which owns the published-list harvesting, the
durable store and its ageing policy, and the per-address budgets. This module is
only the import seam and the one piece that *is* specific to this package: what
counts as a usable answer from the autocomplete endpoint.

The import is soft. keel-seo does not depend on keel-crawler for anything else,
and a host that never harvests keywords through proxies should not have to
install a crawler to get the Landing registry. Install the extra when the
capability is wanted:

    pip install 'keel-seo[proxies]'

When it is absent, everything except ``--proxies`` keeps working and the CLI
says exactly what to install rather than failing on an AttributeError.
"""
from __future__ import annotations

try:
    from keel_crawler.proxy.pool import (PER_PROXY_PER_HOUR, PER_PROXY_PER_MINUTE,
                                         PER_PROXY_RPS, ProxyPool, fetch_through)

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by hosts without the extra
    AVAILABLE = False
    ProxyPool = None  # type: ignore[assignment]
    fetch_through = None  # type: ignore[assignment]
    # Defaults are mirrored so `--help` can print real numbers, and so the
    # argument parser works, on a host that has not installed the extra.
    PER_PROXY_RPS = 0.2
    PER_PROXY_PER_MINUTE = 10
    PER_PROXY_PER_HOUR = 200

MISSING_MESSAGE = (
    "proxy rotation lives in keel-crawler, which is not installed. "
    "Install it with:  pip install 'keel-seo[proxies]'"
)


def accept_suggestions(status: int, body: str) -> bool:
    """Whether a proxied response is a real autocomplete answer.

    The endpoint-specific half of verification, and the reason keel-crawler takes
    an ``accept`` callback rather than assuming a 200 is enough: a proxy that
    returns a captive-portal page or an interstitial also returns 200, and would
    otherwise be admitted to the pool and then fail every real request.
    """
    return status == 200 and body.lstrip().startswith("[")
