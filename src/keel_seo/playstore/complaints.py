"""What users of the weak apps complain about, counted, never summarised.

The low-star reviews of an app that ranks for a phrase are the cheapest product
spec there is: they name what the people already searching did not get. This
module only collects and counts them. Reading them for meaning is a person's job,
or a human-started Claude session's; nothing here calls a model.

The counted unit is a two-word phrase (``no sync``, ``too many ads``), after
dropping filler words, because single words ("app", "good") say nothing and
longer spans repeat too rarely to count.
"""
from __future__ import annotations

import re
from collections import Counter

FILLER = frozenset("""
a an the and or but if so to of in on at for from with without by as is are was
were be been being it its this that these those i me my we our you your he she
they them their app apps application just very really also not no can cant
can't do does did dont don't have has had will would should could get got even
please one all any some more much when then than there here what which who how
""".split())


def bigrams(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z']+", text.lower()) if w not in FILLER and len(w) > 2]
    return [f"{a} {b}" for a, b in zip(words, words[1:]) if a != b]


def complaint_terms(reviews: list[dict], top: int = 15) -> list[tuple[str, int]]:
    """The most repeated two-word phrases, each counted once per review."""
    counts: Counter = Counter()
    for review in reviews:
        counts.update(set(bigrams(review.get("text", ""))))
    return [(term, n) for term, n in counts.most_common(top) if n >= 2]
