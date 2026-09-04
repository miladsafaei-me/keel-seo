"""Group the harvest by how much wording phrases genuinely share.

Every phrase in a universe contains the seed, so the seed's own tokens carry no
information about which phrases belong together and are removed before anything
is compared. What is left is the part that distinguishes ``quotex zigzag
strategy`` from ``quotex withdrawal problem``.

Shared words are then weighted by how rare they are across the harvest. Two
phrases that share ``app`` share almost nothing — half the corpus says ``app``.
Two that share ``zigzag`` are about the same thing. Plain overlap counting
cannot tell those apart, so similarity is inverse-document-frequency weighted:
a shared token contributes in proportion to how few other phrases use it.

Clusters are then grown by average linkage, which is what keeps a long tail from
chaining into one blob. Under single linkage, A-B similar and B-C similar drags
C in however unlike A it is; average linkage asks whether a phrase resembles the
cluster as a whole, so a cluster stops growing once it stops being one topic.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .crawl import Phrase, Universe, seed_tokens

_WORD = re.compile(r"[a-z0-9]+")

# Words that appear everywhere and mean nothing on their own. Deliberately short:
# an aggressive stoplist deletes the very modifiers that distinguish intent
# ("free", "best", "without" all matter here), so only true function words go.
STOPWORDS = frozenset(
    """a an the of in on at to for with and or is are was be been do does did
    my your it its this that these those from by as i you he she they we""".split()
)

# Deterministic intent markers. No model: each bucket is a set of words whose
# presence in a query reliably indicates what the searcher wants to do next.
INTENT_MARKERS = {
    "navigational": frozenset(
        """login log sign signin signup account app apk download install official
        website site web link portal dashboard platform desktop pc""".split()
    ),
    "transactional": frozenset(
        """bonus promo code coupon deposit withdraw withdrawal payout minimum
        price cost fee buy open register registration voucher trial""".split()
    ),
    "commercial": frozenset(
        """best top review reviews vs versus compare comparison alternative
        alternatives legit legitimate scam safe real fake trustworthy better
        worth rating rank""".split()
    ),
    "informational": frozenset(
        """how what why when where which who guide tutorial learn explained
        meaning definition example strategy tips tricks pdf course training
        beginners work works use using legal illegal legit-check halal haram
        banned allowed permitted tax taxable rules regulation licence license
        owner ceo founder country countries difference story history""".split()
    ),
}
# Ties are broken in this order: a query that both names a product action and
# asks a question is served by the action.
INTENT_PRECEDENCE = ("navigational", "transactional", "commercial", "informational")

# A phrase carrying none of the markers above is the seed plus a bare noun -
# "quotex demo", "quotex signal bot". That is not a fifth kind of confusion, it
# is the head of the term itself, and calling it navigational (the old fallback)
# quietly mislabelled every core product phrase in the harvest.
BRAND = "brand"


@dataclass
class Cluster:
    """One topic: its phrases, its label, and what it is worth going after."""

    index: int
    members: list[Phrase] = field(default_factory=list)
    signature: tuple[str, ...] = ()
    intent: str = BRAND

    @property
    def head(self) -> Phrase:
        return self.members[0]

    @property
    def label(self) -> str:
        return " / ".join(self.signature) if self.signature else self.head.text

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def priority(self) -> float:
        """A cluster is worth its best phrase, lifted by how much it covers.

        Size alone would crown the biggest pile of tail phrasings; the head's
        priority alone would ignore that a topic with forty phrasings is a page
        and a topic with two is a sentence. The logarithm keeps size from taking
        over once a cluster is already large.
        """
        return self.head.priority * (1.0 + math.log1p(self.size) / 4.0)

    def as_row(self) -> dict:
        return {
            "cluster": self.index,
            "label": self.label,
            "intent": self.intent,
            "size": self.size,
            "priority": round(self.priority, 1),
            "head": self.head.text,
            "signature": list(self.signature),
            "phrases": [p.as_row() for p in self.members],
        }


def tokenize(phrase: str, drop: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in _WORD.findall(phrase.lower()) if t not in drop)


def classify_intent(tokens: frozenset[str]) -> str:
    scores = {name: len(tokens & markers) for name, markers in INTENT_MARKERS.items()}
    best = max(scores.values())
    if best == 0:
        return BRAND
    for name in INTENT_PRECEDENCE:
        if scores[name] == best:
            return name
    return "informational"


# Tuned on a 4,273-phrase `quotex` harvest (2026-09-01). Sweeping 0.16-0.40:
# 0.16 chained unrelated topics into a 212-phrase blob, 0.40 left 17% of the
# corpus in singletons. 0.24 clustered 92% of phrases while the largest cluster
# (74 phrases, all "is quotex real or fake") stayed narrow enough to be one page,
# which is the unit a cluster is supposed to map to.
# How many topics a harvest is cut into. Not a similarity threshold: the
# previous version grew clusters bottom-up until a similarity cut stopped them,
# which on a 9,499-phrase `quotex` universe produced 1,796 clusters, 47% of them
# a single phrase and a median size of 2. That is a list wearing a cluster's
# name. Fixing the number of topics instead gives an output someone can act on -
# the same corpus becomes ~200 clusters at a median of 28.
DEFAULT_TOPICS = 200

# An anchor word must name a real group, not one phrase.
MIN_ANCHOR_PHRASES = 3

# How many of a keyword's absorbed surface forms are named in the output. Enough
# to show that a brand is typed two ways and that word order moves around; not so
# many that a cell becomes a list nobody reads.
MAX_NAMED_VARIANTS = 4

# Where phrases go when they contain no anchor word at all. Named, and left
# whole, rather than forced into the nearest topic: a similarity fallback was
# measured and placed one phrase out of 7,476, while misfiling `quotex
# erfahrungen` under "trading" and `quotex iran` under "com". An honest residue
# beats a tidy lie.
TAIL_LABEL = "long tail"


def fold_plural(token: str, vocabulary) -> str:
    """Treat a plural as its singular when the singular is also in the corpus.

    Without this, `signal` and `signals` become two topics describing the same
    thing, which is how a keyword list acquires duplicate pages. Deliberately
    crude - only -s and -es, and only when the singular actually occurs - because
    a real stemmer would also fold `trading` into `trade` and `legal` into
    `leg`, merging topics that are genuinely different.
    """
    for suffix in ("es", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            stem = token[: -len(suffix)]
            if stem in vocabulary:
                return stem
    return token


def canonical_key(tokens: frozenset[str]) -> tuple[str, ...]:
    """The identity of a keyword regardless of the order its words were typed in.

    ``quotex ai trading bot``, ``ai quotex trading bot``, ``quotex ai bot
    trading`` and ``quotex trading bot ai`` are one keyword asked four ways, and
    Google returns all four. On the `quotex` universe this collapsed 1,164
    redundant phrases across 819 groups - 12% of the corpus - each of which had
    been diluting its own cluster and its own signals.
    """
    return tuple(sorted(tokens))


def build(universe: Universe, *, topics: int = DEFAULT_TOPICS,
          min_anchor: int = MIN_ANCHOR_PHRASES) -> list[Cluster]:
    """Group a crawled universe into a usable number of named topics.

    Three steps, in order.

    **Collapse word-order variants.** Phrases with the same content words are the
    same keyword; the highest-priority surface form represents the group and the
    rest are counted as variants.

    **Name the topics.** Every remaining keyword is described by its content
    words. The words worth building a page around are the ones carrying the most
    demand, so candidate anchors are ranked by the total priority of the keywords
    containing them, and the top `topics` of those that name at least
    `min_anchor` keywords become the topic set.

    **Assign by the most specific anchor a keyword actually contains.** A phrase
    holding both ``app`` and ``zigzag`` belongs under ``zigzag``: the rarer word
    is the one that says what the phrase is about. A keyword containing no anchor
    is not forced anywhere - it goes to an explicitly named long-tail group.
    """
    phrases = universe.ranked()
    if not phrases:
        return []

    # Every spelling of the seed is dropped, not only the canonical one. With
    # just `fundingpips` dropped, "funding pips rules" keeps `funding` and `pips`
    # as content words while "fundingpips rules" keeps none — so two spellings of
    # one keyword describe two different topics and land in two clusters. The
    # seed is what every phrase in the universe has in common; no spelling of it
    # can be what a phrase is about.
    drop = STOPWORDS | set(seed_tokens(universe.seed))
    for spelling in getattr(universe, "variants", ()):
        drop |= set(seed_tokens(spelling))
    vocabulary = {t for phrase in phrases for t in tokenize(phrase.text, drop)}

    def content(text: str) -> frozenset[str]:
        return frozenset(fold_plural(t, vocabulary) for t in tokenize(text, drop))

    grouped: dict[tuple[str, ...], list[Phrase]] = {}
    for phrase in phrases:
        grouped.setdefault(canonical_key(content(phrase.text)), []).append(phrase)

    # One representative per keyword, plus the variants it absorbed.
    keywords: list[Phrase] = []
    tokens: list[frozenset[str]] = []
    for key, members in grouped.items():
        members.sort(key=lambda p: -p.priority)
        head = members[0]
        head.variants = len(members)
        # Named, not just counted. Which other ways a keyword is typed is the
        # answer to "do people write the brand with a space", and a bare count
        # cannot answer it. Capped, because the point is evidence, not a dump.
        head.also_written = [m.text for m in members[1:MAX_NAMED_VARIANTS + 1]]
        # The strongest evidence in the group represents it: a variant surfaced by
        # more queries is the same keyword, so its reach belongs to the keyword.
        head.parents = set().union(*(m.parents for m in members))
        # A collapsed keyword inherits every market its variants reached, at the
        # best rank any of them managed there. Ranks are minimised, not summed:
        # two spellings of one keyword do not make it twice as prominent.
        merged: dict = {}
        for member in members:
            for code, rank in member.markets.items():
                merged[code] = min(rank, merged.get(code, rank))
        head.markets = merged
        keywords.append(head)
        tokens.append(frozenset(key))

    document_frequency: dict[str, int] = {}
    for bag in tokens:
        for token in bag:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(keywords)
    idf = {t: math.log(total / df) + 1.0 for t, df in document_frequency.items()}

    demand: dict[str, float] = {}
    for index, bag in enumerate(tokens):
        for token in bag:
            demand[token] = demand.get(token, 0.0) + keywords[index].priority

    ranked_anchors = sorted(
        (t for t in demand if document_frequency[t] >= min_anchor),
        key=lambda t: (-demand[t], t),
    )
    anchors = set(ranked_anchors[:topics])

    buckets: dict[str, list[int]] = {}
    for index, bag in enumerate(tokens):
        own = bag & anchors
        # Most specific wins: the rarest anchor present is the one that says what
        # the phrase is about.
        label = max(own, key=lambda t: (idf[t], t)) if own else TAIL_LABEL
        buckets.setdefault(label, []).append(index)

    clusters: list[Cluster] = []
    for label, members in buckets.items():
        members.sort(key=lambda i: -keywords[i].priority)
        cluster = Cluster(index=0, members=[keywords[i] for i in members])
        if label == TAIL_LABEL:
            # The tail has nothing in common by definition, so a signature drawn
            # from it would name whatever happened to repeat and read as a topic.
            cluster.signature = (TAIL_LABEL,)
        else:
            extra = _signature(members, tokens, idf, keep=2)
            cluster.signature = (label,) + tuple(t for t in extra if t != label)
        cluster.intent = _intent(members, tokens)
        clusters.append(cluster)

    # The tail always goes last however large it grows. It is the residue, and a
    # residue at the top of the list reads as the most important topic.
    clusters.sort(key=lambda c: (c.label == TAIL_LABEL, -c.priority))
    for index, cluster in enumerate(clusters, 1):
        cluster.index = index
        for phrase in cluster.members:
            phrase.cluster = index
    return clusters


def _signature(members: list[int], tokens: list[frozenset[str]],
               idf: dict[str, float], keep: int = 3) -> tuple[str, ...]:
    """Name a cluster by the rare words most of its members agree on."""
    counts: dict[str, int] = {}
    for index in members:
        for token in tokens[index]:
            counts[token] = counts.get(token, 0) + 1
    floor = max(1, len(members) // 2)
    shared = [(t, c) for t, c in counts.items() if c >= floor]
    shared.sort(key=lambda tc: (-tc[1] * idf[tc[0]], tc[0]))
    return tuple(token for token, _ in shared[:keep])


def _intent(members: list[int], tokens: list[frozenset[str]]) -> str:
    votes: dict[str, int] = {}
    for index in members:
        name = classify_intent(tokens[index])
        votes[name] = votes.get(name, 0) + 1
    best = max(votes.values())
    for name in INTENT_PRECEDENCE:
        if votes.get(name) == best:
            return name
    return BRAND
