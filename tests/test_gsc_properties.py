"""Extra Search Console properties on the dashboard (``KEEL_SEO["gsc_properties"]``).

Offline: the live pull is mocked, so nothing here reaches Google.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from keel_seo import config
from keel_seo.gsc import dashboard, views

KIFPOOL = {"site": "https://kifpool.me/", "label": "Kifpool", "default_window": "30d",
           "max_window": "90d", "directory_series": False}
PROPERTIES = {"KEEL_SEO": {"gsc_properties": {"kifpool": KIFPOOL, "blank": {"label": "No site"}}}}


@override_settings(**PROPERTIES)
class PropertyConfigTests(SimpleTestCase):
    def test_a_declared_property_carries_its_key(self):
        prop = config.gsc_property("kifpool")
        self.assertEqual((prop["key"], prop["site"]), ("kifpool", "https://kifpool.me/"))

    def test_an_unknown_or_siteless_property_is_none(self):
        self.assertIsNone(config.gsc_property("nope"))
        self.assertIsNone(config.gsc_property("blank"))

    def test_nothing_is_declared_by_default(self):
        with override_settings(KEEL_SEO={}):
            self.assertIsNone(config.gsc_property("kifpool"))


class LiveTargetTests(SimpleTestCase):
    def test_the_host_dashboard_keeps_its_own_windows(self):
        self.assertEqual(dashboard._live_target(None, None, None)[0], dashboard.DEFAULT_WINDOW)
        self.assertEqual(dashboard._live_target("full", None, None)[0], "full")

    def test_a_property_opens_on_its_default_window(self):
        bounds = dashboard._property_window(KIFPOOL)
        self.assertEqual(dashboard._live_target(None, None, None, **bounds)[0], "30d")

    def test_a_preset_wider_than_max_window_falls_back_to_the_widest_allowed(self):
        bounds = dashboard._property_window(KIFPOOL)
        self.assertEqual(dashboard._live_target("full", None, None, **bounds)[0], "90d")

    def test_a_custom_range_longer_than_max_window_keeps_its_end(self):
        key, start, end = dashboard._live_target(None, "2026-01-01", "2026-09-10", max_days=90)
        self.assertEqual((key, end), ("custom", "2026-09-10"))
        self.assertEqual(dt.date.fromisoformat(end) - dt.date.fromisoformat(start), dt.timedelta(days=89))


@override_settings(**PROPERTIES)
class PropertyContextTests(SimpleTestCase):
    def test_a_failed_pull_never_serves_the_host_snapshot(self):
        with mock.patch("keel_seo.gsc.live.live_enabled", return_value=True), \
                mock.patch("keel_seo.gsc.live.build_range", side_effect=RuntimeError("quota")), \
                mock.patch.object(dashboard, "_load_window") as load_window:
            ctx = dashboard.build_context(prop=config.gsc_property("kifpool"))
        load_window.assert_not_called()
        self.assertFalse(ctx["sc_has_data"])
        self.assertTrue(ctx["sc_live_failed"])

    def test_the_pull_targets_the_property_and_the_picker_stops_at_max_window(self):
        payload = {"meta": {"window": "30d"}, "totals": {"clicks": 1}}
        with mock.patch("keel_seo.gsc.live.live_enabled", return_value=True), \
                mock.patch("keel_seo.gsc.live.build_range", return_value=payload) as build_range:
            ctx = dashboard.build_context(prop=config.gsc_property("kifpool"))
        kwargs = build_range.call_args.kwargs
        self.assertEqual((kwargs["site"], kwargs["directory_series"]), ("https://kifpool.me/", False))
        self.assertEqual([w["key"] for w in ctx["sc_windows"]], ["7d", "30d", "60d", "90d"])
        self.assertEqual(ctx["sc_property"]["key"], "kifpool")
        self.assertIsNone(ctx["sc_coverage"])
        self.assertTrue(all(not cat["items"] for cat in ctx["sc_insights_by_category"]))


@override_settings(**PROPERTIES)
class PropertyViewTests(SimpleTestCase):
    def _request(self, user):
        request = RequestFactory().get("/search-console/nope")
        request.user = user
        return request

    def test_an_unknown_property_is_404_for_a_superuser(self):
        superuser = SimpleNamespace(is_authenticated=True, is_superuser=True)
        with self.assertRaises(Http404):
            views.SearchConsoleView.as_view()(self._request(superuser), property_key="nope")

    def test_an_anonymous_visitor_goes_to_login_before_any_lookup(self):
        response = views.SearchConsoleView.as_view()(self._request(AnonymousUser()), property_key="nope")
        self.assertEqual(response.status_code, 302)

    def test_the_route_sits_under_the_mount_and_never_shadows_an_action(self):
        self.assertEqual(reverse("keel_seo_gsc:search_console_property", args=["kifpool"]),
                         "/search-console/kifpool")
        self.assertEqual(resolve("/search-console/queue").url_name, "queue")

    def test_property_urls_shorten_to_their_own_base(self):
        self.assertEqual(views._property_base("https://kifpool.me/"), "https://kifpool.me")
        self.assertEqual(views._property_base("sc-domain:kifpool.me"), "https://kifpool.me")


class StandaloneStylesTests(SimpleTestCase):
    def test_the_compiled_stylesheet_is_on_by_default_and_ships(self):
        from pathlib import Path

        import keel_seo

        self.assertTrue(config.seo_setting("gsc_standalone_css"))
        sheet = Path(keel_seo.__file__).parent / "static/keel_seo/gsc/search_console.utilities.css"
        self.assertIn("bg-gsc-p1", sheet.read_text())
