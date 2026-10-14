"""Sections a site serves but never advertises, stated by header on every response.

**The problem this exists for.** The robots meta a page emits is decided per URL by the
Landing gate (``keel_seo/robots_meta.html``): a URL with no indexable Landing row is
``noindex`` by default. That holds for a rendered page. A section a site keeps public
without ever wanting it indexed -- a forum, a member area, a search page, an upload
endpoint -- also answers with bodies that carry no ``<head>`` at all: an RSS feed, a JSON
reply, a redirect, a 404 page from a third-party app whose templates never included the
partial. For those the meta says nothing, and a crawler that reaches them reads nothing.

``X-Robots-Tag`` is the same statement made on the response rather than in the body, so it
holds whatever the body is. This middleware sets it on every response whose path starts
with one of the configured prefixes.

**What it does.** For a request whose path starts with a prefix in
``KEEL_SEO["noindex_paths"]`` (default: none), the response leaves with
``X-Robots-Tag: <KEEL_SEO["noindex_paths_robots"]>`` (default ``noindex, nofollow``). A
response that already carries an ``X-Robots-Tag`` keeps it: an inner guard that has
spoken (``keel_web.api_guard``, a view's own header) is not overruled, and every value a
guard writes is itself a noindex.

**What it is not.** It does not change the robots meta a page renders, so a page under a
prefix still emits whatever the Landing gate decides; the two agree because a section
named here has no indexable Landing row. It is not access control: the section stays
reachable. It is not ``robots.txt``: a crawler may still fetch the URL, and reads on the
answer that it is not to be indexed or followed.

**Position.** Near the top of ``MIDDLEWARE``, above every middleware that writes
headers, so the tag is the last word on the way out. It never short-circuits a request.

**Configuration** -- ``KEEL_SEO["noindex_paths"]`` and ``KEEL_SEO["noindex_paths_robots"]``;
see ``keel_seo/config.py``.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse

from .config import seo_setting

HEADER = "X-Robots-Tag"


def is_noindex_path(path: str) -> bool:
    """Whether ``path`` falls under a prefix the host never advertises."""
    return any(path.startswith(prefix) for prefix in seo_setting("noindex_paths"))


class NoindexPathsMiddleware:
    """Stamp every response under a configured prefix with a noindex ``X-Robots-Tag``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if is_noindex_path(request.path) and not response.has_header(HEADER):
            response[HEADER] = seo_setting("noindex_paths_robots")
        return response
