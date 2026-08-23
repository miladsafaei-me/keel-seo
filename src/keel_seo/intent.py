"""One query intent, one canonical URL — the deterministic anti-cannibalization gate.

Two pages competing for the same search need is not a content problem that shows up
in a word count; it is an architecture problem that shows up only once both pages are
long enough to be plausible answers. By then the fix is a redirect, not an edit. This
module makes the ownership decision explicit and machine-checkable *before* that
point.

The registry is a declaration, authored by the host, of every query intent the site
deliberately targets and the single URL that owns each one. Everything else on the
site that touches the same intent is listed as a *deferral*: it may exist, it may be
linked, it may even be excellent, but it must not be indexable, because an indexable
second page is the cannibalization.

A host wires it through ``KEEL_SEO["intent_registry_hook"]`` -- a dotted path to a
callable returning either a list of intent dicts or a mapping::

    {
        "intents": [
            {
                "key": "contract.high-low@what-is",
                "entity": "contract.high-low",
                "frame": "what-is",
                "owner": "/instruments/high-low",
                "label": "What a high/low binary option is and how it settles",
                # Live pages that touch this need and must stay noindex.
                "defers": ["/tag/call-option"],
                # Pages withdrawn in favour of the owner: they must be gone from
                # the Landing table and 301 to it. Recording them here is what
                # keeps a resolved collision from quietly coming back.
                "retired": ["/tag/high-low-contract"],
            },
        ],
        # Optional synonym net: alternate spellings of one entity, so two entries
        # that *look* distinct are caught as the same intent under two names.
        "entity_families": {
            "contract.turbo": ["contract.60-second", "contract.30-second"],
        },
        # Optional. URL prefixes where every indexable page must appear somewhere in
        # the registry -- as an owner, a deferral or a retirement. Use it on the
        # section that keeps producing competitors for pages that already exist: a new
        # page there fails the check until somebody states which need it answers, and
        # stating that is what makes a collision with an existing owner visible before
        # the page is written rather than after it ranks.
        "guarded_prefixes": ["/blog/"],
    }

``keel_seo_intent_check`` then enforces the invariants against the live ``Landing``
table. The vocabulary (what the entities and frames of this site are) belongs to the
host; the invariants and the gate belong here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.utils.module_loading import import_string

from .config import seo_setting

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9-]*$")

# Violation codes, in the order the checker reports them. Each is a distinct defect
# with a distinct fix, so they are never collapsed into one "invalid registry".
CODES = (
    "key-shape",
    "duplicate-key",
    "aliased-intent",
    "owner-missing",
    "owner-noindex",
    "deferral-missing",
    "deferral-indexable",
    "deferral-is-owner",
    "retired-still-present",
    "undeclared-in-guarded-section",
)


@dataclass(frozen=True)
class Intent:
    """One query need and the one URL allowed to answer it."""

    key: str
    entity: str
    frame: str
    owner: str
    label: str = ""
    defers: tuple = ()
    retired: tuple = ()


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    key: str = ""
    url: str = ""

    def __str__(self) -> str:
        where = self.url or self.key
        return f"[{self.code}] {where}: {self.message}"


@dataclass
class Registry:
    """The loaded registry plus the lookups a caller actually wants."""

    intents: tuple = ()
    entity_families: dict = field(default_factory=dict)
    guarded_prefixes: tuple = ()

    def by_key(self) -> dict:
        return {i.key: i for i in self.intents}

    def owned_by(self, url: str) -> tuple:
        """Every intent this URL is the canonical owner of."""
        u = _norm_url(url)
        return tuple(i for i in self.intents if _norm_url(i.owner) == u)

    def deferrals_of(self, url: str) -> tuple:
        """Every intent this URL touches but does not own."""
        u = _norm_url(url)
        return tuple(i for i in self.intents if u in {_norm_url(d) for d in i.defers})

    def canonical_owner_for(self, url: str) -> str:
        """The URL owning the first intent this URL defers to, or ``""``.

        What a deferring page renders as its "the full treatment lives here" link.
        """
        deferrals = self.deferrals_of(url)
        return deferrals[0].owner if deferrals else ""

    def mentions(self, url: str) -> bool:
        """Does any entry name this URL at all, in any role?"""
        u = _norm_url(url)
        for intent in self.intents:
            if _norm_url(intent.owner) == u:
                return True
            if u in {_norm_url(d) for d in intent.defers + intent.retired}:
                return True
        return False

    def family_of(self, entity: str) -> str:
        """The canonical entity for ``entity`` after the synonym net is applied."""
        token = _norm_token(entity)
        return self.entity_families.get(token, token)


def _norm_token(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _norm_url(url: str) -> str:
    """Compare URLs without letting a trailing slash decide an outcome."""
    u = str(url or "").strip()
    if len(u) > 1:
        u = u.rstrip("/")
    return u.lower() or "/"


def load_registry() -> Registry:
    """Load the host's registry. An unset hook yields an empty registry, not an error.

    An empty registry makes every check trivially pass, which is the right default: a
    project that has not declared its intents has not opted into the gate.
    """
    dotted = seo_setting("intent_registry_hook")
    if not dotted:
        return Registry()
    payload = import_string(dotted)() or {}
    return build_registry(payload)


def build_registry(payload) -> Registry:
    """Turn the host's raw payload into a :class:`Registry`. Pure, so the invariants
    are testable without a settings dict or a database."""
    if isinstance(payload, dict):
        raw_intents = payload.get("intents") or []
        raw_families = payload.get("entity_families") or {}
        guarded = tuple(payload.get("guarded_prefixes") or ())
    else:
        raw_intents, raw_families, guarded = list(payload), {}, ()

    families = {}
    for canonical, aliases in raw_families.items():
        canon = _norm_token(canonical)
        families[canon] = canon
        for alias in aliases or ():
            families[_norm_token(alias)] = canon

    intents = tuple(
        Intent(
            key=_norm_token(row.get("key")),
            entity=_norm_token(row.get("entity")),
            frame=_norm_token(row.get("frame")),
            owner=str(row.get("owner") or "").strip(),
            label=str(row.get("label") or "").strip(),
            defers=tuple(
                str(d).strip() for d in (row.get("defers") or ()) if str(d).strip()
            ),
            retired=tuple(
                str(d).strip() for d in (row.get("retired") or ()) if str(d).strip()
            ),
        )
        for row in raw_intents
    )
    return Registry(intents=intents, entity_families=families, guarded_prefixes=guarded)


def _landing_map(landings):
    if landings is not None:
        return {_norm_url(u): f for u, f in landings.items()}
    from .models import Landing

    return {
        _norm_url(url): flag
        for url, flag in Landing.objects.values_list("url", "is_indexable")
    }


def check(registry=None, *, landings=None) -> list:
    """Validate the registry against the live ``Landing`` table.

    ``landings`` is an optional ``{url: is_indexable}`` mapping, so the invariants can
    be exercised without a database. When omitted the live table is read.
    """
    registry = registry if registry is not None else load_registry()
    landings = _landing_map(landings)

    violations = []
    seen_keys = set()
    seen_intents = {}

    for intent in registry.intents:
        if not KEY_RE.match(intent.key):
            violations.append(
                Violation(
                    "key-shape",
                    "key must read '<entity>@<frame>' in lower-case, e.g. "
                    "'contract.high-low@what-is'",
                    key=intent.key,
                )
            )
        if intent.key in seen_keys:
            violations.append(
                Violation("duplicate-key", "declared more than once", key=intent.key)
            )
        seen_keys.add(intent.key)

        signature = (registry.family_of(intent.entity), intent.frame)
        first = seen_intents.get(signature)
        if first and first != intent.key:
            violations.append(
                Violation(
                    "aliased-intent",
                    f"same entity and frame as '{first}' -- one need, two keys, so the "
                    "gate would never see them collide",
                    key=intent.key,
                )
            )
        seen_intents.setdefault(signature, intent.key)

        owner = _norm_url(intent.owner)
        if owner not in landings:
            violations.append(
                Violation(
                    "owner-missing",
                    "owner has no Landing row, so nothing enforces its indexability",
                    key=intent.key,
                    url=intent.owner,
                )
            )
        elif not landings[owner]:
            violations.append(
                Violation(
                    "owner-noindex",
                    "the canonical owner of an intent must itself be indexable",
                    key=intent.key,
                    url=intent.owner,
                )
            )

        for deferral in intent.defers:
            url = _norm_url(deferral)
            if url == owner:
                violations.append(
                    Violation(
                        "deferral-is-owner",
                        "a URL cannot defer to itself",
                        key=intent.key,
                        url=deferral,
                    )
                )
            elif url not in landings:
                violations.append(
                    Violation(
                        "deferral-missing",
                        "listed as a live noindex page but has no Landing row -- move "
                        "it to 'retired' if it was withdrawn, or fix the entry",
                        key=intent.key,
                        url=deferral,
                    )
                )
            elif landings[url]:
                violations.append(
                    Violation(
                        "deferral-indexable",
                        f"indexable while '{intent.owner}' owns the same intent -- this "
                        "is the cannibalization the registry exists to stop",
                        key=intent.key,
                        url=deferral,
                    )
                )

        for withdrawn in intent.retired:
            url = _norm_url(withdrawn)
            if url in landings:
                violations.append(
                    Violation(
                        "retired-still-present",
                        f"recorded as withdrawn in favour of '{intent.owner}' but the "
                        "Landing row is back -- a reseed re-created it",
                        key=intent.key,
                        url=withdrawn,
                    )
                )

    # A guarded section is one that keeps producing competitors for pages that already
    # exist. Requiring every indexable URL under it to be declared turns "somebody
    # wrote another post about the payout percentage" from something noticed months
    # later into a failing check on the day it lands.
    for url, indexable in sorted(landings.items()):
        if not indexable:
            continue
        if not any(url.startswith(_norm_url(p).rstrip("/") + "/") or url == _norm_url(p)
                   for p in registry.guarded_prefixes):
            continue
        if registry.mentions(url):
            continue
        violations.append(
            Violation(
                "undeclared-in-guarded-section",
                "indexable and in a guarded section, but no registry entry names it -- "
                "state which need it answers, and whether a landing already owns that "
                "need, before it goes live",
                url=url,
            )
        )

    return violations


def coverage(registry=None, *, landings=None) -> list:
    """Indexable URLs that no registry entry mentions at all.

    Not a violation -- most pages are the uncontested owner of their own need and
    never need an entry. It is the list to read when deciding what to declare next.
    """
    registry = registry if registry is not None else load_registry()
    landings = _landing_map(landings)

    mentioned = {_norm_url(i.owner) for i in registry.intents}
    for intent in registry.intents:
        mentioned.update(_norm_url(d) for d in intent.defers)
        mentioned.update(_norm_url(d) for d in intent.retired)
    return sorted(
        url for url, indexable in landings.items() if indexable and url not in mentioned
    )
