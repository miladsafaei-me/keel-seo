"""The seam to keel-crawler's proxy rotation.

Rotating over public proxies is not an SEO capability — it is crawler plumbing,
useful to anything that fetches. It therefore lives in **keel-crawler**
(:mod:`keel_crawler.proxy.pool`), which owns the published-list harvesting, the
durable store and its ageing policy, and the per-address budgets. This module is
only the import seam and the one piece that *is* specific to this package: what
counts as a usable answer from the autocomplete endpoint.

Nothing here re-states a limit that keel-crawler owns. The per-address budgets
are imported or they are ``None``; they are never copied, because a copy is a
second source of truth that drifts without anything failing.

The import is soft. keel-seo does not depend on keel-crawler for anything else,
and a host that never harvests keywords through proxies should not have to
install a crawler to get the Landing registry. Install the extra when the
capability is wanted:

    pip install 'keel-seo[proxies]'

When it is absent, everything except ``--proxies`` keeps working and the CLI
says exactly what to install rather than failing on an AttributeError.

It also owns the package's **egress policy** — a harvest never leaves from the
machine running it — stated once in :data:`DIRECT_REFUSAL` and enforced by
:func:`require_pooled_egress` at every entry point that crawls.
"""
from __future__ import annotations

try:
    from keel_crawler.proxy.jsonstore import harvest_lock
    from keel_crawler.proxy.pool import (PER_PROXY_PER_HOUR, PER_PROXY_PER_MINUTE,
                                         PER_PROXY_RPS, ProxyPool, fetch_through)
    from keel_crawler.proxy.sources import normalize_country

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by hosts without the extra
    AVAILABLE = False
    ProxyPool = None  # type: ignore[assignment]
    fetch_through = None  # type: ignore[assignment]
    harvest_lock = None  # type: ignore[assignment]
    # None, not a copy of the numbers. This used to mirror them so `--help`
    # could print real values, and the mirror then sat at 0.2/10/200 for a week
    # after keel-crawler measured its way to 1.5/90/1500 - two sources of truth
    # for one limit, one of them silently wrong, in the exact file that explains
    # why the limit matters. Where the extra is absent there are no limits to
    # print, because there is no rotation to limit.
    PER_PROXY_RPS = None
    PER_PROXY_PER_MINUTE = None
    PER_PROXY_PER_HOUR = None

    def normalize_country(label: str) -> str:  # noqa: D103 - trivial stand-in
        label = (label or "").strip()
        return label.upper() if len(label) == 2 and label.isalpha() else ""

MISSING_MESSAGE = (
    "proxy rotation lives in keel-crawler, which is not installed. "
    "Install it with:  pip install 'keel-seo[proxies]'"
)

# The egress policy, and it is a rule rather than a default.
#
# A harvest is never asked from the address running it — not a laptop, and least
# of all a production host. The endpoint's refusal is IP-wide and outlasts the
# run, so the machine that trips it loses the endpoint entirely: every other
# query from that address is refused too, for over an hour, whoever asked it.
# Throttling does not buy immunity. On 2026-09-04 a harvest asking directly at
# this package's own default of 6 q/s was refused after 3,909 network calls, on
# a server shared by six production sites, and left the seed it was collecting
# two-thirds unexpanded. That is the whole case: the direct path is not a slower
# route to the same universe, it is the route that ends the run early and takes
# the address down with it.
#
# There is deliberately no override. An escape hatch here is a rule that holds
# until someone is in a hurry, which is precisely when this block is earned.
DIRECT_REFUSAL = (
    "refusing to crawl from this machine's own address. The endpoint's block is "
    "IP-wide and outlasts the run — measured 2026-09-04: refused after 3,909 "
    "requests at the default 6 q/s, taking the whole host's access with it. "
    "Harvest through the rotating pool instead: --proxies auto "
    "(pip install 'keel-seo[proxies]')."
)


class DirectEgressRefused(RuntimeError):
    """Raised when a caller asks to crawl from the local address."""


def require_pooled_egress(mode: str) -> str:
    """Return the egress mode to use, or raise if it would be this machine.

    One gate for every entry point, so a second CLI cannot quietly reopen the
    path the first one closed — which is exactly how this got shipped: the batch
    walker had defaulted to ``auto`` for months while the single-seed CLI still
    defaulted to ``off``, and the harvest that got blocked went through the
    second one.
    """
    if (mode or "").strip().lower() in ("", "off", "none", "direct"):
        raise DirectEgressRefused(DIRECT_REFUSAL)
    return mode


def accept_suggestions(status: int, body: str) -> bool:
    """Whether a proxied response is a real autocomplete answer.

    The endpoint-specific half of verification, and the reason keel-crawler takes
    an ``accept`` callback rather than assuming a 200 is enough: a proxy that
    returns a captive-portal page or an interstitial also returns 200, and would
    otherwise be admitted to the pool and then fail every real request.
    """
    return status == 200 and body.lstrip().startswith("[")
