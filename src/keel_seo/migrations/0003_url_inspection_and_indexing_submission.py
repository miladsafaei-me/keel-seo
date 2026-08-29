"""Add the Search Console state store: UrlInspection and IndexingSubmission.

Both tables are additive and independent of ``Landing`` — a host that adopted an
existing landing table (``KEEL_SEO["adopt_existing"]``) still gets these created
normally, because there is no pre-existing host table to adopt for either one.

``UrlInspection`` holds one current-state row per (site, url), written by
``manage.py keel_seo_gsc_inspect``. ``IndexingSubmission`` is the append-only log of
Indexing API notifications, written by ``manage.py keel_seo_gsc_index``; it exists
because that API's daily quota is per Cloud project rather than per property, so the
log is what stops two properties from silently spending each other's budget.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('keel_seo', '0002_landing_content_freshness'),
    ]

    operations = [
        migrations.CreateModel(
            name='UrlInspection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('site', models.CharField(db_index=True, help_text="The Search Console property this reading came from, e.g. 'sc-domain:example.com'.", max_length=255)),
                ('url', models.URLField(help_text='The absolute URL that was inspected.', max_length=500)),
                ('fetched_at', models.DateTimeField(db_index=True, help_text='When this inspection was performed (not when Google last crawled).')),
                ('verdict', models.CharField(blank=True, default='', help_text='Overall index-status verdict: PASS, FAIL, NEUTRAL or PARTIAL.', max_length=20)),
                ('coverage_state', models.CharField(blank=True, db_index=True, default='', help_text="Google's own phrasing, e.g. 'Submitted and indexed', 'Crawled - currently not indexed', 'Duplicate without user-selected canonical'.", max_length=255)),
                ('indexing_state', models.CharField(blank=True, default='', max_length=64)),
                ('robots_txt_state', models.CharField(blank=True, default='', max_length=64)),
                ('page_fetch_state', models.CharField(blank=True, default='', max_length=64)),
                ('crawled_as', models.CharField(blank=True, default='', max_length=64)),
                ('last_crawl_time', models.DateTimeField(blank=True, help_text='When Googlebot last crawled the URL, per Google. Null when never crawled.', null=True)),
                ('google_canonical', models.URLField(blank=True, default='', max_length=500)),
                ('user_canonical', models.URLField(blank=True, default='', max_length=500)),
                ('canonical_mismatch', models.BooleanField(db_index=True, default=False, help_text='Google picked a different canonical than the page declares — the quiet failure mode where a page is crawled fine and still ranks nothing.')),
                ('indexed', models.BooleanField(db_index=True, default=False, help_text='Derived from coverage_state (see keel_seo.gsc.inspection.is_indexed).')),
                ('mobile_verdict', models.CharField(blank=True, default='', max_length=20)),
                ('rich_results_verdict', models.CharField(blank=True, default='', max_length=20)),
                ('amp_verdict', models.CharField(blank=True, default='', max_length=20)),
                ('raw', models.JSONField(blank=True, default=dict, help_text='The full inspectionResult payload, kept so a later question about a field this model does not flatten never needs the quota spent again.')),
            ],
            options={
                'verbose_name': 'URL inspection',
                'verbose_name_plural': 'URL inspections',
                'ordering': ['-fetched_at'],
            },
        ),
        migrations.CreateModel(
            name='IndexingSubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('site', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('url', models.URLField(db_index=True, max_length=500)),
                ('notification_type', models.CharField(help_text='URL_UPDATED (published/changed) or URL_DELETED (gone).', max_length=20)),
                ('submitted_at', models.DateTimeField(db_index=True)),
                ('ok', models.BooleanField(default=False)),
                ('error', models.TextField(blank=True, default='')),
                ('response', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'verbose_name': 'Indexing API submission',
                'verbose_name_plural': 'Indexing API submissions',
                'ordering': ['-submitted_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='urlinspection',
            constraint=models.UniqueConstraint(fields=('site', 'url'), name='keel_seo_urlinspection_unique'),
        ),
        migrations.AddIndex(
            model_name='urlinspection',
            index=models.Index(fields=['indexed', 'fetched_at'], name='keel_seo_ur_indexed_41d069_idx'),
        ),
        migrations.AddIndex(
            model_name='indexingsubmission',
            index=models.Index(fields=['url', 'submitted_at'], name='keel_seo_in_url_e4eed3_idx'),
        ),
    ]
