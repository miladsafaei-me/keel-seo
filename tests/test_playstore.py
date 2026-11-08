"""keel_seo.playstore: parsing, expansion and scoring, with no network."""
from __future__ import annotations

import json
import time
import unittest

from keel_seo.playstore.client import parse_suggest
from keel_seo.playstore.complaints import complaint_terms
from keel_seo.playstore.expand import expand
from keel_seo.playstore.score import is_relevant, score_keyword, tokens


def _batch_body(phrases):
    rows = [[p, None, [None] * 5] for p in phrases]
    inner = json.dumps([[rows, ["x"]]])
    chunk = json.dumps([["wrb.fr", "IJ4APc", inner, None, None, None, "generic"]])
    return f")]}}'\n\n{len(chunk)}\n{chunk}\n26\n[[\"e\",4]]\n"


class FakeClient:
    def __init__(self, table):
        self.table = table
        self.asked = []

    def suggest(self, term):
        self.asked.append(term)
        return self.table.get(term, [])


class ParseSuggestTests(unittest.TestCase):
    def test_reads_phrases(self):
        body = _batch_body(["prop firm", "prop firm trading"])
        self.assertEqual(parse_suggest(body), ["prop firm", "prop firm trading"])

    def test_garbage_is_empty(self):
        self.assertEqual(parse_suggest(")]}'\nnot json"), [])


class ExpandTests(unittest.TestCase):
    def test_alphabet_then_each_phrase(self):
        client = FakeClient({"seed a": ["seed app"], "seed app": ["seed app pro"]})
        phrases = [p.text for p in expand(client, ["Seed"], progress=lambda m: None)]
        self.assertEqual(phrases[0], "seed")
        self.assertIn("seed app", phrases)
        self.assertIn("seed app pro", phrases)
        self.assertIn("seed z", client.asked)
        self.assertEqual(len(client.asked), len(set(client.asked)))


def _app(title, installs, score, days_ago, summary=""):
    return {"title": title, "summary": summary, "realInstalls": installs,
            "score": score, "updated": time.time() - days_ago * 86400,
            "free": True, "offersIAP": False}


class ScoreTests(unittest.TestCase):
    def test_unrelated_giants_do_not_count_as_demand(self):
        apps = [_app("LinkedIn", 2_000_000_000, 3.8, 3),
                _app("Prop Firm Calculator", 50, None, 400)]
        result = score_keyword("prop firm calculator", apps)
        self.assertEqual(result.installs_total, 50)
        self.assertEqual(len(result.relevant), 1)

    def test_no_relevant_app_is_unserved(self):
        result = score_keyword("aquarium log", [_app("Weather", 10_000, 4.5, 2)])
        self.assertTrue(result.unserved)
        self.assertEqual(result.opportunity, 0)

    def test_weak_field_beats_strong_field(self):
        weak = [_app("Fish Tank Log", 200_000, 3.2, 700) for _ in range(5)]
        strong = [_app("Fish Tank Log", 5_000_000, 4.7, 10) for _ in range(5)]
        self.assertGreater(score_keyword("fish tank log", weak).weakness,
                           score_keyword("fish tank log", strong).weakness)

    def test_relevance_needs_half_the_words(self):
        wanted = tokens("prop firm challenge tracker")
        self.assertTrue(is_relevant(wanted, {"title": "Prop Challenge Helper"}))
        self.assertFalse(is_relevant(wanted, {"title": "Daily Tracker"}))


class ComplaintTests(unittest.TestCase):
    def test_counts_once_per_review(self):
        reviews = [{"text": "too many ads, too many ads"}, {"text": "many ads again"},
                   {"text": "great"}]
        terms = dict(complaint_terms(reviews))
        self.assertEqual(terms.get("many ads"), 2)


class IdeaTests(unittest.TestCase):
    def _scored(self, phrase, apps):
        return score_keyword(phrase, apps)

    def test_selling_apps_raise_monetization(self):
        from keel_seo.playstore.ideas import app_opportunity
        paid = _app("Fish Tank Log", 50_000, 3.5, 30)
        paid.update(offersIAP=True, inAppProductPrice="$4.99 - $29.99 per item")
        free = _app("Fish Tank Log", 50_000, 3.5, 30)
        a = app_opportunity(self._scored("fish tank log", [paid]), {"fish", "tank"})
        b = app_opportunity(self._scored("fish tank log", [free]), {"fish", "tank"})
        self.assertGreater(a["app_score"], b["app_score"])
        self.assertEqual(a["max_price"], 29.99)

    def test_word_no_listing_uses_is_a_brand(self):
        from keel_seo.playstore.ideas import brand_word
        apps = [_app("Prop Firm Tracker", 100, None, 3, "track your prop firm challenge")]
        self.assertEqual(brand_word("acmefunds prop firm", apps, {"prop", "firm"}), "acmefunds")
        self.assertEqual(brand_word("prop firm challenge", apps, {"prop", "firm"}), "")
        self.assertEqual(brand_word("prop firm challenge", apps, {"prop", "firm"},
                                    frozenset({"challenge"})), "challenge")

    def test_same_apps_make_one_idea(self):
        from keel_seo.playstore.ideas import group_ideas
        rows = [{"phrase": "a b", "app_score": 50}, {"phrase": "a b c", "app_score": 40},
                {"phrase": "x y", "app_score": 30}]
        ideas = group_ideas(rows, {"a b": ["1", "2", "3"], "a b c": ["1", "2", "4"],
                                   "x y": ["7", "8"]})
        self.assertEqual([i["idea"] for i in ideas], ["a b", "x y"])
        self.assertEqual(ideas[0]["phrases_count"], 2)
