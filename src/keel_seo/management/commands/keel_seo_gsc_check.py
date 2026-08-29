"""Diagnose the whole Search Console setup from inside Django.

Same checks as ``python -m keel_seo.gsc check``, run with the project's settings
loaded so it also validates what the host configured rather than only what the
environment exports. Exits non-zero when any capability fails, so a deploy can gate
on it.

    python manage.py keel_seo_gsc_check
    python manage.py keel_seo_gsc_check --site sc-domain:example.com
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from ...gsc import check


class Command(BaseCommand):
    help = "Check credentials, property permissions and every Search Console capability."

    def add_arguments(self, parser):
        parser.add_argument("--site", default="", help="property to check (default the configured one)")
        parser.add_argument("--strict", action="store_true", help="exit non-zero on any failure")

    def handle(self, *args, **options):
        results = check.run_checks(options["site"])
        self.stdout.write(check.format_report(results))
        if options["strict"] and any(r["status"] == check.FAILED for r in results):
            sys.exit(1)
