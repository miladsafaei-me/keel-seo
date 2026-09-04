"""Name the language of each keyword, without a model.

A harvest that asks sixteen countries comes back carrying Portuguese, German,
Spanish, Hindi and Indonesian phrasings alongside the English ones. Mixed into a
single list they read as noise; named and separated, they are a map of demand in
markets the site may not serve yet. Either way the reader has to see which is
which — and *which language*, not merely "not English", because "Portuguese"
tells a reader the row belongs to Brazil while "non-English" tells them nothing
they can act on.

No model, no dependency, no network. Three tests in descending order of
certainty, and every answer carries the evidence that produced it, so a reader
can judge a call rather than trust it.

**Script.** Anything written outside the Latin alphabet — Devanagari, Cyrillic,
Arabic, Han, Hangul, Thai, Bengali — names its language outright. No judgement
involved.

**Marker words.** Plain ASCII gives no typographic clue, so the only remaining
signal is vocabulary: words common in one language that are not English words,
brand fragments or domain parts. This tier is a heuristic and is kept
deliberately narrow. Two lessons paid for during its construction: ``com``
matched ``quotex com login``, which is a domain and perfectly English, and
``dao`` matched ``quotex dao download``, which is the crypto sense. A marker that
is also an English word, a brand fragment or a URL piece costs more in false
positives than it recovers, so it does not go in the list. Where two languages
genuinely share a word — Spanish and Portuguese share many — the language with
more markers in that phrase wins; a tie that survives is reported as the pair
("Portuguese or Spanish") rather than resolved by coin toss.

**Diacritics.** Latin letters carrying marks English does not use (``español``,
``binäre``, ``confiável``). Certain evidence that the phrase is not English, and
sometimes enough to name it: ``ñ`` is Spanish, ``ß`` German, ``ã`` Portuguese.
Where the mark is shared (``é``, ``à``) and no marker word decided it, the answer
is an honest ``non-English``, not a guess.

The order matters: markers are consulted before shared diacritics, because a word
is stronger evidence than an accent four languages use.
"""
from __future__ import annotations

import re
import unicodedata

ENGLISH = "English"
UNDETERMINED = "non-English"

SCRIPT = "non-Latin script"
DIACRITIC = "non-English letters"
VOCABULARY = "non-English word"

# Words that are strong evidence of one language: common there, and not English
# words, brand fragments or domain parts. Keyed by language so a gap is visible
# and an addition has an obvious home. A word shared by two languages appears in
# both sets on purpose — the tie is real, and _marker_language reports it.
MARKERS: dict[str, frozenset[str]] = {
    "Spanish": frozenset("""
        opiniones confiable gratis cuenta retiro deposito dinero descargar
        aplicacion espanol estafa ganar sesion funciona sirve seguro ingresar
        registrarse como
    """.split()),
    "Portuguese": frozenset("""
        opinioes opiniao confiavel corretora cadastro saque portugues sinais
        reclame dinheiro sacar depositar como qual porque
    """.split()),
    "German": frozenset("""
        erfahrungen anmelden kostenlos konto auszahlung serios bewertung betrug
        einzahlung anmeldung verdienen geld sicher warum
    """.split()),
    "French": frozenset("""
        avis inscription gratuit retrait connexion telecharger arnaque fiable
        argent gagner inscrire pourquoi
    """.split()),
    "Italian": frozenset("""
        recensioni opinioni gratuito iscrizione affidabile truffa prelievo
        guadagnare soldi perche
    """.split()),
    "Polish": frozenset("""
        opinie logowanie wyplata oszustwo rejestracja pieniadze bezpieczny
    """.split()),
    "Turkish": frozenset("""
        nedir nasil guvenilir yorumlar giris cekme kazanma
    """.split()),
    "Indonesian": frozenset("""
        cara daftar penarikan apakah penipuan uang menghasilkan aman terpercaya
        adalah bagaimana
    """.split()),
    "Malay": frozenset("""
        adakah selamat wang pengeluaran percuma
    """.split()),
    "Hindi (Latin script)": frozenset("""
        kaise kya karne tarika nikale paise
    """.split()),
    "Urdu (Latin script)": frozenset("""
        kaisay tareeqa paisay
    """.split()),
    "Filipino": frozenset("""
        paano kumita magkano
    """.split()),
    "Swahili": frozenset("""
        jinsi kupata pesa
    """.split()),
    "Russian (Latin script)": frozenset("""
        otzyvy vyvod moshennik dengi
    """.split()),
}

# Consulted only to break a tie between languages that have already matched a
# real marker. On their own these are far too weak to name a language, and one of
# them naming one would be a false positive rather than a finding.
WEAK_MARKERS: dict[str, frozenset[str]] = {
    "Spanish": frozenset({"para", "una", "mejor"}),
    "Portuguese": frozenset({"quanto", "uma", "melhor"}),
    "French": frozenset({"quel", "meilleur"}),
    "German": frozenset({"beste"}),
    "Italian": frozenset({"quanto", "migliore"}),
}

# Every marker in one set, for the "is this English at all" question, which gets
# asked far more often than "which language is it".
MARKER_WORDS = frozenset().union(*MARKERS.values())

# Unicode script name prefixes, mapped to what they mean in this corpus. Script
# is the most certain tier there is, so this is stated flatly rather than hedged.
SCRIPTS: tuple[tuple[str, str], ...] = (
    ("DEVANAGARI", "Hindi"),
    ("ARABIC", "Arabic or Urdu"),
    ("CYRILLIC", "Russian"),
    ("CJK", "Chinese"),
    ("HIRAGANA", "Japanese"),
    ("KATAKANA", "Japanese"),
    ("HANGUL", "Korean"),
    ("THAI", "Thai"),
    ("BENGALI", "Bengali"),
    ("TAMIL", "Tamil"),
    ("TELUGU", "Telugu"),
    ("GUJARATI", "Gujarati"),
    ("GURMUKHI", "Punjabi"),
    ("HEBREW", "Hebrew"),
    ("GREEK", "Greek"),
    ("ETHIOPIC", "Amharic"),
)

# Letters that name one language on their own. Shared accents are deliberately
# absent: `é` belongs to French, Spanish, Portuguese and more, and guessing from
# it is how a column stops being worth reading.
TELLING_LETTERS: dict[str, str] = {
    "ñ": "Spanish",
    "ß": "German",
    "ã": "Portuguese",
    "õ": "Portuguese",
    "ı": "Turkish",
    "ş": "Turkish",
    "ğ": "Turkish",
    "ł": "Polish",
    "ż": "Polish",
    "ź": "Polish",
    "ę": "Polish",
    "ą": "Polish",
}

_LETTERS = re.compile(r"[a-z]+")


def _script_language(text: str) -> str:
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        if name.startswith("LATIN"):
            continue
        for prefix, language in SCRIPTS:
            if name.startswith(prefix):
                return language
        return "non-Latin script"
    return ""


def _has_non_latin_script(text: str) -> bool:
    return bool(_script_language(text))


def _marker_language(text: str) -> tuple[str, frozenset[str]]:
    """The language whose markers this phrase carries, and the words that said so."""
    words = set(_LETTERS.findall(text.lower()))
    scored = {}
    for candidate, markers in MARKERS.items():
        hits = markers & words
        if hits:
            scored[candidate] = frozenset(hits)
    if not scored:
        return "", frozenset()
    best = max(len(hits) for hits in scored.values())
    leaders = sorted(name for name, hits in scored.items() if len(hits) == best)
    if len(leaders) > 1:
        weighted = {name: len(WEAK_MARKERS.get(name, frozenset()) & words)
                    for name in leaders}
        top = max(weighted.values())
        if top and sum(1 for count in weighted.values() if count == top) == 1:
            leaders = [max(weighted, key=lambda name: weighted[name])]
    hits = frozenset().union(*(scored[name] for name in leaders))
    return " or ".join(leaders), hits


def _letter_language(text: str) -> str:
    for char in text.lower():
        if char in TELLING_LETTERS:
            return TELLING_LETTERS[char]
    return ""


def identify(text: str) -> tuple[str, str]:
    """``(language, why)`` for one phrase; ``why`` is "" when it is English."""
    script = _script_language(text)
    if script:
        return script, SCRIPT

    named, hits = _marker_language(text)
    if named:
        return named, f"{VOCABULARY}: {', '.join(sorted(hits))}"

    if any(ord(char) > 127 for char in text):
        return _letter_language(text) or UNDETERMINED, DIACRITIC

    return ENGLISH, ""


def language_of(text: str) -> str:
    """Just the language name, for a spreadsheet column."""
    return identify(text)[0]


def non_english_reason(text: str) -> str:
    """Why this phrase looks non-English, or "" if it does not."""
    return identify(text)[1]


def is_non_english(text: str) -> bool:
    return bool(non_english_reason(text))
