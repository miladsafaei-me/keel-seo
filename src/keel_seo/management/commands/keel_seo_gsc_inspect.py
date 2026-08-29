"""Inspect URLs with the URL Inspection API and store what Google reports.

The point of a sweep — rather than inspecting one URL at a time in the browser — is
the *gap* it exposes: which pages we consider indexable and Google does not, which
pages Google canonicalises somewhere else, which pages it has never crawled. That gap
is only visible when the whole indexable set is checked against one property.

The daily quota is 2,000 inspections per property, so the default target is not "every
URL" but "every URL whose stored reading is older than --stale-days", newest gap
first. A nightly run over a large site therefore walks its backlog instead of
re-reading the same first 2,000 URLs forever.

    python manage.py keel_seo_gsc_inspect --url /pricing/
    python manage.py keel_seo_gsc_inspect --all-indexable --limit 500
    python manage.py keel_seo_gsc_inspect --all-indexable --stale-days 30
    python manage.py keel_seo_gsc_inspect --sitemap https://example.com/sitemap.xml
    python manage.py keel_seo_gsc_inspect --not-indexed        # re-check known gaps
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...gsc import inspection
from ...gsc.auth import GscError, resolve_site
from ...models import Landing, UrlInspection

SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


class Command(BaseCommand):
    help = "Inspect URLs with the Search Console URL Inspection API and store the results."

    def add_arguments(self, parser):
        parser.add_argument("--site", default="", help="property (default $GSC_SITE / KEEL_SEO['gsc_site'])")
        parser.add_argument("--base-url", default="", help="origin to prepend to Landing paths")
        parser.add_argument("--url", action="append", default=[],
                            help="a URL or Landing path to inspect; repeatable")
        parser.add_argument("--all-indexable", action="store_true",
                            help="inspect every indexable Landing URL")
        parser.add_argument("--sitemap", default="", help="inspect every <loc> in this sitemap")
        parser.add_argument("--not-indexed", action="store_true",
                            help="re-inspect only URLs a previous run found not indexed")
        parser.add_argument("--stale-days", type=int, default=0,
                            help="skip URLs inspected within this many days")
        parser.add_argument("--limit", type=int, default=inspection.DAILY_QUOTA,
                            help=f"max inspections this run (daily quota is {inspection.DAILY_QUOTA})")
        parser.add_argument("--no-save", action="store_true", help="report only, write nothing")
        parser.add_argument("--json", dest="json_out", help="also write every raw result here")
        parser.add_argument("--quiet", action="store_true", help="summary only")

    def handle(self, *args, **options):
        try:
            site = resolve_site(options["site"])
        except GscError as exc:
            raise CommandError(str(exc))

        base = (options["base_url"] or _default_base(site)).rstrip("/")
        urls = self._collect(options, site, base)
        if not urls:
            raise CommandError(
                "no URLs to inspect. Pass --url, --all-indexable, --sitemap or --not-indexed."
            )

        limit = min(options["limit"], inspection.DAILY_QUOTA)
        if len(urls) > limit:
            self.stdout.write(
                f"{len(urls)} URLs selected; inspecting the first {limit} "
                f"(quota {inspection.DAILY_QUOTA}/day per property). Re-run to continue."
            )
            urls = urls[:limit]

        save = not options["no_save"]
        quiet = options["quiet"]
        entries = []

        def handle_result(entry):
            entries.append(entry)
            if save and entry["ok"]:
                _store(site, entry)
            if quiet:
                return
            if entry["ok"]:
                summary = entry["summary"]
                mark = "indexed" if summary["indexed"] else "NOT INDEXED"
                extra = " canonical-mismatch" if summary["canonical_mismatch"] else ""
                self.stdout.write(
                    f"{mark:<12} {entry['url']}  ({summary['coverage_state'] or 'no coverage state'}){extra}"
                )
            else:
                self.stderr.write(f"FAILED       {entry['url']}: {entry['error']}")

        try:
            inspection.inspect_urls(urls, site, max_calls=limit, on_result=handle_result)
        except GscError as exc:
            raise CommandError(str(exc))

        if options["json_out"]:
            with open(options["json_out"], "w") as handle:
                json.dump(entries, handle, indent=2, default=str)
            self.stdout.write(f"wrote {options['json_out']}")

        self._report(inspection.coverage_report(entries), site, saved=save)

    def _collect(self, options, site: str, base: str) -> list:
        urls: list = []
        for raw in options["url"]:
            urls.append(raw if raw.startswith(("http://", "https://")) else base + raw)
        if options["all_indexable"]:
            urls.extend(
                base + row for row in
                Landing.objects.filter(is_indexable=True).order_by("url").values_list("url", flat=True)
            )
        if options["sitemap"]:
            urls.extend(_sitemap_urls(options["sitemap"]))
        if options["not_indexed"]:
            urls.extend(
                UrlInspection.objects.filter(site=site, indexed=False)
                .order_by("fetched_at")
                .values_list("url", flat=True)
            )

        seen = set()
        deduped = [u for u in urls if not (u in seen or seen.add(u))]

        if options["stale_days"]:
            cutoff = timezone.now() - dt.timedelta(days=options["stale_days"])
            fresh = set(
                UrlInspection.objects.filter(site=site, fetched_at__gte=cutoff)
                .values_list("url", flat=True)
            )
            deduped = [u for u in deduped if u not in fresh]

        # Never-inspected URLs first, then the oldest reading — a truncated run then
        # spends its quota on the URLs we know least about.
        ages = dict(
            UrlInspection.objects.filter(site=site, url__in=deduped).values_list("url", "fetched_at")
        )
        epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return sorted(deduped, key=lambda u: ages.get(u, epoch))

    def _report(self, report, site: str, *, saved: bool) -> None:
        self.stdout.write("")
        self.stdout.write(f"{site}: {report['total']} inspected, {report['failed']} failed")
        self.stdout.write(
            f"  indexed {report['indexed']}   not indexed {report['not_indexed']}   "
            f"canonical mismatch {report['canonical_mismatch']}   "
            f"robots blocked {report['robots_blocked']}   fetch problems {report['fetch_problem']}"
        )
        for state, count in sorted(report["coverage_states"].items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {count:>5}  {state}")
        if saved:
            self.stdout.write("  results stored in keel_seo.UrlInspection")


def _default_base(site: str) -> str:
    if site.startswith("sc-domain:"):
        return f"https://{site.split(':', 1)[1].strip('/')}"
    return site.rstrip("/")


def _sitemap_urls(sitemap_url: str) -> list:
    """Every ``<loc>`` in a sitemap, following one level of sitemap index.

    Parsed with a regex rather than an XML parser on purpose: this reads our own
    deployed sitemap over HTTP, the only thing needed from it is the loc list, and a
    strict parser turns a single stray character in a 40,000-URL file into a total
    failure instead of a near-complete list.
    """
    with urllib.request.urlopen(sitemap_url, timeout=60) as response:
        body = response.read().decode("utf-8", "replace")
    locs = SITEMAP_LOC.findall(body)
    if "<sitemapindex" in body.lower():
        nested = []
        for child in locs:
            nested.extend(_sitemap_urls(child))
        return nested
    return locs


def _store(site: str, entry: dict) -> None:
    summary = entry["summary"]
    UrlInspection.objects.update_or_create(
        site=site,
        url=entry["url"],
        defaults={
            "fetched_at": timezone.now(),
            "verdict": summary["verdict"][:20],
            "coverage_state": summary["coverage_state"][:255],
            "indexing_state": summary["indexing_state"][:64],
            "robots_txt_state": summary["robots_txt_state"][:64],
            "page_fetch_state": summary["page_fetch_state"][:64],
            "crawled_as": summary["crawled_as"][:64],
            "last_crawl_time": _parse_time(summary["last_crawl_time"]),
            "google_canonical": summary["google_canonical"][:500],
            "user_canonical": summary["user_canonical"][:500],
            "canonical_mismatch": summary["canonical_mismatch"],
            "indexed": summary["indexed"],
            "mobile_verdict": summary["mobile_verdict"][:20],
            "rich_results_verdict": summary["rich_results_verdict"][:20],
            "amp_verdict": summary["amp_verdict"][:20],
            "raw": entry["result"],
        },
    )


def _parse_time(value: str):
    """Google returns RFC 3339 with a trailing Z, which fromisoformat rejects before
    Python 3.11. Normalise it rather than depending on the interpreter version."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
