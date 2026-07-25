"""Initial keel-seo migration — creates the Landing table (greenfield default),
or adopts a host's existing table when ``KEEL_SEO["adopt_existing"]`` is True.

Two modes, selected at load time from settings so the same migration serves both
a fresh project and a host migrating off its own in-repo table:

* Greenfield (default, ``adopt_existing=False``): the ``CreateModel`` runs against
  the database, so a plain ``migrate`` builds the table. This is the common case
  for a new fork.
* Adoption (``adopt_existing=True``): the operation is wrapped in
  ``SeparateDatabaseAndState`` with empty ``database_operations`` — Django records
  the model in migration STATE but emits no ``CREATE TABLE``, adopting the host's
  existing table (e.g. SignalBots' populated ``core_landing``) untouched.

The table name and index are derived from ``KEEL_SEO["landing_db_table"]`` — the
same source ``Landing.Meta`` reads — so ``makemigrations --check`` stays clean for
whatever table name the host configures.
"""
from django.db import migrations, models

from keel_seo.config import seo_setting
from keel_seo.models import Landing

_LANDING_TABLE = seo_setting("landing_db_table")

# The Landing index is unnamed in Meta, so Django derives its name from the
# db_table. Read that resolved name back off the model so the migration's explicit
# name matches whatever table the host configured — keeps makemigrations --check clean.
_IS_INDEXABLE_INDEX_NAME = Landing._meta.indexes[0].name

_CREATE_LANDING = migrations.CreateModel(
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
        'db_table': _LANDING_TABLE,
        'ordering': ['-created_at'],
        'indexes': [models.Index(fields=['is_indexable'], name=_IS_INDEXABLE_INDEX_NAME)],
    },
)


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    if seo_setting("adopt_existing"):
        operations = [
            migrations.SeparateDatabaseAndState(
                state_operations=[_CREATE_LANDING],
                database_operations=[],
            ),
        ]
    else:
        operations = [_CREATE_LANDING]
