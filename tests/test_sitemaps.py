"""Tests for LandingSitemap.lastmod falling back to content_modified_at.

Run: DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests.test_sitemaps
"""
import datetime as dt

from django.test import TestCase

from keel_seo.freshness import record
from keel_seo.models import Landing
from keel_seo.sitemaps import LandingSitemap


def _fake_lastmod_hook():
    # Keys match the sitemap's own lookup convention: `url.rstrip("/")`, so a
    # non-root URL's key carries no trailing slash (see LandingSitemap.lastmod).
    return {"/hooked": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)}


class LandingSitemapLastmodTests(TestCase):
    def setUp(self):
        self.sitemap = LandingSitemap()

    def test_falls_back_to_content_modified_at_when_hook_has_nothing(self):
        landing = Landing.objects.create(url="/plain/", title="Plain", is_indexable=True)

        self.assertIsNone(self.sitemap.lastmod(landing))  # no hook, no recorded content yet

        record(landing.url, "<main><p>Body</p></main>")
        landing.refresh_from_db()
        self.sitemap._lm_cache = None
        self.assertEqual(self.sitemap.lastmod(landing), landing.content_modified_at)

    def test_hook_wins_over_content_modified_at_when_it_answers(self):
        landing = Landing.objects.create(url="/hooked/", title="Hooked", is_indexable=True)
        record(landing.url, "<main><p>Body</p></main>")
        landing.refresh_from_db()

        with self.settings(KEEL_SEO={
            "freshness_enabled": True,
            "lastmod_hook": "tests.test_sitemaps._fake_lastmod_hook",
        }):
            self.sitemap._lm_cache = None
            resolved = self.sitemap.lastmod(landing)
            self.assertEqual(resolved, dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
            self.assertNotEqual(resolved, landing.content_modified_at)

    def test_none_when_neither_source_has_a_date(self):
        landing = Landing.objects.create(url="/bare/", title="Bare", is_indexable=True)
        self.assertIsNone(self.sitemap.lastmod(landing))
