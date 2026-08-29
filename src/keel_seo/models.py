from django.db import models

from .config import seo_setting


class Landing(models.Model):
    """Registry of all marketing/landing pages on the site.

    The single source of truth for which landings exist and whether each one
    should be indexed by search engines and listed in sitemap.xml. A page is
    indexable only when its row's ``is_indexable`` is True; the default is False
    so new landings are noindex until explicitly opened up.
    """

    title = models.CharField(
        max_length=200,
        help_text="Human-readable label shown in the admin table (e.g. 'Pricing Page').",
    )
    url = models.CharField(
        max_length=255,
        unique=True,
        help_text="Absolute URL path with leading and trailing slashes, "
                  "e.g. '/', '/pricing/', '/trading-bots/pocket-option/'.",
    )
    is_indexable = models.BooleanField(
        default=False,
        help_text="When True, the page emits 'index, follow' and is listed in sitemap.xml.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    content_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 hex digest of the page's normalized rendered content "
                  "(see keel_seo.freshness.normalize_content). Empty until the "
                  "keel_seo_freshness command has processed this URL at least once.",
    )
    content_modified_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the rendered content genuinely last changed, per a "
                  "content-hash comparison (keel_seo.freshness). None until first "
                  "recorded by the keel_seo_freshness command. Publish THIS as "
                  "dateModified/lastmod -- never 'updated_at', which bumps on every "
                  "re-save regardless of whether the rendered content changed.",
    )

    class Meta:
        db_table = seo_setting("landing_db_table")
        ordering = ["-created_at"]
        verbose_name = "Landing page"
        verbose_name_plural = "Landing pages"
        indexes = [models.Index(fields=["is_indexable"])]

    def __str__(self) -> str:
        flag = "✓" if self.is_indexable else "✗"
        return f"{flag} {self.title} ({self.url})"


class UrlInspection(models.Model):
    """The latest URL Inspection API answer for one URL of one property.

    One row per (site, url): an inspection is a *current state* reading, not an event
    log, and the daily quota (2,000 per property) makes re-inspecting cheap only if
    the store tells us what is already fresh. ``fetched_at`` is what the sweep filters
    on to skip URLs inspected recently, so a nightly pass over a 5,000-URL site walks
    the backlog in quota-sized bites instead of restarting from the top every night.

    Populated by ``manage.py keel_seo_gsc_inspect``; nothing writes it implicitly.
    """

    site = models.CharField(
        max_length=255,
        db_index=True,
        help_text="The Search Console property this reading came from, "
                  "e.g. 'sc-domain:example.com'.",
    )
    url = models.URLField(
        max_length=500,
        help_text="The absolute URL that was inspected.",
    )
    fetched_at = models.DateTimeField(
        db_index=True,
        help_text="When this inspection was performed (not when Google last crawled).",
    )

    verdict = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Overall index-status verdict: PASS, FAIL, NEUTRAL or PARTIAL.",
    )
    coverage_state = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Google's own phrasing, e.g. 'Submitted and indexed', "
                  "'Crawled - currently not indexed', 'Duplicate without user-selected canonical'.",
    )
    indexing_state = models.CharField(max_length=64, blank=True, default="")
    robots_txt_state = models.CharField(max_length=64, blank=True, default="")
    page_fetch_state = models.CharField(max_length=64, blank=True, default="")
    crawled_as = models.CharField(max_length=64, blank=True, default="")
    last_crawl_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Googlebot last crawled the URL, per Google. Null when never crawled.",
    )

    google_canonical = models.URLField(max_length=500, blank=True, default="")
    user_canonical = models.URLField(max_length=500, blank=True, default="")
    canonical_mismatch = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Google picked a different canonical than the page declares — the quiet "
                  "failure mode where a page is crawled fine and still ranks nothing.",
    )
    indexed = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Derived from coverage_state (see keel_seo.gsc.inspection.is_indexed).",
    )

    mobile_verdict = models.CharField(max_length=20, blank=True, default="")
    rich_results_verdict = models.CharField(max_length=20, blank=True, default="")
    amp_verdict = models.CharField(max_length=20, blank=True, default="")

    raw = models.JSONField(
        default=dict,
        blank=True,
        help_text="The full inspectionResult payload, kept so a later question about a "
                  "field this model does not flatten never needs the quota spent again.",
    )

    class Meta:
        verbose_name = "URL inspection"
        verbose_name_plural = "URL inspections"
        ordering = ["-fetched_at"]
        constraints = [
            models.UniqueConstraint(fields=["site", "url"], name="keel_seo_urlinspection_unique"),
        ]
        indexes = [models.Index(fields=["indexed", "fetched_at"])]

    def __str__(self) -> str:
        flag = "✓" if self.indexed else "✗"
        return f"{flag} {self.url} ({self.coverage_state or 'unknown'})"


class IndexingSubmission(models.Model):
    """One Indexing API notification we sent, kept as an append-only log.

    The Indexing API's daily quota is per Cloud *project*, not per property, so five
    sites on one key share 200 publishes a day. A log is the only way to answer "have
    we already asked for this URL today?" before spending one of them, and the only
    way to see, after the fact, which pages a burst was spent on.
    """

    site = models.CharField(max_length=255, db_index=True, blank=True, default="")
    url = models.URLField(max_length=500, db_index=True)
    notification_type = models.CharField(
        max_length=20,
        help_text="URL_UPDATED (published/changed) or URL_DELETED (gone).",
    )
    submitted_at = models.DateTimeField(db_index=True)
    ok = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")
    response = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Indexing API submission"
        verbose_name_plural = "Indexing API submissions"
        ordering = ["-submitted_at"]
        indexes = [models.Index(fields=["url", "submitted_at"])]

    def __str__(self) -> str:
        flag = "✓" if self.ok else "✗"
        return f"{flag} {self.notification_type} {self.url}"
