"""Grow a seed into the phrases Google Play's search box offers for it.

Play's autocomplete is prefix-only and returns at most five phrases, so one call
per seed shows almost nothing. Two moves widen it:

1. *The alphabet:* ``seed a`` to ``seed z`` and ``seed 0`` to ``seed 9``.
   Each character forces a different set of five.
2. *Drilling:* every new phrase is asked on its own. When its answer is full
   (five phrases, so the store had more), it earns its own alphabet too, for up
   to ``drill`` phrases per level, down to ``depth`` levels. The cap is what
   keeps a run at hundreds of calls rather than thousands.

A phrase is kept only if the store offered it; nothing is invented. How many
different queries offered a phrase is kept as a weak popularity hint, because
Play exposes nothing better.
"""
from __future__ import annotations

import string
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .client import SUGGEST_CAPACITY, Blocked

ALPHABET = list(string.ascii_lowercase) + list(string.digits)


@dataclass
class Phrase:
    text: str
    level: int
    first_seen: int
    offered_by: int = 0


def norm(text: str) -> str:
    return " ".join(text.lower().split())


def expand(client, seeds: list[str], depth: int = 2, drill: int = 8,
           max_phrases: int = 300, workers: int = 4, progress=print) -> list[Phrase]:
    found: dict[str, Phrase] = {}
    asked: set[str] = set()
    counter = [0]

    def ask(queries: list[str]) -> list[tuple[str, list[str]]]:
        queries = [q for q in dict.fromkeys(queries) if q not in asked]
        asked.update(queries)

        def one(query):
            try:
                return query, client.suggest(query)
            except Blocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad call is not a run
                progress(f"  suggest failed for {query!r}: {exc}")
                return query, []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(one, queries))

    def record(answers, level) -> list[str]:
        fresh = []
        for _query, phrases in answers:
            for raw in phrases:
                text = norm(raw)
                if text not in found:
                    counter[0] += 1
                    found[text] = Phrase(text, level, counter[0])
                    fresh.append(text)
                found[text].offered_by += 1
        return fresh

    seeds = list(dict.fromkeys(norm(s) for s in seeds if s.strip()))
    for seed in seeds:
        found.setdefault(seed, Phrase(seed, 0, 0))
    fresh = record(ask(seeds + [f"{s} {c}" for s in seeds for c in ALPHABET]), 1)
    progress(f"level 1: {len(asked)} queries, {len(found)} phrases")

    for level in range(2, depth + 1):
        if not fresh or len(found) >= max_phrases:
            break
        answers = ask(fresh)
        full = [q for q, phrases in answers if len(phrases) >= SUGGEST_CAPACITY]
        new = record(answers, level)
        new += record(ask([f"{q} {c}" for q in full[:drill] for c in ALPHABET]), level)
        fresh = new
        progress(f"level {level}: {len(asked)} queries, {len(found)} phrases")

    # Every phrase found but never asked is asked once, so its own completions
    # are counted too. Cheap, and the store's own ordering is all we have.
    if len(found) < max_phrases:
        record(ask([p for p in found if p not in asked]), depth + 1)
    phrases = sorted(found.values(), key=lambda p: (p.level, p.first_seen))
    return phrases[:max_phrases]
