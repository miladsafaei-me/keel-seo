"""Render every indexable Landing URL and record its content-freshness hash.

Meant to run as a deploy one-shot -- safe to call on every deploy, always: it
is a no-op unless ``KEEL_SEO["freshness_enabled"]`` is True, and even when
enabled it only ever moves a URL's ``content_modified_at`` when that URL's
rendered content genuinely changed (see ``keel_seo.freshness`` for why an
``auto_now`` timestamp can't be trusted for this). Renders in-process with the
Django test client -- no network, no running server required.

    python manage.py keel_seo_freshness              # process every indexable URL
    python manage.py keel_seo_freshness --dry-run     # report only, write nothing
    python manage.py keel_seo_freshness --url /pricing/
    python manage.py keel_seo_freshness --quiet       # summary line only

Exit code is non-zero only when a URL failed to render -- never merely because
content changed, so a normal deploy where pages legitimately changed still
exits 0.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from ...config import seo_setting
from ...freshness import FreshnessOutcome, record
from ...models import Landing


def _client_server_name() -> str:
    # A bare Client() defaults SERVER_NAME to "testserver", which a real
    # ALLOWED_HOSTS (no wildcard) would reject with DisallowedHost outside of
    # Django's own test runner (only django.test.utils.setup_test_environment
    # adds "testserver" to ALLOWED_HOSTS automatically). Match an allowed host
    # instead so this command works unmodified in production.
    allowed = list(getattr(settings, "ALLOWED_HOSTS", None) or [])
    if allowed and allowed[0] != "*":
        return allowed[0]
    return "testserver"


class Command(BaseCommand):
    help = "Render every indexable Landing URL and record its content-freshness hash."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change; write nothing.",
        )
        parser.add_argument(
            "--url",
            dest="single_url",
            default=None,
            help="Only process this one indexable Landing URL.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress per-URL output; print only the summary.",
        )

    def handle(self, *args, **options):
        if not seo_setting("freshness_enabled"):
            self.stdout.write(
                "keel_seo freshness is disabled (KEEL_SEO['freshness_enabled'] "
                "is False) -- nothing to do."
            )
            return

        dry_run = options["dry_run"]
        quiet = options["quiet"]
        single_url = options["single_url"]

        queryset = Landing.objects.filter(is_indexable=True).order_by("url")
        if single_url:
            queryset = queryset.filter(url=single_url)
            if not queryset.exists():
                raise CommandError(
                    f"{single_url!r} is not an indexable Landing URL"
                )

        client = Client(SERVER_NAME=_client_server_name())
        counts = {"created": 0, "changed": 0, "unchanged": 0, "failed": 0}
        changed_urls = []
        failed_urls = []

        for landing in queryset:
            try:
                response = client.get(landing.url, follow=True)
                if response.status_code >= 400:
                    raise ValueError(f"HTTP {response.status_code}")
                html = response.content.decode(response.charset or "utf-8")
                result = record(landing.url, html, dry_run=dry_run)
                outcome = result.outcome
            except Exception as exc:  # noqa: BLE001 -- one bad URL must not abort the run
                counts["failed"] += 1
                failed_urls.append(landing.url)
                if not quiet:
                    self.stderr.write(self.style.ERROR(f"FAILED    {landing.url}: {exc}"))
                continue

            counts[outcome.value] += 1
            if outcome is not FreshnessOutcome.UNCHANGED:
                changed_urls.append(landing.url)
            if not quiet:
                self.stdout.write(f"{outcome.value:<9} {landing.url}")

        self.stdout.write("")
        summary = (
            f"created={counts['created']} changed={counts['changed']} "
            f"unchanged={counts['unchanged']} failed={counts['failed']}"
        )
        self.stdout.write(self.style.WARNING(summary) if dry_run else self.style.SUCCESS(summary))

        if changed_urls:
            self.stdout.write("Changed URLs:")
            for url in changed_urls:
                self.stdout.write(f"  {url}")

        if failed_urls:
            raise CommandError(
                f"{len(failed_urls)} URL(s) failed to render: {', '.join(failed_urls)}"
            )
