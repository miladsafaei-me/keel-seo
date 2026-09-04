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
from .language import language_of
from .proxying import normalize_country
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

# How many phrases must spell the seed a given way before that spelling is worth
# a seed-tier expansion of its own. One occurrence is a typo Google echoed back;
# a spelling people genuinely use turns up throughout the suggestions.
MIN_VARIANT_USES = 3

# How many alternative spellings a run will chase by default. Two is enough for
# the case that motivated this - a brand written with and without a space - and
# the ranking means the two kept are the two most used. More than a handful is
# vanity: each one costs a full seed tier, and the tail is misspellings.
DEFAULT_MAX_VARIANTS = 2

# How many queries a market is asked before the run decides whether to crawl it.
# Sixty is about a sixth of the seed tier and roughly a thousandth of a full
# market crawl, which is what makes the question worth asking at all: the probe
# for all fifteen secondary markets costs less than 2% of crawling one of them.
PROBE_QUERIES = 60

# What a market has to show, in the probe, to earn a full crawl: this share of
# its answers unseen in the primary market's answers to the SAME queries, and at
# least this many of them. Both conditions, because either alone lies. A market
# returning four phrases, all new, is 100% novel and worth nothing; a market
# returning six hundred phrases with 3% new is busy agreeing with the primary
# market in a different accent.
#
# The share was measured, not guessed, and the guess it replaced was 0.25 - which
# would have thrown away Germany, France and Spain. Probing all fifteen secondary
# target markets against a US primary (seed `fundingpips`, 60 queries each,
# 2026-09-04) produced two clearly separated groups:
#
#     ES 42%  ID 40%  AR 40%  DE 32%  FR 30%  PT 29%  BR 29%   <- kept
#     IN 17%  NG 12%  KE 12%  PK 11%  CA 5%  ZA 5%  PH 5%  MY 5% <- set aside
#
# Nothing lands between 17% and 29%, and the gap is not an accident of this seed:
# it is the line between markets asked in another language and markets asked in
# English, which is a structural cause and should hold for any seed. 0.22 sits in
# the middle of that gap with five points of margin on each side. India is the
# closest call at 17%, and the one to re-examine if a seed's own numbers move.
PROBE_NOVELTY_SHARE = 0.22

# A guard against the degenerate case rather than a second threshold: with the
# default 60-query probe every market that passed the share test returned at
# least 102 unseen phrases, so this only ever fires on a probe too small to
# conclude anything from.
PROBE_NOVELTY_FLOOR = 15


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def seed_tokens(seed: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(seed.lower()))


def squash(text: str) -> str:
    """The phrase with every separator removed: ``funding pips`` -> ``fundingpips``.

    This is what lets one harvest cover a brand's spellings. People type
    ``fundingpips``, ``funding pips`` and ``funding-pips`` for the same firm and
    Google answers all three, but a seed matched against the spaced text only
    recognises its own spelling and files the rest as contamination. Matching
    against the squashed text recognises every spacing of the same letters.
    """
    return "".join(_WORD.findall(text.lower()))


def contains_seed(phrase: str, tokens: tuple[str, ...]) -> bool:
    """True when every seed token appears somewhere in the phrase.

    Substring rather than whole-word, so ``quotexapk`` counts for the seed
    ``quotex``; order-independent, so ``calculator for pip value`` counts for the
    seed ``pip value calculator``; and against the squashed phrase, so ``funding
    pips rules`` counts for the seed ``fundingpips``.
    """
    haystack = squash(phrase)
    return all(token in haystack for token in tokens)


def seed_spelling(phrase: str, tokens: tuple[str, ...]) -> str:
    """How this phrase spells the seed — ``funding pips`` inside a longer phrase.

    Only meaningful for a one-token seed, which is the case that has variants
    worth finding: a brand. It walks the text keeping a squashed index alongside,
    so what comes back is the substring the searcher actually typed, spaces and
    hyphens included, rather than a reconstruction of it.
    """
    if len(tokens) != 1:
        return ""
    needle = tokens[0]
    text = normalize(phrase)
    positions = [i for i, char in enumerate(text) if char.isalnum()]
    joined = "".join(text[i] for i in positions)
    at = joined.find(needle)
    if at < 0:
        return ""
    return text[positions[at]:positions[at + len(needle) - 1] + 1]


def discover_variants(universe, tokens: tuple[str, ...], canonical: str,
                      limit: int) -> list[str]:
    """The other spellings of the seed that Google itself keeps returning.

    Deliberately read from the data rather than generated. Splitting
    ``fundingpips`` into ``funding pips`` from the string alone needs a
    dictionary and still guesses wrong on ``the5ers``; the suggestions already
    contain whichever spellings are searched, and how often each appears is
    exactly the ranking of which ones matter. Only the top `limit` are kept —
    the tail of this list is typos, and every variant kept costs a full
    seed-tier expansion.
    """
    if limit <= 0 or len(tokens) != 1:
        return []
    counted: dict[str, int] = {}
    for text in universe.phrases:
        spelling = seed_spelling(text, tokens)
        if spelling and spelling != canonical:
            counted[spelling] = counted.get(spelling, 0) + 1
    ranked = sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))
    return [spelling for spelling, seen in ranked[:limit] if seen >= MIN_VARIANT_USES]


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
    # Which markets return this phrase, and the best rank it reached in each.
    # A market is a country deliberately ASKED FOR with `gl=`, never inferred
    # from which exit answered: an exit tally measures the composition of the
    # proxy pool. Measured on a 7,210-keyword harvest, the country read off the
    # pool tracked proxy workload exactly - US held 26% of the requests and 41%
    # of the labels - and 65% of keywords changed their top country once that
    # bias was divided out. So this dict is empty unless a market was requested.
    markets: dict = field(default_factory=dict)
    # How many surface forms this keyword absorbed, e.g. "quotex ai trading bot"
    # standing for the four orderings Google also returns. 1 means it was unique.
    variants: int = 1
    # The absorbed forms themselves, highest priority first. The count alone said
    # a keyword had been typed three ways without saying which three, which is
    # the half a reader needs: "funding pips rules" sitting under "fundingpips
    # rules" is the evidence that both spellings are searched.
    also_written: list = field(default_factory=list)

    @property
    def market(self) -> str:
        """Where this phrase ranks best, or "" if no market was asked for.

        Best *rank*, not most appearances: rank is what the market itself said
        about the phrase, and is not a function of how much of the crawl that
        market happened to receive.
        """
        if not self.markets:
            return ""
        return min(sorted(self.markets), key=lambda code: self.markets[code])

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
            "variants": self.variants,
            "also_written": list(self.also_written),
            "language": language_of(self.text),
            "market": self.market,
            "markets": dict(sorted(self.markets.items(), key=lambda kv: kv[1])),
            "cluster": self.cluster,
        }


@dataclass
class Universe:
    """The result of one crawl: the phrases, what was rejected, and how it ran."""

    seed: str
    # The other spellings of the seed this run crawled — "funding pips" for the
    # seed "fundingpips". They are part of the universe's identity, not a
    # diagnostic: the clustering has to know them, or one keyword typed two ways
    # lands in two topics.
    variants: tuple[str, ...] = ()
    phrases: dict[str, Phrase] = field(default_factory=dict)
    off_seed: dict[str, int] = field(default_factory=dict)
    # Which exits answered this run, as a diagnostic on the pool - never a
    # statement about where a keyword is searched. See Phrase.markets.
    egress_countries: dict = field(default_factory=dict)
    market: str = ""
    levels_run: int = 0
    queries_asked: int = 0
    network_calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    unexpanded: int = 0
    exhausted: bool = False
    timed_out: bool = False
    blocked: bool = False
    rate_limited: int = 0
    elapsed: float = 0.0
    per_level: list[dict] = field(default_factory=list)

    def ranked(self) -> list[Phrase]:
        return sorted(self.phrases.values(), key=lambda p: (-p.priority, p.text))


def merge_markets(seed: str, per_market: dict) -> Universe:
    """One universe out of several per-market crawls, keeping who found what.

    Each market is a separate crawl because the expansion re-seeds from what came
    back: `gl=id` returns "binary option adalah", which then opens a corner of the
    space a `gl=us` crawl never reaches. Merging afterwards is therefore the only
    way to keep both the union of keywords and the per-market evidence - a single
    crawl with a mixed egress gets neither.

    Ranks are minimised across markets and never averaged: a keyword at rank 1 in
    one market and absent from another is a strong keyword with a narrow market,
    not a mediocre one.
    """
    merged = Universe(seed=seed)
    merged.market = " ".join(sorted(per_market))
    # Spellings are a property of the seed, not of a market, but each market
    # discovers them independently and may find one the others missed. The union
    # is what the clustering needs, so one keyword typed two ways stays one
    # keyword no matter which market surfaced which spelling.
    spellings: list[str] = []
    for code in sorted(per_market):
        for spelling in per_market[code].variants:
            if spelling not in spellings:
                spellings.append(spelling)
    merged.variants = tuple(spellings)
    for code in sorted(per_market):
        universe = per_market[code]
        merged.queries_asked += universe.queries_asked
        merged.network_calls += universe.network_calls
        merged.cache_hits += universe.cache_hits
        merged.errors += universe.errors
        merged.unexpanded += universe.unexpanded
        merged.rate_limited += universe.rate_limited
        merged.elapsed += universe.elapsed
        merged.levels_run = max(merged.levels_run, universe.levels_run)
        merged.timed_out = merged.timed_out or universe.timed_out
        merged.blocked = merged.blocked or universe.blocked
        for entry in universe.per_level:
            merged.per_level.append({**entry, "market": code})
        for text, count in universe.off_seed.items():
            merged.off_seed[text] = merged.off_seed.get(text, 0) + count
        for origin, count in universe.egress_countries.items():
            merged.egress_countries[origin] = (
                merged.egress_countries.get(origin, 0) + count)
        for text, phrase in universe.phrases.items():
            held = merged.phrases.get(text)
            if held is None:
                phrase.markets = {code: phrase.best_rank}
                merged.phrases[text] = phrase
                continue
            held.best_rank = min(held.best_rank, phrase.best_rank)
            held.max_relevance = max(held.max_relevance, phrase.max_relevance)
            held.first_level = min(held.first_level, phrase.first_level)
            held.parents |= phrase.parents
            held.markets[code] = min(phrase.best_rank,
                                     held.markets.get(code, phrase.best_rank))
    # Exhausted only if every market closed: one market still holding an
    # unexpanded frontier means the universe is not closed.
    merged.exhausted = bool(per_market) and all(
        u.exhausted for u in per_market.values())
    return merged


def probe_market(seed: str, client: SuggestClient, reference,
                 *, queries: int = PROBE_QUERIES, tight: bool = True,
                 wildcards: bool = False,
                 variants: tuple[str, ...] = ()) -> tuple[Universe, dict]:
    """Ask a market a sample of the seed tier, and measure how much of it is new.

    The question a probe answers is the only one worth asking before spending a
    market's crawl: **does this country search differently from the primary
    one?** Not "does it return results" — every market returns results, mostly
    the same ones.

    `reference` is what the primary market answered **to these same queries**,
    not its whole universe, and that distinction is the difference between a
    stable threshold and a moving one. Measured against a finished universe, a
    market's novelty falls as the primary is crawled deeper — the same market
    scores 29% against a level-0 primary and less against a level-2 one — so any
    threshold would silently mean something different at each `--levels`. Same
    questions, same sample size, both sides: the comparison is then between
    markets and nothing else.

    The sample is a stride through the seed tier rather than its first N, so all
    six attachment families are represented; taking the front of the list would
    ask nothing but ``a``-``z`` suffixes and measure one corner of the space. The
    same stride is used in every market, so the comparison is between markets and
    not between query sets.

    The phrases it collects are kept and returned. A probe is real data paid for
    in real requests, and throwing it away because the market did not earn a full
    crawl would be the second waste after the one this function exists to end.
    """
    tokens = seed_tokens(seed)
    plan: list[str] = []
    for term in [normalize(seed)] + [normalize(v) for v in variants]:
        plan.extend(expansions(term, SEED, tight=tight, wildcards=wildcards))
    stride = max(1, len(plan) // max(1, queries))
    sample = plan[::stride][:queries]

    universe = Universe(seed=seed, variants=tuple(variants))
    for response in client.fetch_many(sample):
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
                universe.phrases[text] = Phrase(text, suggestion.rank,
                                                suggestion.relevance, 0)
            else:
                phrase.best_rank = min(phrase.best_rank, suggestion.rank)
                phrase.max_relevance = max(phrase.max_relevance,
                                           suggestion.relevance)
            universe.phrases[text].parents.add(response.query)
    universe.queries_asked = len(sample)
    universe.levels_run = 1
    score(universe)

    known = set(reference)
    fresh = [text for text in universe.phrases if text not in known]
    returned = len(universe.phrases)
    verdict = {
        "queries": len(sample),
        "phrases": returned,
        "new": len(fresh),
        "novelty": round(len(fresh) / returned, 3) if returned else 0.0,
    }
    return universe, verdict


def worth_crawling(verdict: dict, *, share: float = PROBE_NOVELTY_SHARE,
                   floor: int = PROBE_NOVELTY_FLOOR) -> bool:
    """Whether a probed market earned its full crawl. Both tests, never either."""
    return verdict["new"] >= floor and verdict["novelty"] >= share


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
    max_seconds: float = 0.0,
    variants: tuple[str, ...] = (),
    max_variants: int = DEFAULT_MAX_VARIANTS,
    progress=None,
) -> Universe:
    """Expand `seed` until it stops yielding, or a stated limit is reached.

    `max_seconds` is a *graceful* deadline, and that distinction is the whole
    point of it. A run killed from outside — an external ``timeout``, a systemd
    ``TimeoutStartSec`` — dies between levels and writes nothing, because output
    is produced once at the end. That happened to a six-hour `quotex` harvest
    which had already found 8,513 phrases and left none of them on disk. Given
    its own deadline the crawl stops at the same moment, keeps everything, and
    says in the report that it ran out of time rather than out of universe.
    """
    started = time.time()
    tokens = seed_tokens(seed)
    canonical = normalize(seed)
    given = [normalize(v) for v in variants if normalize(v) != canonical]
    universe = Universe(seed=seed, variants=tuple(given))
    asked: set[str] = set()
    expanded: set[str] = set()
    # Every spelling the caller named is a seed in its own right, expanded with
    # the full seed grammar. Spellings the crawl discovers for itself join after
    # the first level, once the data says which ones people use.
    frontier = [canonical] + given

    def announce(message: str) -> None:
        if progress:
            progress(message)

    deadline = started + max_seconds if max_seconds else 0.0

    def out_of_time() -> bool:
        return bool(deadline) and time.time() >= deadline

    def run(queries: list[str], level: int) -> list:
        """Ask what has not been asked, record every answer, respect the limits."""
        if out_of_time():
            universe.timed_out = True
            return []
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
            # Which exits answered, counted for the run and not for the keyword.
            # It is worth knowing that a harvest left from thirty countries; it
            # is not worth writing next to a keyword, where it reads as the
            # market that keyword belongs to and is nothing of the kind.
            origin = normalize_country(response.country)
            if origin:
                universe.egress_countries[origin] = (
                    universe.egress_countries.get(origin, 0) + 1)
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
        if out_of_time():
            universe.timed_out = True
            announce("  time limit reached — stopping and keeping everything found")
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

        # The seed's own spellings, asked before drilling so the drill covers
        # them too. This happens after level 0 and only once: by then Google has
        # said which spellings it returns, and asking any later would leave a
        # whole spelling of the brand with no seed-tier expansion of its own.
        if level == 0 and not universe.blocked and not out_of_time():
            found = discover_variants(universe, tokens, canonical, max_variants)
            found = [v for v in found if v not in expanded]
            if found:
                universe.variants = tuple(list(universe.variants) + found)
                announce(f"  seed also spelled: {', '.join(found)}")
                expanded.update(found)
                responses = responses + run(
                    [q for term in found
                     for q in expansions(term, SEED, tight=tight, wildcards=wildcards)],
                    level)

        # Go deeper only underneath the queries Google truncated. A short answer
        # means that corner of the space is already fully reported.
        for round_index in range(saturate):
            saturated = [r for r in responses if r.saturated]
            if (not saturated or universe.queries_asked >= budget
                    or universe.blocked or out_of_time()):
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
        if universe.blocked or universe.timed_out:
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
