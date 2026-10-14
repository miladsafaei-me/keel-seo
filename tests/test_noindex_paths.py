"""Tests for keel_seo.noindex_paths -- the X-Robots-Tag on sections never advertised.

Run: DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests.test_noindex_paths
"""
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from keel_seo.noindex_paths import HEADER, NoindexPathsMiddleware, is_noindex_path

MIDDLEWARE_WITH_GUARD = [
    "keel_seo.noindex_paths.NoindexPathsMiddleware",
    "django.middleware.common.CommonMiddleware",
]


@override_settings(KEEL_SEO={"noindex_paths": ("/page/", "/members/")})
class IsNoindexPathTests(SimpleTestCase):
    def test_a_path_under_a_prefix_is_named(self):
        self.assertTrue(is_noindex_path("/page/"))
        self.assertTrue(is_noindex_path("/page/anything/deeper"))
        self.assertTrue(is_noindex_path("/members/42/"))

    def test_a_path_outside_every_prefix_is_not(self):
        self.assertFalse(is_noindex_path("/"))
        self.assertFalse(is_noindex_path("/pages/"))
        self.assertFalse(is_noindex_path("/search-console"))


@override_settings(KEEL_SEO={"noindex_paths": ("/page/",)}, MIDDLEWARE=MIDDLEWARE_WITH_GUARD)
class NoindexPathsMiddlewareTests(TestCase):
    """Through the test client: the host app's page view reads the Landing table."""
    def test_a_response_under_the_prefix_carries_the_tag(self):
        response = self.client.get("/page/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[HEADER], "noindex, nofollow")

    def test_a_response_outside_the_prefix_carries_none(self):
        response = self.client.get("/search-console/nothing-here/")
        self.assertFalse(response.has_header(HEADER))

    def test_a_404_under_the_prefix_still_carries_the_tag(self):
        response = self.client.get("/page/nothing-here/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response[HEADER], "noindex, nofollow")

    @override_settings(KEEL_SEO={"noindex_paths": ("/page/",), "noindex_paths_robots": "noindex"})
    def test_the_value_is_configurable(self):
        response = self.client.get("/page/")
        self.assertEqual(response[HEADER], "noindex")

    def test_a_header_an_inner_layer_wrote_is_kept(self):
        def inner(request):
            response = HttpResponse("guarded")
            response[HEADER] = "noindex"
            return response

        middleware = NoindexPathsMiddleware(inner)
        response = middleware(RequestFactory().get("/page/"))
        self.assertEqual(response[HEADER], "noindex")

    @override_settings(KEEL_SEO={})
    def test_off_by_default(self):
        response = self.client.get("/page/")
        self.assertFalse(response.has_header(HEADER))
