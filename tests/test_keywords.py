"""Tests for keel_seo.keywords -- the autocomplete keyword-universe crawler.

Plain unittest, no Django and no network: the collector is driven by a stub that
replays canned responses, so the crawl, the scoring and the clustering are all
tested against fixed inputs.

Run: DJANGO_SETTINGS_MODULE=tests.settings python -m django test tests.test_keywords
     python -m unittest tests.test_keywords
"""
import os
import unittest

from keel_seo.keywords import cluster as clustering
from keel_seo.keywords.crawl import Universe, contains_seed, crawl, score, seed_tokens
from keel_seo.keywords.grammar import (BRANCH, DRILL, SEED, expansions,
                                       star_variants)
from keel_seo.keywords.proxying import accept_suggestions
from keel_seo.keywords.suggest import Response, SuggestCache, SuggestClient, Suggestion


class StubClient(SuggestClient):
    """A SuggestClient that answers from a dict instead of the network."""

    def __init__(self, answers, capacity=15):
        super().__init__(cache=SuggestCache(None))
        self.answers = answers
        self._capacity = capacity
        self.asked = []

    @property
    def capacity(self):
        return self._capacity

    def fetch(self, query):
        self.asked.append(query)
        phrases = self.answers.get(query, [])
        return Response(
            query=query,
            suggestions=tuple(
                Suggestion(p, i, 600 - i) for i, p in enumerate(phrases, 1)
            ),
            capacity=self._capacity,
        )


class GrammarTests(unittest.TestCase):
    def test_seed_tier_surrounds_the_term_on_both_sides(self):
        queries = set(expansions("quotex", SEED))
        self.assertIn("quotex a", queries, "spaced suffix sweep missing")
        self.assertIn("quotex 9", queries, "digit suffix missing")
        self.assertIn("quotexa", queries, "tight (no-space) suffix missing")
        self.assertIn("a quotex", queries, "spaced prefix sweep missing")
        self.assertIn("is quotex", queries, "prefix word missing")
        self.assertIn("quotex vs", queries, "suffix word missing")
        self.assertIn("quotex", queries, "the bare seed must be asked too")

    def test_a_star_walks_every_gap_between_words(self):
        """The family that reaches phrases where the term is not the leading text."""
        self.assertEqual(star_variants("quotex signal bot"),
                         ["quotex * signal bot", "quotex signal * bot"])

    def test_a_single_word_term_has_no_gap_to_walk(self):
        self.assertEqual(star_variants("quotex"), [])

    def test_the_seed_tier_asks_everything_twice_with_a_leading_space(self):
        queries = expansions("quotex", SEED)
        self.assertIn(" quotex", queries, "a leading space is its own family")
        self.assertIn(" quotex a", queries)
        self.assertEqual(sum(1 for q in queries if q.startswith(" ")), len(queries) // 2)

    def test_a_trailing_space_is_never_asked(self):
        """Google trims it, so the query is byte-identical to the bare one."""
        for tier in (SEED, BRANCH, DRILL):
            self.assertFalse(any(q.endswith(" ") for q in expansions("quotex bot", tier)),
                             f"{tier} emitted a trailing-space query, which is a "
                             "duplicate request for identical data")

    def test_branch_phrases_get_star_variants(self):
        queries = expansions("quotex signal bot", BRANCH)
        self.assertIn("quotex * signal bot", queries)

    def test_tight_sweep_can_be_switched_off(self):
        self.assertNotIn("quotexa", expansions("quotex", SEED, tight=False))

    def test_branch_tier_is_cheaper_than_seed_tier(self):
        self.assertLess(
            len(expansions("quotex signal bot", BRANCH)),
            len(expansions("quotex signal bot", SEED)),
        )

    def test_drill_tier_is_the_alphabet_only(self):
        self.assertEqual(len(expansions("quotex app", DRILL)), 26)

    def test_no_query_is_asked_twice(self):
        queries = expansions("quotex", SEED)
        self.assertEqual(len(queries), len(set(queries)))

    def test_empty_term_yields_nothing(self):
        self.assertEqual(expansions("   ", SEED), [])


class ContainmentTests(unittest.TestCase):
    def test_compound_without_space_still_contains_the_seed(self):
        self.assertTrue(contains_seed("quotexapk", seed_tokens("quotex")))

    def test_multi_word_seed_matches_in_any_order(self):
        tokens = seed_tokens("pip value calculator")
        self.assertTrue(contains_seed("calculator for pip value", tokens))
        self.assertFalse(contains_seed("pip calculator", tokens))

    def test_off_topic_neighbour_is_rejected(self):
        self.assertFalse(contains_seed("how to quote on reddit", seed_tokens("quotex")))


class SaturationTests(unittest.TestCase):
    def test_full_response_is_saturated_and_short_one_is_not(self):
        full = Response("q", tuple(Suggestion(f"p{i}", i, 1) for i in range(15)), 15)
        short = Response("q", tuple(Suggestion(f"p{i}", i, 1) for i in range(4)), 15)
        self.assertTrue(full.saturated)
        self.assertFalse(short.saturated)

    def test_parser_reads_relevance_and_survives_its_absence(self):
        with_scores = SuggestClient._parse(
            ["q", ["a", "b"], ["", ""], [], {"google:suggestrelevance": [900, 800]}]
        )
        self.assertEqual(with_scores, [["a", 900], ["b", 800]])
        self.assertEqual(SuggestClient._parse(["q", ["a"]]), [["a", 0]])
        self.assertEqual(SuggestClient._parse(None), [])


class CrawlTests(unittest.TestCase):
    def test_only_seed_bearing_phrases_enter_the_universe(self):
        client = StubClient({"quotex": ["quotex login", "how to quote on reddit"]})
        universe = crawl("quotex", client, levels=0, saturate=0, budget=500)
        self.assertIn("quotex login", universe.phrases)
        self.assertNotIn("how to quote on reddit", universe.phrases)
        self.assertEqual(universe.off_seed["how to quote on reddit"], 1)

    def test_reach_counts_distinct_parent_queries(self):
        client = StubClient({
            "quotex": ["quotex app"],
            "quotex a": ["quotex app"],
            "quotex b": ["quotex app"],
        })
        universe = crawl("quotex", client, levels=0, saturate=0, budget=500)
        self.assertEqual(universe.phrases["quotex app"].reach, 3)

    def test_budget_is_a_hard_cap(self):
        client = StubClient({})
        universe = crawl("quotex", client, levels=2, saturate=1, budget=25)
        self.assertLessEqual(universe.queries_asked, 25)

    def test_drilling_happens_only_under_a_truncated_response(self):
        client = StubClient({"quotex": ["quotex a" + c for c in "abcd"]}, capacity=15)
        crawl("quotex", client, levels=0, saturate=1, budget=500)
        self.assertNotIn("quotex a", [q for q in client.asked if q.endswith(" a")][1:],
                         "a short response must not trigger a drill round")

    def test_a_deadline_stops_the_crawl_but_keeps_what_it_found(self):
        """An external kill loses everything; the crawl's own deadline loses nothing."""
        client = StubClient({"quotex": ["quotex login", "quotex app"]})
        universe = crawl("quotex", client, levels=2, saturate=0, budget=5000,
                         max_seconds=0.001)
        self.assertTrue(universe.timed_out)
        self.assertFalse(universe.exhausted)

    def test_no_deadline_by_default(self):
        client = StubClient({"quotex": ["quotex login"]})
        universe = crawl("quotex", client, levels=0, saturate=0, budget=500)
        self.assertFalse(universe.timed_out)

    def test_a_closed_universe_reports_itself_exhausted(self):
        client = StubClient({})
        universe = crawl("quotex", client, levels=1, saturate=0, budget=500)
        self.assertTrue(universe.exhausted)
        self.assertEqual(universe.unexpanded, 0)


class RateLimitTests(unittest.TestCase):
    """The endpoint answers 403 when pushed, and says nothing else about it."""

    def test_throttle_spaces_requests_across_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        import time

        from keel_seo.keywords.suggest import Throttle

        throttle = Throttle(rate=50.0)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: throttle.wait(), range(10)))
        self.assertGreaterEqual(time.monotonic() - started, 0.9 * 9 / 50.0)

    def test_zero_rate_disables_the_throttle(self):
        from keel_seo.keywords.suggest import Throttle

        self.assertEqual(Throttle(rate=0).interval, 0.0)

    def test_a_block_code_trips_the_breaker_and_a_failure_does_not(self):
        from keel_seo.keywords.suggest import BLOCK_CODES, BLOCK_LIMIT

        self.assertIn(403, BLOCK_CODES, "Google answers 403, not 429, for this endpoint")
        client = SuggestClient(cache=SuggestCache(None), rate=0)
        for _ in range(BLOCK_LIMIT - 1):
            client._note_block()
        self.assertFalse(client.blocked)
        client._note_block()
        self.assertTrue(client.blocked)

    def test_one_success_clears_an_isolated_run_of_blocks(self):
        client = SuggestClient(cache=SuggestCache(None), rate=0)
        client._note_block()
        client._note_block()
        client._note_success()
        client._note_block()
        self.assertFalse(client.blocked)

    def test_a_cache_from_another_country_is_not_reused(self):
        """Autocomplete answers by IP, so geography is part of a response's identity."""
        turkey = SuggestCache(None, egress="TR")
        germany = SuggestCache(None, egress="DE")
        self.assertNotEqual(turkey.key("chrome", "en", "", "quotex"),
                            germany.key("chrome", "en", "", "quotex"))
        turkey.put(turkey.key("chrome", "en", "", "quotex"), [["quotex login", 900]])
        self.assertIsNone(germany.get(germany.key("chrome", "en", "", "quotex")))

    def test_language_and_vertical_are_also_part_of_the_key(self):
        cache = SuggestCache(None, egress="TR")
        base = cache.key("chrome", "en", "", "quotex")
        self.assertNotEqual(base, cache.key("chrome", "pt-BR", "", "quotex"))
        self.assertNotEqual(base, cache.key("chrome", "en", "yt", "quotex"))
        self.assertNotEqual(base, cache.key("firefox", "en", "", "quotex"))

    def test_a_blocked_client_stops_making_requests(self):
        client = SuggestClient(cache=SuggestCache(None), rate=0)
        client.blocked = True
        response = client.fetch("quotex")
        self.assertEqual(response.error, "blocked")
        self.assertEqual(client.calls, 0)

    def test_a_blocked_crawl_keeps_what_it_already_collected(self):
        class Blocking(StubClient):
            def fetch(self, query):
                response = super().fetch(query)
                if len(self.asked) > 3:
                    self.blocked = True
                return response

        client = Blocking({"quotex": ["quotex login", "quotex app"]})
        universe = crawl("quotex", client, levels=2, saturate=0, budget=5000)
        self.assertTrue(universe.blocked)
        self.assertIn("quotex login", universe.phrases)
        self.assertLess(universe.queries_asked, 5000,
                        "a blocked crawl must stop, not spend the whole budget")


class WorkbookTests(unittest.TestCase):
    """The .xlsx is optional, and its absence must cost only the workbook."""

    def universe(self):
        from keel_seo.keywords.crawl import Phrase

        u = Universe(seed="quotex")
        for i, text in enumerate(["quotex login", "quotex login page", "quotex apk"]):
            phrase = Phrase(text, 1 + i, 600 - i, 0)
            phrase.parents = {f"q{i}"}
            u.phrases[text] = phrase
        u.off_seed = {"quotes meaning in urdu": 4}
        score(u)
        return u

    def test_the_other_three_formats_survive_a_missing_openpyxl(self):
        import builtins
        import tempfile

        from keel_seo.keywords import report

        universe = self.universe()
        clusters = clustering.build(universe)
        real_import = builtins.__import__

        def no_openpyxl(name, *args, **kwargs):
            if name.startswith("openpyxl"):
                raise ImportError("simulated: openpyxl not installed")
            return real_import(name, *args, **kwargs)

        # Real metadata, not a stub: write_markdown reads a dozen fields and a
        # half-filled dict would fail this test for the wrong reason.
        meta = report.metadata(universe, clusters,
                               {"ip": "1.2.3.4", "country": "TR", "org": "test"},
                               SuggestClient(cache=SuggestCache(None), rate=0))

        with tempfile.TemporaryDirectory() as tmp:
            builtins.__import__ = no_openpyxl
            try:
                paths = report.write_all(tmp, universe, clusters, meta)
            finally:
                builtins.__import__ = real_import
            self.assertNotIn("xlsx", paths, "no workbook without the library")
            for kind in ("json", "csv", "md"):
                self.assertIn(kind, paths)
                self.assertTrue(os.path.getsize(paths[kind]) > 0,
                                f"{kind} must still be written")


class ProxySeamTests(unittest.TestCase):
    """What stays here: the endpoint-specific half of proxy verification.

    Rotation, the durable store and its ageing policy belong to keel-crawler
    (`keel_crawler.proxy.pool`) and are tested there — this package only decides
    what a usable answer from *this* endpoint looks like.
    """

    def test_a_real_autocomplete_answer_is_accepted(self):
        self.assertTrue(accept_suggestions(200, '["quotex",["quotex login"]]'))

    def test_a_captive_portal_is_rejected_despite_its_200(self):
        self.assertFalse(accept_suggestions(200, "<html><body>Login required</body></html>"),
                         "a proxy returning an interstitial would otherwise join the "
                         "pool and then fail every real request")

    def test_a_refusal_or_an_empty_body_is_rejected(self):
        self.assertFalse(accept_suggestions(403, "[]"))
        self.assertFalse(accept_suggestions(200, "   "))


class ScoreTests(unittest.TestCase):
    def build(self, rows):
        universe = Universe(seed="quotex")
        from keel_seo.keywords.crawl import Phrase

        for text, rank, relevance, level, parents in rows:
            phrase = Phrase(text, rank, relevance, level)
            phrase.parents = set(parents)
            universe.phrases[text] = phrase
        score(universe)
        return universe

    def test_better_rank_and_wider_reach_outrank_the_tail(self):
        universe = self.build([
            ("quotex login", 1, 900, 0, ["a", "b", "c", "d"]),
            ("quotex zigzag settings pdf", 14, 550, 2, ["z"]),
        ])
        self.assertGreater(
            universe.phrases["quotex login"].priority,
            universe.phrases["quotex zigzag settings pdf"].priority,
        )

    def test_ranked_output_is_ordered_by_priority(self):
        universe = self.build([
            ("quotex tail", 12, 550, 2, ["z"]),
            ("quotex head", 1, 900, 0, ["a", "b"]),
        ])
        self.assertEqual([p.text for p in universe.ranked()],
                         ["quotex head", "quotex tail"])


class ClusterTests(unittest.TestCase):
    def universe(self, phrases):
        from keel_seo.keywords.crawl import Phrase

        universe = Universe(seed="quotex")
        for index, text in enumerate(phrases):
            phrase = Phrase(text, 1 + index % 5, 600 - index, 0)
            phrase.parents = {f"p{index}"}
            universe.phrases[text] = phrase
        score(universe)
        return universe

    def test_a_rare_shared_word_groups_and_a_common_one_does_not(self):
        """The whole point of weighting overlap by rarity.

        `strategy` is spread across the corpus, so sharing it means almost
        nothing; `zigzag` and `withdrawal` are confined to one topic each, so
        sharing one of those is what makes two phrases the same subject. Plain
        overlap counting cannot tell those two cases apart.
        """
        universe = self.universe([
            "quotex zigzag strategy", "quotex zigzag indicator", "quotex zigzag settings",
            "quotex withdrawal problem", "quotex withdrawal proof", "quotex withdrawal time",
            "quotex martingale strategy", "quotex rsi strategy", "quotex trend strategy",
            "quotex scalping strategy", "quotex candlestick strategy",
        ])
        clusters = clustering.build(universe)
        by_phrase = {p.text: c.index for c in clusters for p in c.members}
        self.assertEqual(by_phrase["quotex zigzag strategy"],
                         by_phrase["quotex zigzag indicator"],
                         "a rare shared word must group two phrases")
        self.assertEqual(by_phrase["quotex withdrawal problem"],
                         by_phrase["quotex withdrawal proof"])
        self.assertNotEqual(by_phrase["quotex zigzag strategy"],
                            by_phrase["quotex martingale strategy"],
                            "sharing only the common word `strategy` is not a topic")
        self.assertNotEqual(by_phrase["quotex zigzag strategy"],
                            by_phrase["quotex withdrawal proof"])

    def test_the_seed_is_never_what_makes_two_phrases_similar(self):
        universe = self.universe(["quotex login", "quotex apk"])
        clusters = clustering.build(universe)
        self.assertEqual(len(clusters), 2, "sharing only the seed is not similarity")

    def test_every_phrase_lands_in_exactly_one_cluster(self):
        phrases = ["quotex login", "quotex login page", "quotex apk", "quotex apk download",
                   "quotex bonus code", "quotex demo"]
        clusters = clustering.build(self.universe(phrases))
        placed = [p.text for c in clusters for p in c.members]
        self.assertEqual(sorted(placed), sorted(phrases))
        self.assertEqual(len(placed), len(set(placed)))

    def test_clusters_come_back_in_priority_order(self):
        universe = self.universe(["quotex login", "quotex login page", "quotex apk"])
        clusters = clustering.build(universe)
        self.assertEqual([c.priority for c in clusters],
                         sorted([c.priority for c in clusters], reverse=True))


class IntentTests(unittest.TestCase):
    def classify(self, phrase):
        drop = clustering.STOPWORDS | set(seed_tokens("quotex"))
        return clustering.classify_intent(clustering.tokenize(phrase, drop))

    def test_each_intent_is_recognised(self):
        self.assertEqual(self.classify("quotex login"), "navigational")
        self.assertEqual(self.classify("quotex vs pocket option"), "commercial")
        self.assertEqual(self.classify("quotex bonus code"), "transactional")
        self.assertEqual(self.classify("how to use quotex"), "informational")
        self.assertEqual(self.classify("is quotex legal in india"), "informational")

    def test_an_unmarked_phrase_is_brand_not_navigational(self):
        self.assertEqual(self.classify("quotex signal bot"), clustering.BRAND)


if __name__ == "__main__":
    unittest.main()
