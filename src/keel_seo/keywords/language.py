"""Tell the non-English keywords apart from the English ones, without a model.

A pooled harvest leaves from fifty-odd countries, so it comes back carrying
Spanish, Portuguese, German, Hindi and Russian phrasings alongside the English
ones. Mixed into a single list they read as noise; separated, they are a map of
demand in markets the site does not serve yet. Either way the reader has to be
able to see which is which.

Three tests, in descending order of certainty, and each result says which one
fired so a reader can judge it rather than trust it:

**Script.** Anything written outside the Latin alphabet — Cyrillic, Devanagari,
Arabic, Hangul, Han — is not English. No judgement involved.

**Diacritics.** Latin letters carrying marks English does not use (``español``,
``móvil``, ``confiável``, ``binäre``). Near-certain.

**Marker words.** Plain ASCII gives no typographic clue, so the only remaining
signal is vocabulary: words that are common in another language and are not
English words, brand fragments or domain parts. This tier is a heuristic and is
kept deliberately narrow. Two lessons paid for during its construction: ``com``
matched ``quotex com login``, which is a domain and perfectly English, and
``dao`` matched ``quotex dao download``, which is the crypto sense. A marker that
is also an English word, a brand fragment or a URL piece costs more in false
positives than it recovers, so it does not go in the list.
"""
from __future__ import annotations

import re
import unicodedata

# Words that are strong evidence of another language: common there, and not
# English words, brand fragments or domain parts. Grouped by language so a gap
# is visible and an addition has an obvious home.
MARKER_WORDS = frozenset("""
opiniones opinioes opiniao confiavel confiable gratis cuenta retiro deposito
dinero descargar aplicacion espanol estafa ganar sesion
corretora cadastro saque portugues sinais confiavel
erfahrungen anmelden kostenlos konto auszahlung serios bewertung betrug
avis inscription gratuit retrait connexion telecharger arnaque fiable
recensioni opinioni gratuito iscrizione affidabile truffa
opinie logowanie wyplata oszustwo
nedir nasil guvenilir yorumlar giris
cara daftar penarikan apakah penipuan
kaise kya karne tarika nikale
otzyvy vyvod moshennik
como qual porque pourquoi warum perche
""".split())

_LETTERS = re.compile(r"[a-z]+")

SCRIPT = "non-Latin script"
DIACRITIC = "non-English letters"
VOCABULARY = "non-English word"


def _has_non_latin_script(text: str) -> bool:
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        if not name.startswith("LATIN"):
            return True
    return False


def non_english_reason(text: str) -> str:
    """Why this phrase looks non-English, or "" if it does not.

    Returning the reason rather than a boolean is deliberate: the third tier is a
    heuristic, and a reader who can see *which* word triggered it can dismiss a
    wrong call instead of quietly distrusting the whole column.
    """
    if _has_non_latin_script(text):
        return SCRIPT
    if any(ord(char) > 127 for char in text):
        return DIACRITIC
    hits = MARKER_WORDS & set(_LETTERS.findall(text.lower()))
    if hits:
        return f"{VOCABULARY}: {', '.join(sorted(hits))}"
    return ""


def is_non_english(text: str) -> bool:
    return bool(non_english_reason(text))
