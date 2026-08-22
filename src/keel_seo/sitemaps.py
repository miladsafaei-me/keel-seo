"""Sitemap for the Landing registry.

Lists only ``is_indexable=True`` rows, so the sitemap never disagrees with a
page's robots meta. URL ordering is deterministic by structural section:

  1. home     — exactly ``/``
  2. company  — a single-segment page (``/about``, ``/contact``, ...)
  3. listing  — a single-segment URL that is the parent of one or more sub URLs
  4. sub      — a multi-segment URL (``/trading-bots/pocket-option``)

Within a section, alphabetical by URL. Regeneration is automatic: each request
reads current DB state, so toggling a row's flag is reflected on the next fetch.

Content sitemaps (blog/news/archives) are NOT here — they belong to the content
package; a host composes its ``sitemaps`` dict from ``LandingSitemap`` plus those.
"""
from django.contrib.sitemaps import Sitemap

from .config import landing_lastmod_map
from .models import Landing


def section_key(url: str, all_urls: set) -> tuple:
    if url == "/":
        return (0, url)
    base = url.rstrip("/")
    parts = [p for p in base.strip("/").split("/") if p]
    if len(parts) >= 2:
        return (3, url)
    prefix = base + "/"
    is_listing = any(
        other != url and other.rstrip("/").startswith(prefix)
        for other in all_urls
    )
    return (2 if is_listing else 1, url)


class LandingSitemap(Sitemap):
    """Indexable landing pages, sourced from the Landing registry."""

    priority = 0.6

    def items(self):
        qs = list(Landing.objects.filter(is_indexable=True))
        all_urls = {l.url for l in qs}
        qs.sort(key=lambda l: section_key(l.url, all_urls))
        return qs

    def location(self, obj: Landing) -> str:
        return "" if obj.url == "/" else obj.url

    def changefreq(self, obj: Landing) -> str:
        # Pages the host reports in the lastmod map re-render as their underlying
        # data changes, so they churn far more often than static marketing pages.
        return "hourly" if (obj.url.rstrip("/") or "/") in self._lastmod_map() else "monthly"

    def lastmod(self, obj: Landing):
        # The host's dynamic-freshness hook wins when it answers for this URL
        # (it's the documented override and some hosts compute a better date);
        # otherwise fall back to the content-freshness engine's real
        # content_modified_at (keel_seo.freshness). ``None`` (tag omitted)
        # only when neither has anything — no fabricated freshness.
        hook_date = self._lastmod_map().get(obj.url.rstrip("/") or "/")
        if hook_date is not None:
            return hook_date
        return obj.content_modified_at

    def _lastmod_map(self) -> dict:
        cache = getattr(self, "_lm_cache", None)
        if cache is None:
            cache = self._lm_cache = landing_lastmod_map()
        return cache
