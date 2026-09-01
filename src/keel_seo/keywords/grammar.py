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

A fifth family, the ``*`` wildcard, is implemented and off by default: measured
against ``quotex * strategy`` and ``best * quotex`` it returned either what the
other families already produce, or noise from other industries.
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

# Wildcard templates, off by default. Kept because the endpoint does honour `*`
# and a future seed may sit in a phrase shape the other families miss.
WILDCARD_TEMPLATES = ("{term} * {tail}", "best * {term}", "{term} * app")

SEED = "seed"
BRANCH = "branch"
DRILL = "drill"


def _dedupe(queries: Iterable[str]) -> list[str]:
    """Preserve order while dropping repeats.

    Families overlap by construction — ``is`` is both a prefix word and a suffix
    word — and every duplicate is a wasted request.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        query = " ".join(query.split())
        if query and query not in seen:
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
        return _dedupe(queries)

    if tight:
        queries += [f"{term}{c}" for c in ALPHABET]
    queries += [f"{c} {term}" for c in ALPHABET]
    queries += [f"{d} {term}" for d in DIGITS]
    queries += [f"{w} {term}" for w in PREFIX_WORDS]
    queries += [f"{term} {w}" for w in SUFFIX_WORDS]
    if wildcards:
        queries += [
            template.format(term=term, tail=tail)
            for template in WILDCARD_TEMPLATES
            for tail in ("", "app", "strategy")
        ]
    return _dedupe(queries)
