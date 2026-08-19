"""Directory-grouped sitemap index.

Re-groups a flat set of source Sitemaps (keel-seo's ``LandingSitemap`` + keel-cms
content buckets + any host bucket) into **one sitemap per first URL path
segment**, published under a sitemap index at ``/sitemap.xml``::

  /sitemap.xml           sitemap index -> every section sitemap below
  /<segment>.xml         every indexable URL whose path is /<segment>/...
                         (e.g. /academy/foo -> listed in /academy.xml)
  /static-sitemap.xml    root-level pages that live in no routing directory
                         (/, /about, /contact, ...)

A "routing directory" is any first path segment that appears on at least one
multi-segment URL. A single-segment URL (``/about``) whose segment is NOT a
routing directory is a standalone page and lands in the static sitemap; a
single-segment URL whose segment IS a routing directory (``/academy``, the
section root) is grouped into that directory's sitemap alongside its children.

A directory carrying fewer than ``KEEL_SEO["sitemap_min_section_urls"]``
indexable URLs (default 2) gets no section of its own — the index must not link
a sitemap file holding one page. Its URLs move into ``/static-sitemap.xml``, so
they stay in the sitemap; only the section link disappears, and it comes back
by itself once the directory grows past the threshold.

Every section is rebuilt from live source state on each request — the source
Sitemaps read the DB in ``items()``, and each source lists only indexable URLs —
so publishing a page or toggling an indexability flag shows up on the next fetch.
No cron, no cached file, no per-project sitemap code.

Wiring (host ``urls.py``)::

    from keel_seo.sitemap_directory import directory_sitemap_urls

    urlpatterns = [
        *directory_sitemap_urls(SITEMAPS),
        ...
    ]

``SITEMAPS`` is the same flat dict a host already composes for the sitemap view
(``{"landings": LandingSitemap, **content_sitemaps()}``). The two patterns this
returns must be reachable at the site root, so splat them near the top of
``urlpatterns`` (the section pattern matches ``^<name>.xml$`` only, so it never
shadows a real page route, which always has a non-``.xml`` suffix or a path
separator).
"""
from urllib.parse import urlsplit

from django.contrib.sitemaps import Sitemap
from django.contrib.sitemaps import views as sitemap_views
from django.contrib.sites.shortcuts import get_current_site
from django.urls import path, re_path

from .config import seo_setting

# URL name the sitemap index reverses to build each child ``<loc>``. The section
# regex turns the section key into ``<key>.xml`` at the site root.
SECTION_URL_NAME = "keel_sitemap_section"

# Section key (and therefore filename stem) for the root-level static sitemap.
STATIC_SECTION = "static-sitemap"


class _PrebuiltSitemap(Sitemap):
    """A single section holding already-built, absolute URL dicts.

    The Django sitemap/index views drive every section through ``get_urls`` /
    ``get_latest_lastmod`` / ``paginator``; this holds one directory's URLs
    (collected once per request from the real source Sitemaps) and answers those
    without re-hitting the DB.
    """

    def __init__(self, url_dicts):
        self._urls = list(url_dicts)
        lastmods = [u["lastmod"] for u in self._urls if u.get("lastmod")]
        if lastmods:
            # A non-callable ``lastmod`` makes ``get_latest_lastmod()`` (used by
            # the index) return it directly; ``latest_lastmod`` feeds the section
            # response's Last-Modified header.
            self.lastmod = max(lastmods)
            self.latest_lastmod = self.lastmod

    def items(self):
        return self._urls

    def get_urls(self, page=1, site=None, protocol=None, **kwargs):
        return self._urls


def _path_parts(location):
    """Path segments of an absolute sitemap URL (``https://d/a/b`` -> ``[a, b]``)."""
    return [p for p in urlsplit(location).path.split("/") if p]


def _collect(request, source_sitemaps):
    """All URL dicts from every source Sitemap, absolute for this request."""
    req_site = get_current_site(request)
    protocol = request.scheme
    entries = []
    for src in source_sitemaps.values():
        smap = src() if callable(src) else src
        try:
            num_pages = smap.paginator.num_pages
        except Exception:
            num_pages = 1
        for page in range(1, num_pages + 1):
            try:
                entries.extend(
                    smap.get_urls(page=page, site=req_site, protocol=protocol)
                )
            except Exception:
                break
    return entries


def _grouped(request, source_sitemaps):
    """The per-directory sections for this request, ordered deterministically.

    First pass finds the routing directories (segments seen on a multi-segment
    URL); second pass files every URL into its directory section or the static
    section; third pass demotes directories too thin to deserve a section of
    their own. Directory sections come alphabetically, static last.
    """
    entries = _collect(request, source_sitemaps)
    dir_segments = set()
    for entry in entries:
        parts = _path_parts(entry["location"])
        if len(parts) >= 2:
            dir_segments.add(parts[0])
    buckets = {}
    for entry in entries:
        parts = _path_parts(entry["location"])
        key = parts[0] if (parts and parts[0] in dir_segments) else STATIC_SECTION
        buckets.setdefault(key, []).append(entry)
    # A directory holding fewer than ``sitemap_min_section_urls`` indexable URLs
    # is not a section: the index would advertise a sitemap file carrying a
    # single page. Those URLs are demoted into the static sitemap rather than
    # dropped, so nothing leaves the sitemap when a directory empties out.
    minimum = max(1, int(seo_setting("sitemap_min_section_urls")))
    for key in [
        k for k, urls in buckets.items()
        if k != STATIC_SECTION and len(urls) < minimum
    ]:
        buckets.setdefault(STATIC_SECTION, []).extend(buckets.pop(key))
    ordered = {}
    for segment in sorted(k for k in buckets if k != STATIC_SECTION):
        ordered[segment] = _PrebuiltSitemap(buckets[segment])
    if STATIC_SECTION in buckets:
        ordered[STATIC_SECTION] = _PrebuiltSitemap(buckets[STATIC_SECTION])
    return ordered


def make_index_view(source_sitemaps):
    def view(request):
        return sitemap_views.index(
            request,
            _grouped(request, source_sitemaps),
            sitemap_url_name=SECTION_URL_NAME,
        )

    return view


def make_section_view(source_sitemaps):
    def view(request, section):
        return sitemap_views.sitemap(
            request, _grouped(request, source_sitemaps), section=section
        )

    return view


def directory_sitemap_urls(source_sitemaps):
    """URL patterns for the directory-grouped sitemap index + its sections.

    ``source_sitemaps`` is the flat dict of source Sitemap classes/instances a
    host already composes. Splat the returned list into ``urlpatterns``.
    """
    return [
        path(
            "sitemap.xml",
            make_index_view(source_sitemaps),
            name="keel_sitemap_index",
        ),
        re_path(
            r"^(?P<section>[\w-]+)\.xml$",
            make_section_view(source_sitemaps),
            name=SECTION_URL_NAME,
        ),
    ]
