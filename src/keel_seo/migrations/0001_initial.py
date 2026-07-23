"""Initial keel-seo migration — ADOPT an existing Landing table (state-only).

This migration was authored against SignalBots, which already had a populated
``core_landing`` table (set via ``KEEL_SEO["landing_db_table"]``). It therefore
uses ``SeparateDatabaseAndState`` so Django records the ``Landing`` model in its
migration STATE but performs no ``CREATE TABLE`` — the host's existing table (and
its data) is adopted untouched. The ``db_table`` and index name below match that
existing table exactly, so ``makemigrations --check`` stays clean.

Greenfield note: a brand-new project with no pre-existing landing table cannot
rely on this state-only migration to create it — provide the table (or a
create-including initial) for that case. Tracked as a keel-seo follow-up.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Landing',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('title', models.CharField(help_text="Human-readable label shown in the admin table (e.g. 'Pricing Page').", max_length=200)),
                        ('url', models.CharField(help_text="Absolute URL path with leading and trailing slashes, e.g. '/', '/pricing/', '/trading-bots/pocket-option/'.", max_length=255, unique=True)),
                        ('is_indexable', models.BooleanField(default=False, help_text="When True, the page emits 'index, follow' and is listed in sitemap.xml.")),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        'verbose_name': 'Landing page',
                        'verbose_name_plural': 'Landing pages',
                        'db_table': 'core_landing',
                        'ordering': ['-created_at'],
                        'indexes': [models.Index(fields=['is_indexable'], name='core_landin_is_inde_3757c6_idx')],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
