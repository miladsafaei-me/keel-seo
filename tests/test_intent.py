"""Tests for keel_seo.intent -- the one-intent-one-URL gate.

Run: DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests.test_intent
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from keel_seo.intent import build_registry, check, coverage, load_registry
from keel_seo.models import Landing

PILLAR = "/instruments/high-low"
TERM = "/tag/high-low-contract"


def payload(**overrides):
    row = {
        "key": "contract.high-low@what-is",
        "entity": "contract.high-low",
        "frame": "what-is",
        "owner": PILLAR,
        "label": "What a high/low binary option is",
        "defers": [],
        "retired": [TERM],
    }
    row.update(overrides)
    return {"intents": [row]}


class BuildRegistryTests(TestCase):
    """Parsing is pure -- no settings, no database."""

    def test_accepts_a_bare_list(self):
        registry = build_registry(payload()["intents"])
        self.assertEqual(len(registry.intents), 1)
        self.assertEqual(registry.intents[0].owner, PILLAR)

    def test_normalizes_keys_and_frames_to_lower_case(self):
        registry = build_registry(payload(key="Contract.High-Low@What-Is", frame="What-Is"))
        self.assertEqual(registry.intents[0].key, "contract.high-low@what-is")
        self.assertEqual(registry.intents[0].frame, "what-is")

    def test_entity_families_resolve_aliases_to_one_canonical_entity(self):
        registry = build_registry(
            {
                "intents": [],
                "entity_families": {"contract.turbo": ["contract.60-second"]},
            }
        )
        self.assertEqual(registry.family_of("contract.60-second"), "contract.turbo")
        self.assertEqual(registry.family_of("contract.ladder"), "contract.ladder")

    def test_lookups_ignore_a_trailing_slash(self):
        registry = build_registry(payload(defers=[TERM], retired=[]))
        self.assertEqual(len(registry.owned_by(PILLAR + "/")), 1)
        self.assertEqual(registry.canonical_owner_for(TERM + "/"), PILLAR)

    def test_canonical_owner_is_empty_for_an_unrelated_url(self):
        self.assertEqual(build_registry(payload()).canonical_owner_for("/blog/"), "")


class CheckTests(TestCase):
    """The invariants, exercised against an injected landing map."""

    def test_a_retired_page_that_is_really_gone_is_clean(self):
        """The end state of a resolved collision: the page 301s and has no row."""
        self.assertEqual(check(build_registry(payload()), landings={PILLAR: True}), [])

    def test_a_retired_page_whose_row_came_back_is_flagged(self):
        landings = {PILLAR: True, TERM: False}
        codes = [v.code for v in check(build_registry(payload()), landings=landings)]
        self.assertEqual(codes, ["retired-still-present"])

    def test_a_live_spoke_must_be_noindex(self):
        rows = payload(retired=[], defers=[TERM])
        landings = {PILLAR: True, TERM: False}
        self.assertEqual(check(build_registry(rows), landings=landings), [])

    def test_indexable_deferral_is_the_headline_violation(self):
        rows = payload(retired=[], defers=[TERM])
        landings = {PILLAR: True, TERM: True}
        codes = [v.code for v in check(build_registry(rows), landings=landings)]
        self.assertEqual(codes, ["deferral-indexable"])

    def test_a_deferral_with_no_row_at_all_is_flagged_as_stale(self):
        rows = payload(retired=[], defers=[TERM])
        codes = [v.code for v in check(build_registry(rows), landings={PILLAR: True})]
        self.assertEqual(codes, ["deferral-missing"])

    def test_owner_must_itself_be_indexable(self):
        landings = {PILLAR: False}
        codes = [v.code for v in check(build_registry(payload()), landings=landings)]
        self.assertEqual(codes, ["owner-noindex"])

    def test_owner_without_a_landing_row_is_flagged(self):
        codes = [v.code for v in check(build_registry(payload(retired=[])), landings={})]
        self.assertEqual(codes, ["owner-missing"])

    def test_malformed_key_is_flagged(self):
        rows = payload(key="high low", retired=[])
        codes = [v.code for v in check(build_registry(rows), landings={PILLAR: True})]
        self.assertEqual(codes, ["key-shape"])

    def test_duplicate_key_is_flagged(self):
        rows = payload(retired=[])["intents"] * 2
        codes = [v.code for v in check(build_registry(rows), landings={PILLAR: True})]
        self.assertIn("duplicate-key", codes)

    def test_two_keys_for_one_entity_and_frame_collide_through_the_family_net(self):
        rows = {
            "intents": [
                payload(retired=[])["intents"][0],
                {
                    "key": "contract.60-second@what-is",
                    "entity": "contract.60-second",
                    "frame": "what-is",
                    "owner": "/instruments/turbo-60-second",
                    "defers": [],
                },
            ],
            "entity_families": {"contract.high-low": ["contract.60-second"]},
        }
        landings = {PILLAR: True, "/instruments/turbo-60-second": True}
        codes = [v.code for v in check(build_registry(rows), landings=landings)]
        self.assertEqual(codes, ["aliased-intent"])

    def test_distinct_entities_in_the_same_frame_do_not_collide(self):
        rows = [
            payload(retired=[])["intents"][0],
            {
                "key": "contract.ladder@what-is",
                "entity": "contract.ladder",
                "frame": "what-is",
                "owner": "/instruments/ladder",
            },
        ]
        landings = {PILLAR: True, "/instruments/ladder": True}
        self.assertEqual(check(build_registry(rows), landings=landings), [])

    def test_self_deferral_is_flagged(self):
        rows = payload(defers=[PILLAR], retired=[])
        codes = [v.code for v in check(build_registry(rows), landings={PILLAR: True})]
        self.assertEqual(codes, ["deferral-is-owner"])


class CoverageTests(TestCase):
    def test_lists_indexable_urls_no_entry_mentions(self):
        landings = {PILLAR: True, "/guide/money-management": True}
        self.assertEqual(
            coverage(build_registry(payload()), landings=landings),
            ["/guide/money-management"],
        )


class HookTests(TestCase):
    def test_no_hook_means_an_empty_registry_and_no_enforcement(self):
        self.assertEqual(load_registry().intents, ())
        self.assertEqual(check(load_registry(), landings={PILLAR: True}), [])


def _registry_hook():
    return payload()


@override_settings(KEEL_SEO={"intent_registry_hook": "tests.test_intent._registry_hook"})
class CommandTests(TestCase):
    def test_command_reports_the_violation_and_strict_exits_non_zero(self):
        Landing.objects.create(title="Pillar", url=PILLAR, is_indexable=True)
        Landing.objects.create(title="Term", url=TERM, is_indexable=False)
        out = StringIO()
        call_command("keel_seo_intent_check", stdout=out)
        self.assertIn("retired-still-present", out.getvalue())
        with self.assertRaises(SystemExit):
            call_command("keel_seo_intent_check", "--strict", stdout=StringIO())

    def test_clean_site_passes_strict(self):
        Landing.objects.create(title="Pillar", url=PILLAR, is_indexable=True)
        out = StringIO()
        call_command("keel_seo_intent_check", "--strict", "--coverage", stdout=out)
        self.assertIn("no violations", out.getvalue())
