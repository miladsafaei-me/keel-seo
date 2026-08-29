"""List, submit or delete this property's sitemaps through the Search Console API.

Submitting on every deploy is the cheap habit worth having: it is idempotent, costs
no quota worth counting, and re-queues a sitemap Google may not have re-downloaded
since the last content release. ``--auto`` submits the site's own
``/sitemap.xml`` without needing it spelled out in the deploy script.

    python manage.py keel_seo_gsc_sitemaps                         # list
    python manage.py keel_seo_gsc_sitemaps --submit https://example.com/sitemap.xml
    python manage.py keel_seo_gsc_sitemaps --auto                  # submit <origin>/sitemap.xml
    python manage.py keel_seo_gsc_sitemaps --delete https://example.com/old.xml
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ...gsc import sitemaps
from ...gsc.auth import GscError, resolve_site


class Command(BaseCommand):
    help = "List, submit or delete the property's sitemaps."

    def add_arguments(self, parser):
        parser.add_argument("--site", default="", help="property (default $GSC_SITE / KEEL_SEO['gsc_site'])")
        parser.add_argument("--submit", default="", help="sitemap URL to submit or re-submit")
        parser.add_argument("--auto", action="store_true", help="submit <property origin>/sitemap.xml")
        parser.add_argument("--delete", default="", help="sitemap URL to unregister")
        parser.add_argument("--index", default="", help="list the children of this sitemap index")

    def handle(self, *args, **options):
        try:
            site = resolve_site(options["site"])
            if options["delete"]:
                sitemaps.delete_sitemap(options["delete"], site)
                self.stdout.write(f"deleted {options['delete']} from {site}")
                return
            target = options["submit"] or (_origin(site) + "/sitemap.xml" if options["auto"] else "")
            if target:
                sitemaps.submit_sitemap(target, site)
                self.stdout.write(f"submitted {target} to {site}")
                return
            entries = [sitemaps.summarize(e) for e in sitemaps.list_sitemaps(site, sitemap_index=options["index"])]
        except GscError as exc:
            raise CommandError(str(exc))

        if not entries:
            self.stdout.write(f"{site}: no sitemaps registered")
            return
        self.stdout.write(f"{site}: {len(entries)} sitemap(s)")
        for e in entries:
            flags = []
            if e["is_index"]:
                flags.append("index")
            if e["is_pending"]:
                flags.append("pending")
            if e["errors"]:
                flags.append(f"{e['errors']} errors")
            if e["warnings"]:
                flags.append(f"{e['warnings']} warnings")
            self.stdout.write(
                f"  {e['path']}\n"
                f"    downloaded {e['last_downloaded'] or 'never'}   "
                f"submitted URLs {e['submitted']}   indexed {e['indexed']}"
                + (f"   [{', '.join(flags)}]" if flags else "")
            )


def _origin(site: str) -> str:
    if site.startswith("sc-domain:"):
        return f"https://{site.split(':', 1)[1].strip('/')}"
    return site.rstrip("/")
