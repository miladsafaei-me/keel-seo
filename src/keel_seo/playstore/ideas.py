"""Turn scored phrases into app ideas a builder can choose between.

The phrase score in :mod:`.score` says where demand meets weak apps. A builder
needs two more answers before that is an opportunity:

*Do people pay here?* Monetization (0 to 1) is read from the relevant apps:
the share that sells something (in-app products or a paid install), and whether
any of them with real reach (10 thousand installs or more) does. Measured on the
first run, 2026-11-08: the only clear payment signal under "prop firm" was a
challenge simulator selling items up to $999, which the phrase score could not
see at all.

*Can anyone but the brand win it?* A phrase like "fundednext prop firm" is a
person looking for one company's own app, and no third-party app can win it. A
word of the phrase that is not in the seed marks a brand query when any of these
holds: it is on the run's ``--brands`` list; it is in the developer name of one
of the top five apps; or no top app's title or short description uses it at all.
The last test is what catches a brand with no app: measured 2026-11-08,
FundedNext has no Play app, so "fundednext prop firm app" ranked only other
companies, none of which say "fundednext", while a generic word like
"simulator" or "calculator" is always in some listing.

The app score (0 to 100) is ``35% demand + 30% weakness + 35% monetization``,
times 0.25 for a brand query. Unserved phrases have no score; a person judges
them. Phrases whose top apps overlap by half or more are one need, because Play
answered them with the same apps, and are grouped into one idea.
"""
from __future__ import annotations

import re

from .score import tokens

APP_WEIGHTS = {"demand": 0.35, "weakness": 0.30, "monetization": 0.35}
BRAND_FACTOR = 0.25
REACH_INSTALLS = 10_000
SAME_NEED = 0.5
TIERS = ((45, "build candidate"), (30, "worth a look"), (0, "weak"))

PRICE = re.compile(r"\$\s?([0-9]+(?:\.[0-9]+)?)")


def max_price(app: dict) -> float:
    """The top of the in-app price range, or the install price, in dollars."""
    prices = [float(p) for p in PRICE.findall(app.get("inAppProductPrice") or "")]
    if app.get("free") is False and app.get("price"):
        prices.append(float(app["price"]))
    return max(prices, default=0.0)


def sells(app: dict) -> bool:
    return bool(app.get("offersIAP")) or app.get("free") is False


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def brand_word(phrase: str, apps: list[dict], seed_words: set[str],
               brands: frozenset[str] = frozenset()) -> str:
    """The word that makes this phrase one company's query, or ''."""
    squashed = _squash(phrase)
    for brand in brands:
        if brand and _squash(brand) in squashed:
            return brand
    extra = [w for w in tokens(phrase) - seed_words if len(w) >= 3 and not w.isdigit()]
    developers = [_squash(a.get("developer")) for a in apps[:5]]
    vocabulary: set[str] = set()
    for app in apps:
        vocabulary |= tokens(app.get("title")) | tokens(app.get("summary"))
    for word in extra:
        if any(word in dev for dev in developers) or word not in vocabulary:
            return word
    return ""


def app_opportunity(score, seed_words: set[str], brands: frozenset[str] = frozenset()) -> dict:
    """The builder's view of one phrase: monetization, brand, app score, verdict."""
    relevant = score.relevant
    out = {"monetization": 0.0, "max_price": 0.0, "brand": "", "app_score": None,
           "verdict": "unserved, judge by hand" if score.unserved else ""}
    if score.unserved or not relevant:
        return out
    selling = [a for a in relevant if sells(a)]
    reach = any(sells(a) and (a.get("realInstalls") or 0) >= REACH_INSTALLS for a in relevant)
    out["monetization"] = round(0.6 * len(selling) / len(relevant) + 0.4 * reach, 2)
    out["max_price"] = max((max_price(a) for a in relevant), default=0.0)
    out["brand"] = brand_word(score.phrase, score.apps, seed_words, brands)
    value = 100 * (APP_WEIGHTS["demand"] * score.demand
                   + APP_WEIGHTS["weakness"] * score.weakness
                   + APP_WEIGHTS["monetization"] * out["monetization"])
    if out["brand"]:
        value *= BRAND_FACTOR
    out["app_score"] = round(value, 1)
    out["verdict"] = (f"brand query ({out['brand']})" if out["brand"]
                      else next(label for floor, label in TIERS if value >= floor))
    return out


def group_ideas(rows: list[dict], top_ids: dict[str, list[str]]) -> list[dict]:
    """Group phrases Play answered with mostly the same apps into one idea.

    ``rows`` are keyword rows already carrying ``app_score``; the best-scoring
    phrase of each group leads it and names it.
    """
    ordered = sorted(rows, key=lambda r: -(r.get("app_score") or -1))
    ideas: list[dict] = []
    for row in ordered:
        ids = set(top_ids.get(row["phrase"], []))
        home = None
        for idea in ideas:
            union = ids | idea["_ids"]
            if union and len(ids & idea["_ids"]) / len(union) >= SAME_NEED:
                home = idea
                break
        if home is None:
            home = {"idea": row["phrase"], "_ids": ids, "phrases": [], "lead": row}
            ideas.append(home)
        home["phrases"].append(row["phrase"])
        row["idea"] = home["idea"]
    out = []
    for idea in ideas:
        lead = idea["lead"]
        out.append({
            "idea": idea["idea"], "app_score": lead.get("app_score"),
            "verdict": lead.get("verdict"), "phrases_count": len(idea["phrases"]),
            "phrases": " | ".join(idea["phrases"]), "demand": lead.get("demand"),
            "weakness": lead.get("weakness"), "monetization": lead.get("monetization"),
            "max_price": lead.get("max_price"), "installs_total": lead.get("installs_total"),
            "reasons": lead.get("reasons"), "top_apps": lead.get("top_apps"),
        })
    return out
