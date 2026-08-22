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
