"""Walk one seed's whole query space, then rank what came back.

The crawl is a breadth-first search over phrases, not a fixed list of requests.
Level 0 asks the seed through the full grammar. Every on-seed phrase that comes
back becomes a level-1 seed and is asked through the reduced grammar, and so on.
The cycle closes on its own: each level discovers fewer unseen phrases than the
last, and when a level discovers none the universe is exhausted at that grammar.

Two mechanisms keep that from being either shallow or unbounded.

*Saturation drilling.* A truncated response means Google had more to say, so any
query that came back full is re-asked with each letter appended. A query that
came back short is left alone — there is nothing behind it. This spends requests
only where evidence says more exists.

*The containment filter.* Only phrases containing the seed are recorded as part
of the universe and only they are re-seeded. This is what the crawl is for — the
complete set of phrases built on the seed — and it is also what stops the search
wandering: ``how to quotex`` returns ``how to quote on reddit``, and re-seeding
that would walk the crawl into an unrelated industry. Rejected phrases are not
thrown away; they are counted separately, because a term whose neighbours belong
to another market is exactly the contamination worth knowing about.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from .grammar import BRANCH, DRILL, SEED, expansions
from .suggest import SuggestClient

# How the crawl signals combine into one comparable number.
#
# These weights are measured, not chosen. Validated against a Semrush export of
# 1,747 `quotex` keywords carrying real US search volume, on the 379 phrases both
# sources hold, reach is by far the strongest predictor of actual demand
# (Spearman +0.443 on its own) while Google's suggestion rank is the weakest
# (+0.145). That is the opposite of the intuition this table first encoded, which
# led with rank and scored +0.269 overall.
#
# A grid search peaked at rank 0.00 / reach 0.80 / relevance 0.20 (+0.449), but
# zeroing a signal on 379 rows from one seed is overfitting, and rank is Google's
# own ordering rather than a derived quantity. The weights below keep all four
# alive and reach +0.421 - 94% of the achievable gain.
#
# Reach leads because a phrase surfaced by many independent expansions is
# structurally central rather than incidentally returned once. Relevance is
# Google's own score, useful but compressed into a narrow band. Rank and depth
# now mostly break ties.
WEIGHTS = {"rank": 0.10, "reach": 0.60, "relevance": 0.20, "depth": 0.10}

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def seed_tokens(seed: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(seed.lower()))


def contains_seed(phrase: str, tokens: tuple[str, ...]) -> bool:
    """True when every seed token appears somewhere in the phrase.

    Substring rather than whole-word, so ``quotexapk`` counts for the seed
    ``quotex``; order-independent, so ``calculator for pip value`` counts for the
    seed ``pip value calculator``.
    """
    haystack = normalize(phrase)
    return all(token in haystack for token in tokens)


@dataclass
class Phrase:
    """One harvested phrase and every signal the crawl learned about it."""

    text: str
    best_rank: int
    max_relevance: int
    first_level: int
    parents: set[str] = field(default_factory=set)
    priority: float = 0.0
    cluster: int = -1

    @property
    def reach(self) -> int:
        """How many distinct queries independently surfaced this phrase."""
        return len(self.parents)

    @property
    def words(self) -> int:
        return len(normalize(self.text).split())

    def as_row(self) -> dict:
        return {
            "phrase": self.text,
            "priority": round(self.priority, 1),
            "best_rank": self.best_rank,
            "reach": self.reach,
            "relevance": self.max_relevance,
            "level": self.first_level,
            "words": self.words,
            "cluster": self.cluster,
        }


@dataclass
class Universe:
    """The result of one crawl: the phrases, what was rejected, and how it ran."""

    seed: str
    phrases: dict[str, Phrase] = field(default_factory=dict)
    off_seed: dict[str, int] = field(default_factory=dict)
    levels_run: int = 0
    queries_asked: int = 0
    network_calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    unexpanded: int = 0
    exhausted: bool = False
    blocked: bool = False
    rate_limited: int = 0
    elapsed: float = 0.0
    per_level: list[dict] = field(default_factory=list)

    def ranked(self) -> list[Phrase]:
        return sorted(self.phrases.values(), key=lambda p: (-p.priority, p.text))


def crawl(
    seed: str,
    client: SuggestClient,
    *,
    levels: int = 2,
    budget: int = 150000,
    saturate: int = 1,
    frontier_cap: int = 300,
    tight: bool = True,
    wildcards: bool = False,
    progress=None,
) -> Universe:
    """Expand `seed` until it stops yielding, or until `budget` queries are spent."""
    started = time.time()
    tokens = seed_tokens(seed)
    universe = Universe(seed=seed)
    asked: set[str] = set()
    expanded: set[str] = set()
    frontier = [normalize(seed)]

    def announce(message: str) -> None:
        if progress:
            progress(message)

    def run(queries: list[str], level: int) -> list:
        """Ask what has not been asked, record every answer, respect the budget."""
        fresh = [q for q in queries if q not in asked]
        room = budget - universe.queries_asked
        if room <= 0:
            return []
        if len(fresh) > room:
            fresh = fresh[:room]
        asked.update(fresh)
        universe.queries_asked += len(fresh)
        responses = list(client.fetch_many(fresh))
        for response in responses:
            if response.error:
                universe.errors += 1
                continue
            for suggestion in response.suggestions:
                text = normalize(suggestion.phrase)
                if not contains_seed(text, tokens):
                    universe.off_seed[text] = universe.off_seed.get(text, 0) + 1
                    continue
                phrase = universe.phrases.get(text)
                if phrase is None:
                    phrase = Phrase(text, suggestion.rank, suggestion.relevance, level)
                    universe.phrases[text] = phrase
                else:
                    phrase.best_rank = min(phrase.best_rank, suggestion.rank)
                    phrase.max_relevance = max(phrase.max_relevance, suggestion.relevance)
                phrase.parents.add(response.query)
        if client.blocked:
            # Google has started refusing us. Everything already collected is
            # kept and written out; continuing would only convert budget into
            # 403s, which is what an earlier unthrottled run did for an hour.
            universe.blocked = True
            announce("  rate limited by the endpoint — stopping and keeping what we have")
        return responses

    for level in range(levels + 1):
        if not frontier or universe.queries_asked >= budget or universe.blocked:
            break
        before = len(universe.phrases)
        tier = SEED if level == 0 else BRANCH
        queries = [
            query
            for term in frontier
            for query in expansions(term, tier, tight=tight, wildcards=wildcards)
        ]
        expanded.update(frontier)
        announce(f"level {level}: {len(frontier)} term(s) -> {len(queries)} queries")
        responses = run(queries, level)

        # Go deeper only underneath the queries Google truncated. A short answer
        # means that corner of the space is already fully reported.
        for round_index in range(saturate):
            saturated = [r for r in responses if r.saturated]
            if not saturated or universe.queries_asked >= budget or universe.blocked:
                break
            drill = [q for r in saturated for q in expansions(r.query, DRILL)]
            announce(
                f"  drill {round_index + 1}: {len(saturated)} saturated -> {len(drill)} queries"
            )
            responses = run(drill, level)

        found = len(universe.phrases) - before
        universe.levels_run = level + 1
        universe.per_level.append(
            {"level": level, "terms": len(frontier), "new_phrases": found,
             "queries_total": universe.queries_asked}
        )
        announce(f"  level {level}: +{found} phrases (total {len(universe.phrases)})")

        candidates = [p for text, p in universe.phrases.items() if text not in expanded]
        candidates.sort(key=lambda p: (-p.reach, p.best_rank, -p.max_relevance))
        universe.unexpanded = len(candidates)
        frontier = [p.text for p in candidates[:frontier_cap]]
        if universe.blocked:
            break
        if not candidates:
            universe.exhausted = True
            announce("  universe closed: no unexpanded phrases remain")
            break

    universe.network_calls = client.calls
    universe.cache_hits = client.cache.hits
    universe.rate_limited = client.rate_limited
    universe.elapsed = time.time() - started
    score(universe)
    return universe


def score(universe: Universe) -> None:
    """Give every phrase one comparable priority number, in place.

    Relevance is scored by percentile rather than by min-max, because Google
    reserves values above 1000 for the one or two phrases it treats as near
    exact matches; a linear scale would let those two flatten everything else.
    """
    phrases = list(universe.phrases.values())
    if not phrases:
        return
    max_reach = max(p.reach for p in phrases) or 1
    max_level = max(p.first_level for p in phrases)

    by_relevance = sorted(phrases, key=lambda p: p.max_relevance)
    percentile = {}
    for index, phrase in enumerate(by_relevance):
        percentile[phrase.text] = index / max(1, len(by_relevance) - 1)

    for phrase in phrases:
        rank_score = 1.0 / (1.0 + math.log2(max(1, phrase.best_rank)))
        reach_score = math.log1p(phrase.reach) / math.log1p(max_reach)
        relevance_score = percentile[phrase.text]
        depth_score = 1.0 - (phrase.first_level / max(1, max_level)) if max_level else 1.0
        phrase.priority = 100.0 * (
            WEIGHTS["rank"] * rank_score
            + WEIGHTS["reach"] * reach_score
            + WEIGHTS["relevance"] * relevance_score
            + WEIGHTS["depth"] * depth_score
        )
