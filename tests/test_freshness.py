"""Tests for keel_seo.freshness -- the content-hashing engine.

Run: DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests.test_freshness
"""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase

from keel_seo.freshness import (
    FreshnessOutcome,
    freshness_for,
    freshness_schema,
    normalize_content,
    record,
)
from keel_seo.models import Landing


class NormalizeContentTests(TestCase):
    """Pure hashing-input tests -- no DB, no HTTP."""

    def test_extracts_only_the_selector_region(self):
        html = "<body><header>nav</header><main><p>Body</p></main><footer>f</footer></body>"
        self.assertEqual(normalize_content(html), "<main><p>Body</p></main>")

    def test_missing_selector_raises(self):
        html = "<body><p>No main here</p></body>"
        with self.assertRaises(ValueError):
            normalize_content(html, selector="main")

    def test_id_selector(self):
        html = '<body><div id="content"><p>Hi</p></div></body>'
        self.assertEqual(normalize_content(html, selector="#content"), '<div id="content"><p>Hi</p></div>')

    def test_class_selector(self):
        html = '<body><section class="article body">Text</section></body>'
        self.assertEqual(
            normalize_content(html, selector=".body"),
            '<section class="article body">Text</section>',
        )

    def test_cache_busting_query_string_is_stripped(self):
        html_a = '<main><img src="/s/logo.png?v=abc123"></main>'
        html_b = '<main><img src="/s/logo.png?v=999fff"></main>'
        self.assertEqual(normalize_content(html_a), normalize_content(html_b))

    def test_csrf_token_field_is_stripped(self):
        html_a = '<main><form><input type="hidden" name="csrfmiddlewaretoken" value="AAAA"></form></main>'
        html_b = '<main><form><input type="hidden" name="csrfmiddlewaretoken" value="BBBB"></form></main>'
        self.assertEqual(normalize_content(html_a), normalize_content(html_b))

    def test_nonce_attribute_is_stripped(self):
        html_a = '<main><script nonce="aaa">x()</script></main>'
        html_b = '<main><script nonce="bbb">x()</script></main>'
        self.assertEqual(normalize_content(html_a), normalize_content(html_b))

    def test_data_keel_freshness_element_is_excluded(self):
        html_a = "<main><p>Body copy</p></main>"
        html_b = (
            "<main><p>Body copy</p>"
            '<p class="last-updated" data-keel-freshness>'
            '<time datetime="2026-08-22T00:00:00Z">August 22, 2026</time></p></main>'
        )
        self.assertEqual(normalize_content(html_a), normalize_content(html_b))

    def test_nested_same_tag_is_balanced_correctly(self):
        # A malformed-but-plausible nested <main> must not truncate extraction early.
        html = "<main>outer <main>inner</main> tail</main>"
        self.assertEqual(normalize_content(html), "<main>outer <main>inner</main> tail</main>")

    def test_genuinely_different_content_normalizes_differently(self):
        html_a = "<main><p>Version one.</p></main>"
        html_b = "<main><p>Version two.</p></main>"
        self.assertNotEqual(normalize_content(html_a), normalize_content(html_b))

    def test_whitespace_is_collapsed(self):
        html_a = "<main>\n\n  <p>Hello   world</p>\n</main>"
        html_b = "<main> <p>Hello world</p> </main>"
        self.assertEqual(normalize_content(html_a), normalize_content(html_b))


class RecordTests(TestCase):
    """DB-backed tests for record()/freshness_for()/freshness_schema()."""

    def setUp(self):
        self.landing = Landing.objects.create(url="/x/", title="X", is_indexable=True)
        self.html_a = "<main><p>Original body copy.</p></main>"
        self.html_a_diff_csrf = (
            '<main><p>Original body copy.</p>'
            '<input type="hidden" name="csrfmiddlewaretoken" value="ZZZZ"></main>'
        )
        self.html_b = "<main><p>Rewritten body copy.</p></main>"

    def test_first_record_is_created(self):
        result = record(self.landing.url, self.html_a)
        self.assertEqual(result.outcome, FreshnessOutcome.CREATED)
        self.landing.refresh_from_db()
        self.assertIsNotNone(self.landing.content_modified_at)
        self.assertTrue(self.landing.content_hash)

    def test_idempotent_on_unchanged_content(self):
        record(self.landing.url, self.html_a)
        self.landing.refresh_from_db()
        first_date = self.landing.content_modified_at

        result = record(self.landing.url, self.html_a)
        self.assertEqual(result.outcome, FreshnessOutcome.UNCHANGED)
        self.landing.refresh_from_db()
        self.assertEqual(self.landing.content_modified_at, first_date)

    def test_volatile_strip_coverage_keeps_date_unchanged(self):
        record(self.landing.url, self.html_a)
        self.landing.refresh_from_db()
        first_date = self.landing.content_modified_at

        # Same visible content, different CSRF token -- must NOT look like a change.
        result = record(self.landing.url, self.html_a_diff_csrf)
        self.assertEqual(result.outcome, FreshnessOutcome.UNCHANGED)
        self.landing.refresh_from_db()
        self.assertEqual(self.landing.content_modified_at, first_date)

    def test_genuine_change_moves_the_date(self):
        record(self.landing.url, self.html_a)
        self.landing.refresh_from_db()
        first_date = self.landing.content_modified_at

        result = record(self.landing.url, self.html_b)
        self.assertEqual(result.outcome, FreshnessOutcome.CHANGED)
        self.landing.refresh_from_db()
        self.assertIsNotNone(self.landing.content_modified_at)
        self.assertNotEqual(self.landing.content_modified_at, first_date)

    def test_dry_run_never_writes(self):
        result = record(self.landing.url, self.html_a, dry_run=True)
        self.assertEqual(result.outcome, FreshnessOutcome.CREATED)
        self.landing.refresh_from_db()
        self.assertFalse(self.landing.content_hash)
        self.assertIsNone(self.landing.content_modified_at)

    def test_updated_at_is_never_touched(self):
        record(self.landing.url, self.html_a)
        self.landing.refresh_from_db()
        stamp_after_first = self.landing.updated_at

        record(self.landing.url, self.html_b)
        self.landing.refresh_from_db()
        self.assertEqual(self.landing.updated_at, stamp_after_first)

    def test_unregistered_url_raises_does_not_exist(self):
        with self.assertRaises(Landing.DoesNotExist):
            record("/never-registered/", self.html_a)

    def test_freshness_for_unknown_url_is_none(self):
        self.assertIsNone(freshness_for("/never-registered/"))

    def test_freshness_for_and_schema_after_record(self):
        record(self.landing.url, self.html_a)
        dt = freshness_for(self.landing.url)
        self.assertIsNotNone(dt)
        schema = freshness_schema(self.landing.url)
        self.assertIn("dateModified", schema)
        self.assertTrue(schema["dateModified"].endswith("Z"))

    def test_freshness_schema_unknown_url_is_empty(self):
        self.assertEqual(freshness_schema("/never-registered/"), {})


class TemplateTagAndCommandTests(TestCase):
    """Full-stack: render through the test Client, exercise the management command."""

    def setUp(self):
        self.landing = Landing.objects.create(url="/page/", title="Page", is_indexable=True)
        self.client = Client()

    def test_last_updated_renders_nothing_before_first_record(self):
        response = self.client.get("/page/")
        self.assertNotIn("keel-seo-last-updated", response.content.decode())

    def test_freshness_line_is_excluded_from_its_own_hash(self):
        first_html = self.client.get("/page/").content.decode()
        result_1 = record(self.landing.url, first_html)
        self.assertEqual(result_1.outcome, FreshnessOutcome.CREATED)

        second_html = self.client.get("/page/").content.decode()
        self.assertIn("keel-seo-last-updated", second_html)  # the line now renders...
        result_2 = record(self.landing.url, second_html)
        # ...but must not count as a content change, or the date could never converge.
        self.assertEqual(result_2.outcome, FreshnessOutcome.UNCHANGED)

    def test_command_dry_run_reports_without_writing(self):
        out = StringIO()
        call_command("keel_seo_freshness", "--dry-run", stdout=out)
        self.landing.refresh_from_db()
        self.assertIsNone(self.landing.content_modified_at)
        self.assertIn("created=1", out.getvalue())

    def test_command_records_and_is_idempotent(self):
        out = StringIO()
        call_command("keel_seo_freshness", stdout=out)
        self.landing.refresh_from_db()
        self.assertIsNotNone(self.landing.content_modified_at)
        first_date = self.landing.content_modified_at

        out2 = StringIO()
        call_command("keel_seo_freshness", stdout=out2)
        self.landing.refresh_from_db()
        self.assertEqual(self.landing.content_modified_at, first_date)
        self.assertIn("unchanged=1", out2.getvalue())

    def test_command_single_url_option(self):
        Landing.objects.create(url="/other/", title="Other", is_indexable=True)
        out = StringIO()
        call_command("keel_seo_freshness", "--url", "/page/", stdout=out)
        self.assertIn("created=1", out.getvalue())
        other = Landing.objects.get(url="/other/")
        self.assertIsNone(other.content_modified_at)

    def test_command_counts_failures_and_exits_nonzero(self):
        Landing.objects.create(url="/broken/", title="Broken", is_indexable=True)
        out = StringIO()
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command("keel_seo_freshness", stdout=out, stderr=err)
        self.assertIn("failed=1", out.getvalue())

    def test_command_is_noop_when_disabled(self):
        with self.settings(KEEL_SEO={"freshness_enabled": False}):
            out = StringIO()
            call_command("keel_seo_freshness", stdout=out)
        self.landing.refresh_from_db()
        self.assertIsNone(self.landing.content_modified_at)
