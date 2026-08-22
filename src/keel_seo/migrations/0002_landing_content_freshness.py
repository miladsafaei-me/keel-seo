"""Add the content-freshness tracking fields to Landing.

``content_hash`` and ``content_modified_at`` are populated by
``keel_seo.freshness.record`` (run via the ``keel_seo_freshness`` management
command). They are independent of ``updated_at``, which keeps its existing
auto_now behavior and must never be published as a page's public
last-modified date -- see ``keel_seo/freshness.py`` for why.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('keel_seo', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='landing',
            name='content_hash',
            field=models.CharField(
                blank=True,
                default='',
                help_text="SHA-256 hex digest of the page's normalized rendered content "
                          "(see keel_seo.freshness.normalize_content). Empty until the "
                          "keel_seo_freshness command has processed this URL at least once.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name='landing',
            name='content_modified_at',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="When the rendered content genuinely last changed, per a "
                          "content-hash comparison (keel_seo.freshness). None until first "
                          "recorded by the keel_seo_freshness command. Publish THIS as "
                          "dateModified/lastmod -- never 'updated_at', which bumps on every "
                          "re-save regardless of whether the rendered content changed.",
                null=True,
            ),
        ),
    ]
