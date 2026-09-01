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
DEFAULT_THRESHOLD = 0.24


def build(universe: Universe, *, threshold: float = DEFAULT_THRESHOLD,
          max_postings: int = 400) -> list[Cluster]:
    """Cluster a crawled universe, most valuable topic first."""
    phrases = universe.ranked()
    if not phrases:
        return []

    drop = STOPWORDS | set(seed_tokens(universe.seed))
    tokens = [tokenize(p.text, drop) for p in phrases]

    document_frequency: dict[str, int] = {}
    for bag in tokens:
        for token in bag:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(phrases)
    idf = {t: math.log(total / df) + 1.0 for t, df in document_frequency.items()}

    weight = [sum(idf[t] for t in bag) for bag in tokens]

    # Only phrases sharing at least one token can be similar, so pairs are drawn
    # from an inverted index rather than from all N^2. Very common tokens are
    # skipped as pair *generators* - they would propose most of the corpus while
    # contributing almost nothing to the score - but they still count in it.
    postings: dict[str, list[int]] = {}
    for index, bag in enumerate(tokens):
        for token in bag:
            postings.setdefault(token, []).append(index)

    similarity: dict[tuple[int, int], float] = {}
    for token, members in postings.items():
        if len(members) < 2 or len(members) > max_postings:
            continue
        for position, i in enumerate(members):
            for j in members[position + 1:]:
                key = (i, j)
                if key in similarity:
                    continue
                shared = tokens[i] & tokens[j]
                if not shared:
                    continue
                union = weight[i] + weight[j] - sum(idf[t] for t in shared)
                if union <= 0:
                    continue
                score = sum(idf[t] for t in shared) / union
                if score >= threshold:
                    similarity[key] = score

    parent = list(range(len(phrases)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    groups: dict[int, list[int]] = {i: [i] for i in range(len(phrases))}
    for (i, j), score in sorted(similarity.items(), key=lambda kv: -kv[1]):
        a, b = root(i), root(j)
        if a == b:
            continue
        left, right = groups[a], groups[b]
        linkage = sum(
            similarity.get((min(x, y), max(x, y)), 0.0) for x in left for y in right
        ) / (len(left) * len(right))
        if linkage < threshold:
            continue
        parent[b] = a
        groups[a] = left + right
        del groups[b]

    clusters: list[Cluster] = []
    for members in groups.values():
        members.sort(key=lambda i: -phrases[i].priority)
        cluster = Cluster(index=0, members=[phrases[i] for i in members])
        cluster.signature = _signature(members, tokens, idf)
        cluster.intent = _intent(members, tokens)
        clusters.append(cluster)

    clusters.sort(key=lambda c: -c.priority)
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
