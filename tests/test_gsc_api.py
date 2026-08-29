"""Tests for the Search Console API layer.

Every test here runs offline: the Google client libraries are never imported and no
request leaves the machine. What is worth testing without a network is exactly the
part that carries the logic — request-body assembly, filter parsing, result
flattening, index-state derivation, quota accounting and the error explanations —
while the transport itself is thin enough that a fake request object proves it.
"""
from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from keel_seo.gsc import analytics, auth, check, indexing, inspection, sitemaps
from keel_seo.models import IndexingSubmission, UrlInspection


class FakeRequest:
    """Stands in for a googleapiclient request: returns, or raises, on execute()."""

    def __init__(self, result=None, error=None, fail_times=0):
        self.result = result or {}
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.fail_times >= self.calls:
            raise FakeHttpError(503, "backend error")
        if self.error is not None:
            raise self.error
        return self.result


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(f"<HttpError {status} {message}>")
        self.resp = FakeResponse(status)


class SiteResolutionTests(SimpleTestCase):
    def test_explicit_site_wins(self):
        self.assertEqual(auth.resolve_site("sc-domain:a.com"), "sc-domain:a.com")

    def test_env_is_used_when_no_argument(self):
        with self.settings(KEEL_SEO={}):
            with self.modify_settings():
                import os

                os.environ["GSC_SITE"] = "sc-domain:env.com"
                try:
                    self.assertEqual(auth.resolve_site(), "sc-domain:env.com")
                finally:
                    del os.environ["GSC_SITE"]

    @override_settings(KEEL_SEO={"gsc_site": "sc-domain:settings.com"})
    def test_django_setting_is_the_fallback(self):
        self.assertEqual(auth.resolve_site(), "sc-domain:settings.com")

    @override_settings(KEEL_SEO={})
    def test_unset_property_raises_rather_than_guessing(self):
        with self.assertRaises(auth.GscError):
            auth.resolve_site()


class ExecuteTests(SimpleTestCase):
    def test_retries_transient_failures_then_succeeds(self):
        request = FakeRequest(result={"ok": True}, fail_times=2)
        # Retries sleep with backoff; two failures is under a second in total.
        self.assertEqual(auth.execute(request, retries=3), {"ok": True})
        self.assertEqual(request.calls, 3)

    def test_dropped_connections_are_retried_like_a_503(self):
        class Flaky(FakeRequest):
            def execute(self):
                self.calls += 1
                if self.calls < 3:
                    raise ConnectionResetError(104, "Connection reset by peer")
                return {"ok": True}

        request = Flaky()
        self.assertEqual(auth.execute(request, retries=3), {"ok": True})
        self.assertEqual(request.calls, 3)

    def test_unrecovered_connection_failure_reads_as_a_network_problem(self):
        message = auth.explain(ConnectionResetError(104, "Connection reset by peer"))
        self.assertIn("network problem, not an API one", message)

    def test_permanent_failure_is_not_retried(self):
        request = FakeRequest(error=FakeHttpError(400, "bad request"))
        with self.assertRaises(auth.GscError):
            auth.execute(request, retries=3)
        self.assertEqual(request.calls, 1)

    def test_403_explanation_names_the_permission_ladder(self):
        message = auth.explain(FakeHttpError(403, "forbidden"))
        self.assertIn("Users and permissions", message)
        self.assertIn("URL Inspection needs Full or Owner", message)
        self.assertIn("Indexing API needs Owner", message)

    def test_disabled_api_explanation_links_both_api_pages(self):
        message = auth.explain(Exception("SERVICE_DISABLED: has not been used in project 123"))
        self.assertIn("searchconsole.googleapis.com", message)
        self.assertIn("indexing.googleapis.com", message)


class AnalyticsBodyTests(SimpleTestCase):
    def test_filter_expression_keeps_multi_word_values(self):
        parsed = analytics.parse_filter("query contains best prop firm")
        self.assertEqual(
            parsed,
            {"dimension": "query", "operator": "contains", "expression": "best prop firm"},
        )

    def test_operator_spelling_is_normalised(self):
        self.assertEqual(analytics.parse_filter("page notContains /tag/")["operator"], "notContains")
        self.assertEqual(analytics.parse_filter("page not_contains /tag/")["operator"], "notContains")

    def test_unknown_operator_is_rejected_with_the_valid_list(self):
        with self.assertRaises(auth.GscError) as ctx:
            analytics.parse_filter("page startswith /blog/")
        self.assertIn("includingRegex", str(ctx.exception))

    def test_body_carries_every_option_the_api_supports(self):
        body = analytics.build_body(
            start_date="2026-01-01",
            end_date="2026-01-28",
            dimensions=("query", "page"),
            filters=["page contains /blog/"],
            search_type="discover",
            aggregation_type="byPage",
            data_state="all",
            row_limit=99,
        )
        self.assertEqual(body["type"], "discover")
        self.assertEqual(body["aggregationType"], "byPage")
        self.assertEqual(body["dataState"], "all")
        self.assertEqual(body["rowLimit"], 99)
        self.assertEqual(body["dimensionFilterGroups"][0]["filters"][0]["expression"], "/blog/")

    def test_row_limit_is_capped_at_the_api_maximum(self):
        body = analytics.build_body(
            start_date="2026-01-01", end_date="2026-01-02", row_limit=999999
        )
        self.assertEqual(body["rowLimit"], analytics.MAX_ROWS_PER_CALL)

    def test_unknown_enum_values_are_rejected_before_the_call(self):
        for kwargs in (
            {"search_type": "podcast"},
            {"aggregation_type": "bySomething"},
            {"data_state": "maybe"},
            {"dimensions": ("query", "browser")},
        ):
            with self.assertRaises(auth.GscError):
                analytics.build_body(start_date="2026-01-01", end_date="2026-01-02", **kwargs)

    def test_final_window_stops_at_the_data_lag_while_all_reaches_today(self):
        _, final_end = analytics.window(days=7, data_state="final")
        _, all_end = analytics.window(days=7, data_state="all")
        self.assertEqual(
            final_end,
            (dt.date.today() - dt.timedelta(days=auth.DATA_LAG_DAYS)).isoformat(),
        )
        self.assertEqual(all_end, dt.date.today().isoformat())

    def test_records_zip_keys_back_onto_dimension_names(self):
        rows = [{"keys": ["prop firm", "/a/"], "clicks": 3, "impressions": 90, "ctr": 0.033, "position": 8.1}]
        record = analytics.to_records(rows, ["query", "page"])[0]
        self.assertEqual(record["query"], "prop firm")
        self.assertEqual(record["page"], "/a/")
        self.assertEqual(record["clicks"], 3)


class InspectionSummaryTests(SimpleTestCase):
    INDEXED = {
        "indexStatusResult": {
            "verdict": "PASS",
            "coverageState": "Submitted and indexed",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "pageFetchState": "SUCCESSFUL",
            "lastCrawlTime": "2026-08-20T10:00:00Z",
            "googleCanonical": "https://example.com/a/",
            "userCanonical": "https://example.com/a/",
            "sitemap": ["https://example.com/sitemap.xml"],
            "crawledAs": "MOBILE",
        },
        "mobileUsabilityResult": {"verdict": "PASS"},
    }

    def test_indexed_page_is_flattened_and_marked_indexed(self):
        summary = inspection.summarize(self.INDEXED)
        self.assertTrue(summary["indexed"])
        self.assertFalse(summary["canonical_mismatch"])
        self.assertEqual(summary["coverage_state"], "Submitted and indexed")
        self.assertEqual(summary["sitemaps"], ["https://example.com/sitemap.xml"])

    def test_crawled_but_not_indexed_is_not_counted_as_indexed(self):
        summary = inspection.summarize(
            {"indexStatusResult": {"coverageState": "Crawled - currently not indexed"}}
        )
        self.assertFalse(summary["indexed"])

    def test_unfamiliar_indexed_phrasing_still_reads_as_indexed(self):
        summary = inspection.summarize(
            {"indexStatusResult": {"coverageState": "Indexed, though blocked by robots.txt"}}
        )
        self.assertTrue(summary["indexed"])

    def test_canonical_mismatch_is_surfaced_as_its_own_flag(self):
        summary = inspection.summarize(
            {
                "indexStatusResult": {
                    "coverageState": "Duplicate, Google chose different canonical than user",
                    "googleCanonical": "https://example.com/b/",
                    "userCanonical": "https://example.com/a/",
                }
            }
        )
        self.assertTrue(summary["canonical_mismatch"])
        self.assertFalse(summary["indexed"])

    def test_missing_blocks_collapse_to_empty_values(self):
        summary = inspection.summarize({})
        self.assertEqual(summary["amp_verdict"], "")
        self.assertEqual(summary["mobile_issues"], [])
        self.assertEqual(summary["referring_urls"], [])

    def test_coverage_report_counts_every_failure_mode(self):
        entries = [
            {"url": "/a/", "ok": True, "summary": inspection.summarize(self.INDEXED)},
            {
                "url": "/b/",
                "ok": True,
                "summary": inspection.summarize(
                    {
                        "indexStatusResult": {
                            "coverageState": "Crawled - currently not indexed",
                            "robotsTxtState": "DISALLOWED",
                            "pageFetchState": "SOFT_404",
                        }
                    }
                ),
            },
            {"url": "/c/", "ok": False, "error": "boom"},
        ]
        report = inspection.coverage_report(entries)
        self.assertEqual(
            (report["total"], report["ok"], report["failed"], report["indexed"], report["not_indexed"]),
            (3, 2, 1, 1, 1),
        )
        self.assertEqual(report["robots_blocked"], 1)
        self.assertEqual(report["fetch_problem"], 1)
        self.assertEqual(report["coverage_states"]["Submitted and indexed"], 1)

    def test_relative_url_is_refused_before_any_call(self):
        with self.assertRaises(auth.GscError):
            inspection.inspect_url("/relative/", "sc-domain:example.com")


class IndexingTests(SimpleTestCase):
    def test_indexing_error_is_catchable_as_a_gsc_error(self):
        self.assertTrue(issubclass(indexing.IndexingError, auth.GscError))

    def test_relative_url_is_refused(self):
        with self.assertRaises(indexing.IndexingError):
            indexing.notify_url("/relative/")

    def test_batch_stops_at_the_daily_quota(self):
        urls = [f"/relative-{i}" for i in range(10)]
        results = indexing.notify_urls(urls, max_calls=4)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(not r["ok"] for r in results))

    def test_removal_guidance_states_the_boundary_and_the_robots_trap(self):
        guidance = indexing.removal_guidance("https://example.com/gone/")
        self.assertIn("no public API", guidance["no_api"])
        self.assertTrue(any("robots.txt" in step for step in guidance["durable_path"]))


class SitemapSummaryTests(SimpleTestCase):
    def test_counts_are_summed_across_content_types(self):
        summary = sitemaps.summarize(
            {
                "path": "https://example.com/sitemap.xml",
                "isSitemapsIndex": True,
                "lastDownloaded": "2026-08-28T04:00:00Z",
                "warnings": "2",
                "errors": "0",
                "contents": [
                    {"type": "web", "submitted": "120", "indexed": "100"},
                    {"type": "image", "submitted": "30", "indexed": "10"},
                ],
            }
        )
        self.assertEqual((summary["submitted"], summary["indexed"]), (150, 110))
        self.assertEqual(summary["warnings"], 2)
        self.assertTrue(summary["is_index"])

    def test_a_path_rather_than_a_url_is_refused(self):
        with self.assertRaises(auth.GscError):
            sitemaps.submit_sitemap("/sitemap.xml", "sc-domain:example.com")


class CheckProbeTests(SimpleTestCase):
    def test_domain_property_probe_url_is_the_https_root(self):
        self.assertEqual(check._home_url("sc-domain:example.com"), "https://example.com/")

    def test_url_prefix_property_probe_keeps_its_prefix(self):
        self.assertEqual(check._home_url("https://example.com/shop"), "https://example.com/shop/")

    def test_report_marks_failures_and_prints_their_fix(self):
        report = check.format_report(
            [
                check._result("service-account key", check.OK, "sa@x.iam"),
                check._result("Indexing API", check.FAILED, "403", "Add the account as Owner."),
            ]
        )
        self.assertIn("[FAIL] Indexing API", report)
        self.assertIn("fix: Add the account as Owner.", report)
        self.assertIn("1/2 checks passed", report)


class StateStoreTests(TestCase):
    def test_one_inspection_row_per_site_and_url(self):
        from keel_seo.management.commands.keel_seo_gsc_inspect import _store

        entry = {
            "url": "https://example.com/a/",
            "ok": True,
            "result": {"indexStatusResult": {"coverageState": "Submitted and indexed"}},
            "summary": inspection.summarize(InspectionSummaryTests.INDEXED),
        }
        _store("sc-domain:example.com", entry)
        entry["summary"] = inspection.summarize(
            {"indexStatusResult": {"coverageState": "Crawled - currently not indexed"}}
        )
        _store("sc-domain:example.com", entry)

        self.assertEqual(UrlInspection.objects.count(), 1)
        row = UrlInspection.objects.get()
        self.assertFalse(row.indexed)
        self.assertEqual(row.coverage_state, "Crawled - currently not indexed")

    def test_last_crawl_time_parses_googles_trailing_z(self):
        from keel_seo.management.commands.keel_seo_gsc_inspect import _parse_time

        parsed = _parse_time("2026-08-20T10:00:00Z")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.utcoffset(), dt.timedelta(0))
        self.assertIsNone(_parse_time(""))
        self.assertIsNone(_parse_time("not a date"))

    def test_submissions_are_an_append_only_log(self):
        for ok in (True, False):
            IndexingSubmission.objects.create(
                site="sc-domain:example.com",
                url="https://example.com/a/",
                notification_type=indexing.URL_UPDATED,
                submitted_at=timezone.now(),
                ok=ok,
            )
        self.assertEqual(IndexingSubmission.objects.count(), 2)
