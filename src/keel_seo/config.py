"""Host configuration surface for keel-seo.

A consuming project configures keel-seo through a ``KEEL_SEO`` settings dict; every
key is optional and the defaults make the package work standalone:

    KEEL_SEO = {
        # Reuse an existing landing table so adoption needs no data migration
        # (only a metadata-level AlterModelTable). Omit for a fresh project.
        "landing_db_table": "core_landing",
        # Per-path Landing lookup cache TTL (seconds).
        "cache_ttl": 300,
        # Dotted path to a callable returning {url: date} for pages whose content
        # genuinely changes often (so the sitemap emits a real <lastmod> for them
        # and marks them changefreq=hourly). Default: no dynamic-freshness pages.
        "lastmod_hook": "myapp.seo.build_lastmod_map",
    }
"""
from django.conf import settings
from django.utils.module_loading import import_string

_DEFAULTS = {
    "landing_db_table": "keel_seo_landing",
    "cache_ttl": 300,
    "lastmod_hook": None,
}


def seo_setting(key):
    return getattr(settings, "KEEL_SEO", {}).get(key, _DEFAULTS[key])


def landing_lastmod_map():
    """Resolve the optional ``lastmod_hook`` to a ``{url: date}`` map.

    The hook is where the host injects its dynamic-freshness logic (e.g. a
    signals mirror's most-recent-tick date per market URL) — kept out of the
    package so keel-seo stays domain-neutral. Any failure falls back to an empty
    map, i.e. no ``<lastmod>`` rather than a fabricated one.
    """
    dotted = seo_setting("lastmod_hook")
    if not dotted:
        return {}
    try:
        return import_string(dotted)() or {}
    except Exception:
        return {}
