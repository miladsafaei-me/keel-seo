"""Enforce "one query intent, one canonical URL" against the live Landing table.

Safe to run on every deploy: with no ``KEEL_SEO["intent_registry_hook"]`` configured
the registry is empty and the command is a no-op that exits 0.

    python manage.py keel_seo_intent_check                # report; exit 0 always
    python manage.py keel_seo_intent_check --strict       # exit 1 on any violation
    python manage.py keel_seo_intent_check --coverage     # indexable URLs not declared
    python manage.py keel_seo_intent_check --json         # machine-readable report

``--strict`` is what belongs in a deploy script and in CI. The plain form is what you
run while editing the registry, because it prints every violation instead of dying on
the first one.
"""
import json

from django.core.management.base import BaseCommand

from ...intent import check, coverage, load_registry


class Command(BaseCommand):
    help = "Check the intent registry: one query intent, one canonical indexable URL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero when any violation is found.",
        )
        parser.add_argument(
            "--coverage",
            action="store_true",
            help="Also list indexable URLs no registry entry mentions.",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the report as JSON instead of prose.",
        )

    def handle(self, *args, **options):
        registry = load_registry()
        violations = check(registry)
        uncovered = coverage(registry) if options["coverage"] else []

        if options["as_json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "intents": len(registry.intents),
                        "violations": [
                            {
                                "code": v.code,
                                "key": v.key,
                                "url": v.url,
                                "message": v.message,
                            }
                            for v in violations
                        ],
                        "uncovered": uncovered,
                    },
                    indent=2,
                )
            )
        else:
            self.stdout.write(f"intent registry: {len(registry.intents)} declared intents")
            if not registry.intents:
                self.stdout.write(
                    "  no registry configured (KEEL_SEO['intent_registry_hook'] unset)"
                )
            for violation in violations:
                self.stdout.write(self.style.ERROR(f"  {violation}"))
            if not violations:
                self.stdout.write(self.style.SUCCESS("  no violations"))
            if options["coverage"]:
                self.stdout.write(f"\nindexable URLs with no declared intent: {len(uncovered)}")
                for url in uncovered:
                    self.stdout.write(f"  {url}")

        if violations and options["strict"]:
            raise SystemExit(1)
