"""The Search Console report's pure transforms (keel_seo.gsc.report)."""
import datetime as dt

from django.test import SimpleTestCase, TestCase

from keel_seo.gsc import report as R

TODAY = dt.date(2026, 11, 19)
LAST = dt.date(2026, 11, 16)


def day_rows(start, values, metric="impressions"):
    return [{"keys": [(start + dt.timedelta(days=i)).isoformat()], "clicks": v // 10 if metric == "impressions" else v,
             "impressions": v, "position": 5.0} for i, v in enumerate(values)]


class RangeTests(SimpleTestCase):
    def test_presets_end_on_the_last_complete_day(self):
        r = R.resolve_range("28d", LAST, TODAY)
        self.assertEqual((r["start"], r["end"], r["days"]), (dt.date(2026, 10, 20), LAST, 28))
        r = R.resolve_range("3m", LAST, TODAY)
        self.assertEqual(r["start"], dt.date(2026, 8, 17))

    def test_the_comparison_has_exactly_the_same_length(self):
        for key in ("7d", "28d", "3m", "6m"):
            cur = R.resolve_range(key, LAST, TODAY)
            for mode in ("previous", "year"):
                comp = R.resolve_compare(mode, cur, TODAY)
                if comp["available"]:
                    self.assertEqual((comp["end"] - comp["start"]).days + 1, cur["days"], (key, mode))
                    self.assertLess(comp["end"], cur["start"])

    def test_no_comparison_beyond_sixteen_months(self):
        cur = R.resolve_range("12m", LAST, TODAY)
        self.assertFalse(R.resolve_compare("previous", cur, TODAY)["available"])
        self.assertIsNone(R.resolve_compare("none", cur, TODAY))

    def test_custom_range_is_clamped(self):
        r = R.resolve_range("custom", LAST, TODAY, "2026-11-01", "2026-11-30")
        self.assertEqual(r["end"], LAST)
        r = R.resolve_range("custom", LAST, TODAY, "2020-01-01", "2026-01-31")
        self.assertEqual(r["start"], R.earliest_date(TODAY))


class FigureTests(SimpleTestCase):
    def test_totals_weight_position_by_impressions(self):
        t = R.totals_of([{"clicks": 1, "impressions": 100, "position": 2.0},
                         {"clicks": 0, "impressions": 300, "position": 10.0}])
        self.assertEqual(t, {"clicks": 1, "impressions": 400, "ctr": 0.25, "position": 8.0})

    def test_coverage_and_hidden_brand_share(self):
        totals = {"clicks": 100, "impressions": 1000}
        rows = [{"keys": ["acme login"], "clicks": 40, "impressions": 300, "position": 1.0},
                {"keys": ["best widgets"], "clicks": 20, "impressions": 400, "position": 8.0}]
        cov = R.coverage(rows, totals)
        self.assertEqual((cov["clicks_pct"], cov["impressions_pct"]), (60.0, 70.0))
        split = {r["key"]: r for r in R.brand_split(rows, totals, R.brand_rules([("Acme", "acme")]))["rows"]}
        self.assertEqual((split["brand"]["clicks"], split["nonbrand"]["clicks"], split["hidden"]["clicks"]), (40, 20, 40))
        self.assertEqual(split["hidden"]["clicks_pct"], 40.0)

    def test_change_units(self):
        self.assertEqual(R.change(50, 100, "count")["value"], -50.0)
        self.assertEqual(R.change(10.0, 12.5, "position")["value"], 2.5)
        self.assertTrue(R.change(10.0, 12.5, "position")["good"])
        self.assertEqual(R.change(3, 0, "count")["unit"], "new")
        self.assertTrue(R.change(0, 0, "count")["flat"])

    def test_compact_matches_search_console(self):
        self.assertEqual([R.compact(v) for v in (67, 1727, 15803, 348106319)], ["67", "1.73K", "15.8K", "348M"])
        self.assertEqual([R.compact(v) for v in (100000, 150000, 2000, 10500)], ["100K", "150K", "2K", "10.5K"])
        self.assertEqual((R.compact(3.88, "ctr"), R.compact(15.84, "position")), ("3.9%", "15.8"))


class SeriesTests(SimpleTestCase):
    def test_comparison_days_line_up_by_offset(self):
        cur = R.resolve_range("7d", LAST, TODAY)
        comp = R.resolve_compare("previous", cur, TODAY)
        series = R.daily_series(day_rows(cur["start"], [10] * 7), day_rows(comp["start"], [20] * 7), cur, comp)
        self.assertEqual(len(series), 7)
        self.assertEqual(series[0]["prev"]["date"], comp["start"].isoformat())
        self.assertEqual(series[0]["prev"]["impressions"], 20)


class EventTests(SimpleTestCase):
    def test_a_lasting_cliff_is_found_once_with_its_date(self):
        start = dt.date(2026, 8, 1)
        values = [800] * 60 + [60] * 30
        rows = day_rows(start, values)
        events = R.events(rows, start + dt.timedelta(days=len(values) - 1))
        drops = [e for e in events if e["kind"] == "drop"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["date"], (start + dt.timedelta(days=60)).isoformat())
        self.assertFalse(drops[0]["recovered"])
        self.assertIn("fell 92%", R.event_sentence(drops[0]))

    def test_a_steady_line_has_no_events(self):
        start = dt.date(2026, 8, 1)
        self.assertEqual(R.events(day_rows(start, [500, 520, 480] * 30), start + dt.timedelta(days=89)), [])


class SectionTests(SimpleTestCase):
    def test_locale_folder_is_skipped(self):
        self.assertEqual(R.section_of("https://x.com/en/brokers/acme"), "/en/brokers")
        self.assertEqual(R.section_of("https://x.com/pt-br/blog/a"), "/pt-br/blog")
        self.assertEqual(R.section_of("https://x.com/blog/a"), "/blog")
        self.assertEqual(R.section_of("https://x.com/"), "/")
        self.assertEqual(R.section_of("https://x.com/en"), "/en")


class OpportunityTests(SimpleTestCase):
    def test_low_ctr_needs_a_readable_baseline(self):
        rows = [{"keys": [f"q{i}"], "clicks": 30, "impressions": 100, "position": 2.0} for i in range(3)]
        rows.append({"keys": ["weak"], "clicks": 1, "impressions": 100, "position": 2.0})
        curve = R.ctr_curve(rows)
        found = R.low_ctr(rows, curve, 28, {})
        self.assertEqual([r["key"] for r in found], ["weak"])

    def test_cannibalization_needs_two_real_shares(self):
        qp = [{"keys": ["acme", "https://x.com/a"], "clicks": 1, "impressions": 100, "position": 3.0},
              {"keys": ["acme", "https://x.com/b"], "clicks": 0, "impressions": 40, "position": 9.0},
              {"keys": ["solo", "https://x.com/a"], "clicks": 0, "impressions": 100, "position": 3.0},
              {"keys": ["solo", "https://x.com/c"], "clicks": 0, "impressions": 2, "position": 30.0}]
        self.assertEqual([r["key"] for r in R.cannibalization(qp)], ["acme"])


class ReportPageTests(TestCase):
    """The whole report for a declared property, with Google replaced by fixed rows."""

    def fake_query(self, prop, start, end, dimensions, **kwargs):
        days = (end - start).days + 1
        if dimensions == ["date"]:
            rows = day_rows(start, [100] * days)
            return {"rows": rows, "metadata": {"firstIncompleteDate": "2026-11-17"}}
        if dimensions == ["query"]:
            return {"rows": [{"keys": ["acme"], "clicks": 50, "impressions": 900, "position": 1.4},
                             {"keys": ["widgets"], "clicks": 3, "impressions": 300, "position": 8.0}], "metadata": {}}
        if dimensions == ["query", "page"]:
            return {"rows": [{"keys": ["acme", "https://acme.com/en"], "clicks": 50, "impressions": 900, "position": 1.4}],
                    "metadata": {}}
        key = {"page": "https://acme.com/en/blog/a", "country": "usa", "device": "MOBILE",
               "searchAppearance": "VIDEO"}[dimensions[0]]
        return {"rows": [{"keys": [key], "clicks": 10, "impressions": 100, "position": 4.0}], "metadata": {}}

    def test_every_property_renders_the_same_report(self):
        from unittest import mock

        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory, override_settings

        from keel_seo.gsc import report_api, views

        user = mock.Mock(is_authenticated=True, is_superuser=True)
        props = {"acme": {"site": "sc-domain:acme.com", "credentials": "/none.json", "label": "Acme",
                          "brands": [("Acme", "acme")]}}
        request = RequestFactory().get("/admin-os/search-console/acme", {"range": "7d"})
        request.user = user
        with override_settings(KEEL_SEO={"gsc_properties": props},
                               CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}), \
                mock.patch.object(report_api, "query", side_effect=self.fake_query), \
                mock.patch.object(report_api, "_call", return_value={"sitemap": []}):
            ctx = views.report_context(request, {"key": "acme", **props["acme"]})
            response = views.SearchConsoleView.as_view()(request, property_key="acme")
        data = ctx["report"]
        self.assertIsNone(ctx["report_error"])
        self.assertEqual(data["range"]["end"], "2026-11-16")
        self.assertEqual(data["range"]["days"], 7)
        self.assertEqual(data["compare"]["days"], 7)
        self.assertEqual(data["totals"]["impressions"], 700)
        self.assertEqual(data["tables"]["country"]["rows"][0]["label"], "United States")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for part in ("gsc-performance", "gsc-breakdown", "gsc-history", "gsc-opportunities",
                     "gsc-brand", "gsc-ranking", "gsc-sections", "gsc-sitemaps", "gsc-notes", "gsc-report-data"):
            self.assertIn(part, html)
        # The earlier period is an option on the chart, never drawn by default.
        self.assertIn("data-compare-toggle", html)
        self.assertNotIn("data-compare-toggle checked", html)


class DeviceAndMonthTests(SimpleTestCase):
    def test_device_gap_finds_pages_weaker_on_mobile(self):
        rows = [
            {"keys": ["https://x.com/a", "MOBILE"], "clicks": 1, "impressions": 200, "position": 9.0},
            {"keys": ["https://x.com/a", "DESKTOP"], "clicks": 20, "impressions": 200, "position": 4.0},
            {"keys": ["https://x.com/b", "MOBILE"], "clicks": 20, "impressions": 200, "position": 4.0},
            {"keys": ["https://x.com/b", "DESKTOP"], "clicks": 20, "impressions": 200, "position": 4.0},
        ]
        gap = R.device_gap(rows, 28)
        self.assertEqual([g["key"] for g in gap], ["https://x.com/a"])
        self.assertEqual((gap[0]["places"], gap[0]["missed"]), (5.0, 19))

    def test_complete_months_stop_at_the_last_finished_day(self):
        months = R.complete_months(dt.date(2026, 11, 16), TODAY)
        self.assertEqual(months[0], ("2026-10", "October 2026"))
        self.assertEqual(R.complete_months(dt.date(2026, 10, 31), TODAY)[0][0], "2026-10")

    def test_months_of_different_length_compare_per_day(self):
        ch = R.per_day_changes({"clicks": 310, "impressions": 3100, "ctr": 10.0, "position": 5.0},
                               {"clicks": 300, "impressions": 3000, "ctr": 10.0, "position": 5.0}, 31, 30)
        self.assertTrue(ch["clicks"]["flat"])


class PdfTests(TestCase):
    def test_the_monthly_report_prints(self):
        try:
            import weasyprint  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("weasyprint is not installed")
        from unittest import mock

        from django.test import override_settings

        from keel_seo.gsc import report_api, report_pdf

        prop = {"site": "sc-domain:acme.com", "credentials": "/none.json", "label": "Acme", "brands": [("Acme", "acme")]}
        with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}), \
                mock.patch.object(report_api, "query", side_effect=ReportPageTests.fake_query.__get__(ReportPageTests())), \
                mock.patch.object(report_api, "_call", return_value={"sitemap": []}):
            payload = report_api.monthly(prop, today=dt.date(2026, 11, 19))
            pdf = report_pdf.render(payload, prop, {"name": "Agency"})
        self.assertEqual(payload["month"], "2026-10")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(report_pdf.filename(prop, "2026-10"), "acme-search-console-2026-10.pdf")
