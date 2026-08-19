"""Host configuration surface for keel-seo.

A consuming project configures keel-seo through a ``KEEL_SEO`` settings dict; every
key is optional and the defaults make the package work standalone:

    KEEL_SEO = {
        # Reuse an existing landing table so adoption needs no data migration
        # (only a metadata-level AlterModelTable). Omit for a fresh project.
        "landing_db_table": "core_landing",
        # Set True ONLY when adopting a host's pre-existing landing table: the
        # initial migration then records model state without emitting CREATE TABLE.
        # Default False → a fresh project's initial migration creates the table.
        "adopt_existing": True,
        # Per-path Landing lookup cache TTL (seconds).
        "cache_ttl": 300,
        # Dotted path to a callable returning {url: date} for pages whose content
        # genuinely changes often (so the sitemap emits a real <lastmod> for them
        # and marks them changefreq=hourly). Default: no dynamic-freshness pages.
        "lastmod_hook": "myapp.seo.build_lastmod_map",
        # Smallest number of URLs a routing directory needs before the directory
        # sitemap index links its own /<segment>.xml. A thinner directory keeps
        # its URLs (they move into /static-sitemap.xml) but earns no section file.
        # Default 2 — a one-page "directory" is not a section.
        "sitemap_min_section_urls": 2,

        # ---- keel_seo.gsc dashboard (the /search-console UI, keel_seo.gsc.urls) ----
        # Directory holding the committed exporter output: gsc_dashboard.json,
        # gsc_insights.json, query_enrichment.json, windows/*.json. Default (unset):
        # <MEDIA_ROOT>/gsc/data, which works but is normally overridden to point at
        # the host's own exporter output directory (e.g. wherever
        # tools/gsc/export_dashboard.py already writes).
        "gsc_data_dir": "/app/backend/admin_os/data/gsc",
        # Template the dashboard extends for its chrome (title/extra_head/content/
        # extra_js blocks). Default is keel-seo's own bare fallback so the package
        # renders standalone; a host with its own admin chrome overrides this.
        "gsc_base_template": "admin_os/base_admin.html",
        # Dotted path to a callable `(spec: dict, *, source_type: str, source_ref:
        # str) -> (obj, outcome)` that deposits a curated GSC insight into the
        # host's content-ideation queue (the dashboard's "Add to Plan" button on an
        # insight card). Host business logic — no default, the button errors until set.
        "gsc_queue_hook": "content_pipeline.keel_adapter.upsert_content_plan_spec",
        # URL name (reverse()-able, no args) of the host's clustering-queue LIST
        # view — used for "already queued" links after a pick/cluster is queued.
        "gsc_queue_list_url_name": "admin_os:cluster_queue_list",
        # URL name (reverse()-able with args=[pk]) of the host's content-plan EDIT
        # view — the link returned after "Add to Plan" queues an insight.
        "gsc_plan_edit_url_name": "admin_os:content_plan_brief",
        # URL name an authenticated-but-not-superuser visitor is redirected to.
        # Default None raises Django's standard PermissionDenied (403) instead.
        "gsc_forbidden_redirect": "core:home",
    }
"""
from django.conf import settings
from django.urls import reverse
from django.utils.module_loading import import_string

_DEFAULTS = {
    "landing_db_table": "keel_seo_landing",
    "adopt_existing": False,
    "cache_ttl": 300,
    "lastmod_hook": None,
    "sitemap_min_section_urls": 2,
    "gsc_data_dir": None,
    "gsc_base_template": "keel_seo/gsc/_default_base.html",
    "gsc_queue_hook": None,
    "gsc_queue_list_url_name": None,
    "gsc_plan_edit_url_name": None,
    "gsc_forbidden_redirect": None,
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


def gsc_data_dir():
    """Directory holding the committed GSC exporter output (see the ``gsc_data_dir``
    key above). Falls back to ``<MEDIA_ROOT>/gsc/data`` when unset — the dashboard
    then simply has nothing to read and renders its "no data yet" state rather than
    raising, so an unconfigured host still boots."""
    from pathlib import Path

    raw = seo_setting("gsc_data_dir")
    if raw:
        return Path(raw).expanduser()
    return Path(settings.MEDIA_ROOT) / "gsc" / "data"


def gsc_queue_list_url() -> str:
    """Reverse ``gsc_queue_list_url_name`` (the host's clustering-queue list view) —
    used for the dashboard's "already queued" / "Added to Plan" links. Empty string
    when unset or the name does not resolve, so the dashboard renders the flag
    without a link rather than erroring."""
    name = seo_setting("gsc_queue_list_url_name")
    if not name:
        return ""
    try:
        return reverse(name)
    except Exception:
        return ""
