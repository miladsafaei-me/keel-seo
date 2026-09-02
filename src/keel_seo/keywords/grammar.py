"""Every way a term can be surrounded, expressed as queries to ask.

Autocomplete answers a *prefix*, and it answers at most 15 phrases at a time.
Asking a bare term therefore returns its fifteen most popular continuations and
hides everything else. The only way to reach the rest is to ask the same term
many times with something different attached, so that each variant opens onto a
different fifteen.

This module is that set of attachments. Four families, each reaching phrases the
others cannot:

*Spaced suffix* — ``quotex a`` .. ``quotex z``, ``quotex 0`` .. ``quotex 9``.
Partitions the continuation space by the next word's first character. This is
the workhorse: it turns one window of 15 onto the whole alphabet's worth.

*Tight suffix* — ``quotexa`` .. ``quotexz``, no space. Reaches compounds a
spaced sweep never sees; ``quotexa`` returned ``quotexapk``.

*Spaced prefix* — ``a quotex`` .. ``z quotex``, ``0 quotex`` .. ``9 quotex``.
The endpoint is not a strict prefix matcher, so a leading character pulls back
phrases in which the term sits in the *middle or at the end*: ``a quotex``
returned ``is quotex a broker`` and ``what is a quotex trading``. Without this
family the harvest only ever sees term-initial phrases, which is a minority of
how any brand is actually searched.

*Word affixes* — real words before and after (``is quotex``, ``vs quotex``,
``quotex vs``, ``how to use quotex``). Single characters cannot reach the
question and comparison shapes; ``is quotex`` returned ``is quotex legal in
india``, ``is quotex legit``, ``is quotex trading halal``, none of which appears
under any letter sweep.

*Mid-phrase star* — ``quotex * signal bot``, ``quotex signal * bot``: a ``*``
walked through every gap between words, which Google fills. This is the
highest-yield family in the grammar and the only one that reaches phrases where
the term is **not the leading text**. Measured against a finished 2,869-phrase
``quotex`` universe, 900 of these returned **1,346 phrases nothing else found** —
1.5 new per query, against a 0.1 bar. Examples: ``best indicator for quotex
trading``, ``ai trading bot quotex otc``, ``affiliate center quotex``.

*Leading space* — every seed-tier query asked a second time with one space in
front. ``" quotex"`` and ``"quotex"`` are different queries to this endpoint;
0.286 new phrases per query.

A **trailing** space is deliberately absent. Google trims it, so ``"quotex "``
came back byte-identical to ``"quotex"``: asking both would double the request
count for exactly zero new data. The fixed ``WILDCARD_TEMPLATES`` remain off by
default, superseded by the gap-walk above.
"""
from __future__ import annotations

import string
from typing import Iterable

ALPHABET = tuple(string.ascii_lowercase)
DIGITS = tuple(string.digits)

# Words placed BEFORE the term. These reach the phrasings where the term is not
# the first word — questions, comparisons and qualifiers — which no single
# leading character reliably produces.
PREFIX_WORDS = (
    "how", "how to", "how to use", "how to get", "how to open", "how much",
    "what", "what is", "what are", "why", "when", "where", "which", "who",
    "can", "can i", "does", "do", "is", "are", "will", "should",
    "best", "top", "free", "online", "new", "cheap", "safe", "legit", "real",
    "download", "review", "alternative to", "better than", "similar to",
    "for", "with", "without", "like", "about", "using", "from", "in", "on",
    "vs", "compare", "no", "not", "my", "your",
)

# Words placed AFTER the term. A trailing connector opens the *second* word
# position, which the alphabet sweep only reaches one character at a time.
SUFFIX_WORDS = (
    "for", "with", "without", "vs", "or", "and", "in", "on", "to", "from",
    "near", "like", "under", "over", "after", "before",
    "is", "are", "not", "does", "how", "what", "why", "when", "where", "which",
    "best", "free", "online", "app", "login", "download", "review", "safe",
    "legit", "alternative", "meaning", "price", "cost",
)

# Attachments for a phrase discovered mid-crawl. A discovered phrase is already
# several words long, so it needs far less surrounding than the seed does — the
# full grammar on every one of hundreds of phrases would spend the whole budget
# re-asking near-identical questions.
BRANCH_SUFFIX_WORDS = (
    "for", "with", "vs", "in", "not", "is", "how", "best", "free", "app",
    "download", "review",
)
BRANCH_PREFIX_WORDS = ("how to", "what is", "best", "is", "why", "free")

# Wildcard templates, off by default. Superseded for practical purposes by the
# mid-phrase `*` below, which is measured and on; these fixed shapes are kept only
# because a future seed may sit in a phrase the gap-walk cannot reach.
WILDCARD_TEMPLATES = ("{term} * {tail}", "best * {term}", "{term} * app")

SEED = "seed"
BRANCH = "branch"
DRILL = "drill"


def star_variants(term: str) -> list[str]:
    """Walk a `*` through every gap between the term's words.

    ``quotex signal bot`` becomes ``quotex * signal bot`` and
    ``quotex signal * bot``. Google fills the gap, and what it fills it with is
    the family every other attachment misses: phrases where the term is **not
    the leading text**. Measured against a finished 2,869-phrase `quotex`
    universe, 900 of these queries returned **1,346 phrases the rest of the
    grammar never found** — 1.5 new phrases per query, where the bar for adding
    a family at all was 0.1. What came back is exactly the shape that was
    missing: ``best indicator for quotex trading``, ``ai trading bot quotex
    otc``, ``affiliate center quotex``.

    Single-word terms have no internal gap and yield nothing here; they are
    reached by the prefix families instead.
    """
    words = term.split()
    if len(words) < 2:
        return []
    return [" ".join(words[:i]) + " * " + " ".join(words[i:])
            for i in range(1, len(words))]


def _dedupe(queries: Iterable[str]) -> list[str]:
    """Preserve order while dropping repeats.

    Families overlap by construction — ``is`` is both a prefix word and a suffix
    word — and every duplicate is a wasted request.

    Internal whitespace is collapsed but **a single leading space is kept**,
    because it is not formatting: ``" quotex"`` and ``"quotex"`` return different
    answers, and normalising it away would silently delete a whole family. A
    *trailing* space is not preserved, and deliberately so — Google trims it, so
    ``"quotex "`` came back byte-identical to ``"quotex"`` and asking both would
    double the request count for nothing.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        lead = " " if query.startswith(" ") else ""
        query = lead + " ".join(query.split())
        if query.strip() and query not in seen:
            seen.add(query)
            ordered.append(query)
    return ordered


def expansions(term: str, tier: str = SEED, *, tight: bool = True,
               wildcards: bool = False) -> list[str]:
    """The queries to ask so that `term` gives up everything it can.

    ``tier`` selects how much surrounding the term earns. ``SEED`` is the full
    grammar and is paid once per run; ``BRANCH`` is the reduced grammar every
    discovered phrase gets; ``DRILL`` is the alphabet alone, used to go one level
    deeper underneath a query Google truncated.
    """
    term = term.strip()
    if not term:
        return []

    if tier == DRILL:
        return _dedupe(f"{term} {c}" for c in ALPHABET)

    queries = [term]
    queries += [f"{term} {c}" for c in ALPHABET]
    queries += [f"{term} {d}" for d in DIGITS]

    if tier == BRANCH:
        queries += [f"{term} {w}" for w in BRANCH_SUFFIX_WORDS]
        queries += [f"{w} {term}" for w in BRANCH_PREFIX_WORDS]
        # The highest-yield family in the whole grammar, and the only one that
        # reaches phrases where the term is not the leading text. Branch phrases
        # are multi-word by the time they get here, which is exactly where it
        # applies.
        queries += star_variants(term)
        return _dedupe(queries)

    if tight:
        queries += [f"{term}{c}" for c in ALPHABET]
    queries += [f"{c} {term}" for c in ALPHABET]
    queries += [f"{d} {term}" for d in DIGITS]
    queries += [f"{w} {term}" for w in PREFIX_WORDS]
    queries += [f"{term} {w}" for w in SUFFIX_WORDS]
    queries += star_variants(term)
    # A leading space is its own family: " quotex" and "quotex" return different
    # answers, and asking every seed-tier query both ways returned 0.286 new
    # phrases per query — nearly three times the bar for keeping a family.
    queries += [f" {q}" for q in queries]
    if wildcards:
        queries += [
            template.format(term=term, tail=tail)
            for template in WILDCARD_TEMPLATES
            for tail in ("", "app", "strategy")
        ]
    return _dedupe(queries)
