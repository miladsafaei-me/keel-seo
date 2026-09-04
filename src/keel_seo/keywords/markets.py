"""Which countries a harvest asks, and in which language it asks them.

A market is a country the crawl deliberately asks Google as, with ``gl=``. It is
never inferred from which address answered — that measures the proxy pool, not
demand. This module holds the default list of countries worth asking, so a
project does not have to remember sixteen codes, and so the list is one editable
thing rather than a habit spread across shell histories.

**Why a country carries a language.** Asking Brazil in English returns the
English phrasings Brazilians type, which is a real but small slice of Brazilian
demand; asking it with ``hl=pt`` returns the Portuguese half that is most of it.
The pairing is not decoration — it is what makes a market's answer worth the
requests it costs. Where English *is* the language of search, the pair says so:
India, Pakistan, Nigeria, Kenya, South Africa, the Philippines and Malaysia are
asked in English on purpose, because asking them in a local language would return
a smaller and less commercial universe than the one their searchers actually use.

**Per project.** The default is the list below. A Django host overrides it with
``KEEL_SEO["keyword_markets"]``; anything else can set
``KEEL_SEO_KEYWORD_MARKETS`` in the environment; and ``--markets`` on the command
line beats both. All three take the same shape — country codes, comma or space
separated, e.g. ``us,de,br``.
"""
from __future__ import annotations

import os

# The default target markets, in the order they are asked. Each is an ISO-3166
# alpha-2 country mapped to the interface language (`hl=`) that market's search
# is actually conducted in.
TARGET_MARKETS: dict[str, str] = {
    "US": "en",   # United States
    "CA": "en",   # Canada
    "DE": "de",   # Germany
    "FR": "fr",   # France
    "ES": "es",   # Spain
    "PT": "pt",   # Portugal
    "BR": "pt",   # Brazil
    "AR": "es",   # Argentina
    "IN": "en",   # India
    "PK": "en",   # Pakistan
    "ZA": "en",   # South Africa
    "NG": "en",   # Nigeria
    "KE": "en",   # Kenya
    "PH": "en",   # Philippines
    "MY": "en",   # Malaysia
    "ID": "id",   # Indonesia
}

SETTING_NAME = "keyword_markets"
ENV_NAME = "KEEL_SEO_KEYWORD_MARKETS"

# What a caller passes to mean "the whole default list" rather than typing it.
ALL = "target"


class UnknownMarket(ValueError):
    """A market code that is not two letters."""


def parse(value: str) -> list[str]:
    """Country codes out of one string, upper-cased and de-duplicated in order.

    Accepts commas or whitespace, so a list pasted from anywhere works. ``target``
    expands to the full default list; an empty string means no market at all,
    which is a legitimate answer — it asks Google without ``gl=`` and the output
    then says, honestly, that nothing in it can name a market.
    """
    codes: list[str] = []
    for raw in value.replace(",", " ").split():
        code = raw.strip().upper()
        if code.lower() == ALL:
            codes.extend(TARGET_MARKETS)
            continue
        if len(code) != 2 or not code.isalpha():
            raise UnknownMarket(
                f"markets take ISO-3166 alpha-2 country codes; {raw!r} is not one")
        codes.append(code)
    seen: set[str] = set()
    return [c for c in codes if not (c in seen or seen.add(c))]


def _from_django() -> str | None:
    """The host's `KEEL_SEO["keyword_markets"]`, or None if there is no host.

    Imported here rather than at module scope on purpose: this package is used
    from a plain CLI far more often than from inside a Django process, and it
    must not require a configured settings module to run.
    """
    try:
        from django.conf import settings  # noqa: PLC0415 - deliberate soft import
    except Exception:
        return None
    try:
        configured = getattr(settings, "KEEL_SEO", None)
    except Exception:
        # Django is installed but nothing is configured. Not an error here.
        return None
    if not isinstance(configured, dict):
        return None
    value = configured.get(SETTING_NAME)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def resolve(explicit: str | None = None) -> list[str]:
    """The markets to ask: the caller's answer, else the project's, else ours.

    Precedence is deliberate and matches every other knob in this package — the
    command line beats the project, and the project beats the default — so a
    one-off run never has to edit a setting and a project never has to repeat
    itself on the command line.
    """
    if explicit is not None:
        return parse(explicit)
    configured = _from_django()
    if configured is None:
        configured = os.environ.get(ENV_NAME)
    if configured is None:
        return list(TARGET_MARKETS)
    return parse(configured)


def language_for(market: str, override: str = "") -> str:
    """The interface language to ask a market in.

    An explicit `--hl` wins everywhere, because a run comparing markets on one
    language is a legitimate thing to want. Otherwise a known market is asked in
    its own language and an unknown one in English, which is the safe default for
    a code this module has never heard of.
    """
    if override:
        return override
    return TARGET_MARKETS.get((market or "").upper(), "en")
