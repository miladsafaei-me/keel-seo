"""Submit URLs to the Indexing API — request (re)crawl, or notify removal.

The daily quota is 200 publishes per Cloud *project*, shared by every property on the
same service-account key, so this command is deliberately conservative: it defaults to
a dry run's worth of caution by refusing to spend quota on a URL it already notified
within ``--cooldown-hours``, and it logs every submission to ``IndexingSubmission`` so
the next run can see what the last one spent.

    python manage.py keel_seo_gsc_index --url /blog/new-post/
    python manage.py keel_seo_gsc_index --all-indexable --limit 50
    python manage.py keel_seo_gsc_index --changed-since 3        # freshness-driven
    python manage.py keel_seo_gsc_index --url /old-page/ --remove
    python manage.py keel_seo_gsc_index --url /old-page/ --removal-guidance

``--remove`` sends URL_DELETED, which only accelerates a re-crawl: Google drops the
page once it confirms a 404/410 or a noindex, and never because we asked. The Search
Console Removals tool (the temporary block) has no API — ``--removal-guidance``
prints the full durable path rather than pretending otherwise.
"""
from __future__ import annotations

import datetime as dt
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...gsc import indexing
from ...gsc.auth import GscError, resolve_site
from ...models import IndexingSubmission, Landing


class Command(BaseCommand):
    help = "Notify Google's Indexing API that URLs were updated or removed."

    def add_arguments(self, parser):
        parser.add_argument("--site", default="", help="property (default $GSC_SITE / KEEL_SEO['gsc_site'])")
        parser.add_argument("--base-url", default="", help="origin to prepend to Landing paths")
        parser.add_argument("--url", action="append", default=[],
                            help="a URL or Landing path to notify; repeatable")
        parser.add_argument("--all-indexable", action="store_true",
                            help="notify every indexable Landing URL")
        parser.add_argument("--changed-since", type=int, default=0, metavar="DAYS",
                            help="notify Landings whose content genuinely changed in the last N days "
                                 "(keel_seo.freshness's content_modified_at, not updated_at)")
        parser.add_argument("--remove", action="store_true", help="send URL_DELETED instead of URL_UPDATED")
        parser.add_argument("--removal-guidance", action="store_true",
                            help="print the correct removal path for --url and exit without submitting")
        parser.add_argument("--cooldown-hours", type=int, default=24,
                            help="skip URLs already notified successfully within this many hours")
        parser.add_argument("--limit", type=int, default=indexing.DAILY_QUOTA,
                            help=f"max notifications this run (quota {indexing.DAILY_QUOTA}/day per Cloud project)")
        parser.add_argument("--dry-run", action="store_true", help="list what would be sent, send nothing")

    def handle(self, *args, **options):
        try:
            site = resolve_site(options["site"])
        except GscError as exc:
            raise CommandError(str(exc))
        base = (options["base_url"] or _default_base(site)).rstrip("/")

        urls = self._collect(options, base)
        if not urls:
            raise CommandError(
                "no URLs selected. Pass --url, --all-indexable or --changed-since."
            )

        if options["removal_guidance"]:
            for url in urls:
                self.stdout.write(json.dumps(indexing.removal_guidance(url), indent=2))
            return

        type_ = indexing.URL_DELETED if options["remove"] else indexing.URL_UPDATED

        if options["cooldown_hours"]:
            cutoff = timezone.now() - dt.timedelta(hours=options["cooldown_hours"])
            recent = set(
                IndexingSubmission.objects.filter(
                    url__in=urls, notification_type=type_, ok=True, submitted_at__gte=cutoff
                ).values_list("url", flat=True)
            )
            skipped = len(recent)
            urls = [u for u in urls if u not in recent]
            if skipped:
                self.stdout.write(
                    f"skipping {skipped} URL(s) already notified within "
                    f"{options['cooldown_hours']}h (--cooldown-hours 0 to override)"
                )

        limit = min(options["limit"], indexing.DAILY_QUOTA)
        if len(urls) > limit:
            self.stdout.write(
                f"{len(urls)} URLs selected; sending the first {limit} "
                f"(quota {indexing.DAILY_QUOTA}/day per Cloud project). Re-run tomorrow to continue."
            )
            urls = urls[:limit]

        if options["dry_run"]:
            for url in urls:
                self.stdout.write(f"would notify {type_}  {url}")
            self.stdout.write(f"\n{len(urls)} notification(s) — nothing sent (--dry-run)")
            return

        def handle_result(entry):
            IndexingSubmission.objects.create(
                site=site,
                url=entry["url"],
                notification_type=entry["type"],
                submitted_at=timezone.now(),
                ok=entry["ok"],
                error=entry.get("error", ""),
                response=entry.get("response", {}) or {},
            )
            if entry["ok"]:
                self.stdout.write(f"notified {entry['type']}  {entry['url']}")
            else:
                self.stderr.write(f"FAILED   {entry['url']}: {entry['error']}")

        results = indexing.notify_urls(urls, type_, max_calls=limit, on_result=handle_result)
        ok = sum(1 for r in results if r["ok"])
        self.stdout.write("")
        self.stdout.write(f"{ok}/{len(results)} notified as {type_} for {site}")
        if type_ == indexing.URL_DELETED:
            self.stdout.write(
                "URL_DELETED only accelerates the confirming crawl — the URL must already "
                "return 404/410 or noindex. Run with --removal-guidance for the full path."
            )
        if ok:
            self.stdout.write(
                "Verify with: manage.py keel_seo_gsc_inspect --url <path> (allow a few days)"
            )

    def _collect(self, options, base: str) -> list:
        urls: list = []
        for raw in options["url"]:
            urls.append(raw if raw.startswith(("http://", "https://")) else base + raw)
        if options["all_indexable"]:
            urls.extend(
                base + path for path in
                Landing.objects.filter(is_indexable=True).order_by("url").values_list("url", flat=True)
            )
        if options["changed_since"]:
            cutoff = timezone.now() - dt.timedelta(days=options["changed_since"])
            urls.extend(
                base + path for path in
                Landing.objects.filter(is_indexable=True, content_modified_at__gte=cutoff)
                .order_by("-content_modified_at")
                .values_list("url", flat=True)
            )
        seen = set()
        return [u for u in urls if not (u in seen or seen.add(u))]


def _default_base(site: str) -> str:
    if site.startswith("sc-domain:"):
        return f"https://{site.split(':', 1)[1].strip('/')}"
    return site.rstrip("/")
