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

*Native grammars.* The letters and the words above are English, and for a seed
typed in another script they reach nothing: ``تک ویزا ترکیه a`` never opens the
next Persian word, and ``how تک ویزا ترکیه`` is not a question anyone asks.
``grammar_for(hl)`` therefore picks the alphabet and the word affixes by the
interface language the run asks in. Persian, Arabic, Russian, German and Spanish
have their own; for Persian, Arabic and Russian the Latin a-z sweep still runs
after the native one, because people mix Latin brand names into those queries.
English, an empty language and any language without a grammar get exactly the
English grammar, so an English run asks the same queries it always did.
"""
from __future__ import annotations

import string
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Grammar:
    """The letters and words one language surrounds a term with.

    ``letters`` is the language's own alphabet and drives the tight sweep, where
    a Latin letter glued to a Persian or Cyrillic word means nothing. ``sweep``
    drives the spaced suffix, spaced prefix and drill families: the native
    letters first, then a-z where the language mixes Latin brand names in.
    """

    language: str
    letters: tuple[str, ...]
    sweep: tuple[str, ...]
    prefix_words: tuple[str, ...]
    suffix_words: tuple[str, ...]
    branch_suffix_words: tuple[str, ...]
    branch_prefix_words: tuple[str, ...]


ENGLISH = Grammar(
    language="en",
    letters=ALPHABET,
    sweep=ALPHABET,
    prefix_words=PREFIX_WORDS,
    suffix_words=SUFFIX_WORDS,
    branch_suffix_words=BRANCH_SUFFIX_WORDS,
    branch_prefix_words=BRANCH_PREFIX_WORDS,
)


def _native(language: str, letters: str, *, latin: bool, prefix: tuple,
            suffix: tuple, branch_suffix: tuple, branch_prefix: tuple) -> Grammar:
    own = tuple(letters.split())
    sweep = own + tuple(c for c in ALPHABET if c not in own) if latin else own
    return Grammar(language, own, sweep, prefix, suffix, branch_suffix,
                   branch_prefix)


# Persian: the 32 letters plus alef madda, with Persian kaf and yeh. Words are
# written as people type them into a search box: no diacritics, no zero-width
# non-joiner.
PERSIAN = _native(
    "fa",
    "آ ا ب پ ت ث ج چ ح خ د ذ ر ز ژ س ش ص ض ط ظ ع غ ف ق ک گ ل م ن و ه ی",
    latin=True,
    prefix=(
        "چگونه", "چطور", "نحوه", "روش", "راهنمای", "آموزش", "چرا", "آیا",
        "کدام", "چه", "بهترین", "ارزانترین", "قیمت", "هزینه", "شرایط", "مدارک",
        "مدارک لازم برای", "مراحل", "ثبت نام", "درخواست", "دریافت", "خرید",
        "دانلود", "رایگان", "آنلاین", "نظرات", "تجربه", "مزایای", "معایب",
        "تفاوت", "مقایسه", "جدیدترین", "لیست", "انواع", "برای", "با", "در",
        "بدون",
    ),
    suffix=(
        "چیست", "چیه", "چطور", "چگونه", "چقدر", "کجاست", "چند", "قیمت", "هزینه",
        "شرایط", "مدارک", "مدارک لازم", "مراحل", "ثبت نام", "درخواست", "خرید",
        "دانلود", "رایگان", "آنلاین", "نظرات", "تجربه", "مزایا", "معایب",
        "بهتر است یا", "یا", "و", "با", "بدون", "برای", "در", "از", "خوبه",
        "معتبر", "جدید", "سایت",
    ),
    branch_suffix=("چیست", "قیمت", "هزینه", "شرایط", "مدارک", "برای", "با",
                   "در", "یا", "رایگان", "آنلاین", "نظرات"),
    branch_prefix=("چگونه", "نحوه", "شرایط", "بهترین", "آیا", "هزینه"),
)

# Arabic: the 28 letters with Arabic kaf and yeh. Where both spellings are in
# common use, the word is listed with and without hamza.
ARABIC = _native(
    "ar",
    "ا ب ت ث ج ح خ د ذ ر ز س ش ص ض ط ظ ع غ ف ق ك ل م ن ه و ي",
    latin=True,
    prefix=(
        "كيف", "كيفية", "طريقة", "ما هو", "ما هي", "لماذا", "متى", "اين", "أين",
        "هل", "افضل", "أفضل", "شروط", "متطلبات", "اوراق", "أوراق", "سعر",
        "اسعار", "أسعار", "تكلفة", "رسوم", "تحميل", "تنزيل", "شرح", "مميزات",
        "عيوب", "الفرق بين", "مقارنة", "التقديم على", "طلب", "تجربتي مع",
        "مجانا", "عن", "في", "مع",
    ),
    suffix=(
        "مجانا", "اون لاين", "اونلاين", "سعر", "اسعار", "أسعار", "تكلفة", "رسوم",
        "شروط", "متطلبات", "الاوراق المطلوبة", "الأوراق المطلوبة", "كيف",
        "ما هو", "او", "أو", "مع", "بدون", "في", "من", "الى", "إلى", "افضل",
        "أفضل", "تجربة", "تقييم", "رأي", "تحميل", "رابط", "التقديم", "مقابل",
    ),
    branch_suffix=("سعر", "اسعار", "شروط", "متطلبات", "مجانا", "في", "مع",
                   "او", "كيف", "تحميل", "تقييم", "افضل"),
    branch_prefix=("كيف", "ما هو", "افضل", "أفضل", "هل", "شروط"),
)

RUSSIAN = _native(
    "ru",
    "а б в г д е ё ж з и й к л м н о п р с т у ф х ц ч ш щ ъ ы ь э ю я",
    latin=True,
    prefix=(
        "как", "как получить", "как оформить", "как сделать", "что такое",
        "почему", "когда", "где", "какой", "какая", "какие", "кто", "можно ли",
        "сколько стоит", "стоимость", "цена", "лучший", "лучшие", "топ",
        "бесплатно", "онлайн", "новый", "отзывы", "условия", "требования",
        "документы для", "оформление", "получение", "заявка на", "скачать",
        "альтернатива", "аналог", "сравнение", "для", "без", "про",
    ),
    suffix=(
        "для", "без", "или", "из", "как", "что это", "почему", "где",
        "сколько стоит", "цена", "стоимость", "условия", "требования",
        "документы", "отзывы", "онлайн", "бесплатно", "скачать", "вход",
        "приложение", "лучший", "плюсы и минусы", "против", "аналог",
        "официальный сайт", "срок", "как получить", "это", "на", "по",
    ),
    branch_suffix=("для", "без", "или", "цена", "стоимость", "условия",
                   "документы", "отзывы", "онлайн", "как", "скачать", "на"),
    branch_prefix=("как", "что такое", "лучший", "сколько стоит", "почему",
                   "можно ли"),
)

GERMAN = _native(
    "de",
    " ".join(ALPHABET) + " ä ö ü ß",
    latin=False,
    prefix=(
        "wie", "wie viel", "wieviel", "wie lange", "was ist", "was kostet",
        "warum", "wann", "wo", "welche", "wer", "kann man", "ist", "gibt es",
        "beste", "bester", "günstig", "kostenlos", "online", "neue",
        "erfahrungen", "test", "voraussetzungen", "antrag", "unterlagen für",
        "alternative zu", "vergleich", "für", "mit", "ohne", "in", "auf", "über",
    ),
    suffix=(
        "für", "mit", "ohne", "oder", "und", "in", "auf", "von", "vs", "wie",
        "was", "warum", "kosten", "preis", "gebühren", "voraussetzungen",
        "anforderungen", "unterlagen", "dokumente", "beantragen", "antrag",
        "kostenlos", "online", "erfahrungen", "test", "bewertung", "app",
        "login", "download", "alternative", "vergleich", "seriös", "dauer",
        "deutsch",
    ),
    branch_suffix=("für", "mit", "oder", "in", "kosten", "preis",
                   "voraussetzungen", "beantragen", "kostenlos", "online",
                   "erfahrungen", "vergleich"),
    branch_prefix=("wie", "was ist", "beste", "was kostet", "warum", "kann man"),
)

# Spanish is usually typed without accents, so the common accented words are
# listed both ways.
SPANISH = _native(
    "es",
    " ".join(ALPHABET) + " ñ",
    latin=False,
    prefix=(
        "como", "cómo", "como sacar", "como obtener", "como solicitar", "que es",
        "qué es", "por que", "por qué", "cuando", "donde", "cual",
        "cuanto cuesta", "precio", "mejor", "mejores", "gratis", "online",
        "nuevo", "opiniones", "requisitos para", "documentos para",
        "solicitud de", "solicitar", "tramitar", "alternativa a", "comparativa",
        "para", "con", "sin", "en", "es", "se puede",
    ),
    suffix=(
        "para", "con", "sin", "en", "de", "vs", "como", "que es", "precio",
        "costo", "coste", "requisitos", "documentos", "solicitud", "gratis",
        "online", "en linea", "opiniones", "reseña", "es seguro",
        "es confiable", "app", "descargar", "iniciar sesion", "alternativa",
        "comparativa", "cuanto cuesta", "como funciona", "cuanto tarda",
        "español", "o", "y",
    ),
    branch_suffix=("para", "con", "o", "en", "precio", "costo", "requisitos",
                   "gratis", "online", "opiniones", "como", "vs"),
    branch_prefix=("como", "que es", "mejor", "cuanto cuesta",
                   "requisitos para", "por que"),
)

GRAMMARS = {g.language: g for g in (ENGLISH, PERSIAN, ARABIC, RUSSIAN, GERMAN,
                                    SPANISH)}


def grammar_for(language: str = "") -> Grammar:
    """The grammar for an interface language (`hl`), English when there is none.

    Only the primary subtag counts, so ``fa-IR`` and ``pt_BR`` resolve like
    ``fa`` and ``pt``. A language with no grammar of its own gets English, which
    is what every run got before native grammars existed.
    """
    code = (language or "").strip().lower().replace("_", "-").split("-")[0]
    return GRAMMARS.get(code, ENGLISH)


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
               wildcards: bool = False, language: str = "") -> list[str]:
    """The queries to ask so that `term` gives up everything it can.

    ``tier`` selects how much surrounding the term earns. ``SEED`` is the full
    grammar and is paid once per run; ``BRANCH`` is the reduced grammar every
    discovered phrase gets; ``DRILL`` is the alphabet alone, used to go one level
    deeper underneath a query Google truncated.

    ``language`` is the interface language the run asks in (its ``hl``). It picks
    the alphabet and the word affixes through ``grammar_for``; English, empty or
    unknown gives the English grammar unchanged.
    """
    grammar = grammar_for(language)
    term = term.strip()
    if not term:
        return []

    if tier == DRILL:
        return _dedupe(f"{term} {c}" for c in grammar.sweep)

    queries = [term]
    queries += [f"{term} {c}" for c in grammar.sweep]
    queries += [f"{term} {d}" for d in DIGITS]

    if tier == BRANCH:
        queries += [f"{term} {w}" for w in grammar.branch_suffix_words]
        queries += [f"{w} {term}" for w in grammar.branch_prefix_words]
        # The highest-yield family in the whole grammar, and the only one that
        # reaches phrases where the term is not the leading text. Branch phrases
        # are multi-word by the time they get here, which is exactly where it
        # applies.
        queries += star_variants(term)
        return _dedupe(queries)

    if tight:
        queries += [f"{term}{c}" for c in grammar.letters]
    queries += [f"{c} {term}" for c in grammar.sweep]
    queries += [f"{d} {term}" for d in DIGITS]
    queries += [f"{w} {term}" for w in grammar.prefix_words]
    queries += [f"{term} {w}" for w in grammar.suffix_words]
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
