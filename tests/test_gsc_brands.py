"""Brand share and keyword clusters follow each property's own market.

The SignalBots family's binary-options brand list used to be built in, so every
property, a Persian encyclopedia included, was split into Quotex, Pocket Option and
the rest. Now the host keeps that list as its default and a declared property gets
only the brands it names.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from keel_seo.gsc import build, dashboard


def _q(clicks, impressions=100):
    return {"clicks": clicks, "impressions": impressions, "ctr": clicks / impressions,
            "position": 5.0}


QUERIES = {"quotex signal": _q(5), "ویکی لیکس": _q(3), "تاریخ ایران": _q(2)}


class BrandShareTests(SimpleTestCase):
    def test_the_host_default_is_the_binary_options_list(self):
        share = build._brand_share(QUERIES, build.brand_rules(build.HOST_DEFAULT_BRANDS))
        self.assertEqual(share["clicks"][0]["brand"], "Quotex")

    def test_no_brand_list_gives_no_panel(self):
        self.assertEqual(build._brand_share(QUERIES, build.brand_rules(None)), {})

    def test_a_list_that_matches_nothing_gives_no_panel(self):
        rules = build.brand_rules([("Kifpool", r"kifpool")])
        self.assertEqual(build._brand_share(QUERIES, rules), {})

    def test_a_property_list_splits_on_its_own_brand(self):
        rules = build.brand_rules([("Wikilix", r"wikilix|ویکی ?لیکس")])
        share = build._brand_share(QUERIES, rules)
        self.assertEqual([s["brand"] for s in share["clicks"]], ["Wikilix", "Other"])


class ClusterTests(SimpleTestCase):
    def test_persian_queries_get_a_subject_cluster(self):
        clusters = build.cluster_keywords(["تاریخ ایران", "دانلود تاریخ ایران"],
                                          build.CLUSTER_QUALIFIERS_GENERIC)
        self.assertEqual(clusters["تاریخ ایران"], clusters["دانلود تاریخ ایران"])
        self.assertNotEqual(clusters["تاریخ ایران"], "other")

    def test_the_host_default_keeps_the_signalbots_words(self):
        self.assertTrue({"signal", "bot", "trading"} <= build.HOST_DEFAULT_QUALIFIERS)
        self.assertFalse({"signal", "bot", "trading"} & build.CLUSTER_QUALIFIERS_GENERIC)


WIKI = {"site": "sc-domain:wikilix.com", "label": "Wikilix"}


class PropertyPassesItsOwnMarketTests(SimpleTestCase):
    def _call(self, prop):
        with mock.patch("keel_seo.gsc.live.live_enabled", return_value=True), \
             mock.patch("keel_seo.gsc.live.build_range", return_value={"x": 1}) as br:
            dashboard._resolve_payload(window="28d", prop=prop)
        return br.call_args.kwargs

    def test_a_declared_property_gets_no_host_brands(self):
        kw = self._call(WIKI)
        self.assertIsNone(kw["brands"])
        self.assertEqual(kw["cluster_qualifiers"], build.CLUSTER_QUALIFIERS_GENERIC)

    def test_a_declared_property_names_its_own_brands(self):
        kw = self._call({**WIKI, "brands": [("Wikilix", "wikilix")]})
        self.assertEqual(kw["brands"], [("Wikilix", "wikilix")])

    @override_settings(KEEL_SEO={"gsc_site": "https://signalbots.ai/"})
    def test_the_host_property_keeps_the_binary_list(self):
        self.assertEqual(self._call(None)["brands"], "binary_options")


class MarketPanelTests(SimpleTestCase):
    def _markets(self, by_market):
        data = {"by_market": by_market}
        with mock.patch.object(dashboard, "_resolve_payload", return_value=(data, "28d", None, False)):
            return dashboard.build_context(window="28d", prop=WIKI)["sc_by_market"]

    def test_only_cross_market_rows_hide_the_panel(self):
        self.assertEqual(self._markets([{"market": "cross-market", "clicks": 9}]), [])

    def test_real_markets_keep_the_panel(self):
        rows = self._markets([{"market": "quotex", "clicks": 9}, {"market": "cross-market", "clicks": 1}])
        self.assertEqual(len(rows), 2)
