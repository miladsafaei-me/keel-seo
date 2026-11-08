"""Score one phrase: how much demand, and how weakly it is answered.

Every input is a public number from the top ``top`` apps Play ranks for the
phrase. Nothing is a volume; the score ranks phrases against each other within
one run, and it is never a forecast of downloads or revenue.

*Relevant apps first.* Play fills a thin result page with unrelated giants:
measured 2026-11-08, "lucid prop firm" ranked Lucidchart and LinkedIn, and their
billions of installs made a niche phrase look like the biggest market in the run.
So an app counts only when its title and short description name at least half of
the phrase's words. The rest are kept, as a sign of how little Play has to show.

Demand (0 to 1) is the log of the installs the relevant apps collected, scaled
between 10 thousand and 100 million.

Weakness (0 to 1) is a weighted mix of four shares:

* relevant apps rated under 4.0, or not rated at all (35%),
* top slots taken by apps that do not name the need (25%),
* relevant apps not updated for over a year, among those with a date (20%),
* relevant apps that are not strong incumbents (20%): a strong incumbent has
  over a million installs, a rating of 4.3 or more, and an update in the last
  six months.

Opportunity is ``100 * demand * weakness``. A phrase the store offers but no
relevant app answers is marked ``unserved``: its demand cannot be read from
installs, so it scores zero and is listed for a person to judge.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field

STALE_DAYS = 365
FRESH_DAYS = 180
WEAK_RATING = 4.0
STRONG_RATING = 4.3
STRONG_INSTALLS = 1_000_000
DEMAND_FLOOR = 4.0
DEMAND_CEILING = 8.0
WEIGHTS = {"weak_rating": 0.35, "off_topic": 0.25, "stale": 0.20, "no_incumbent": 0.20}

STOPWORDS = frozenset("a an the for of to in on and or with app apps best free my "
                      "your how what is are by at".split())


@dataclass
class KeywordScore:
    phrase: str
    apps: list[dict] = field(default_factory=list)
    relevant: list[dict] = field(default_factory=list)
    installs_total: int = 0
    installs_median: int = 0
    rating_avg: float | None = None
    relevant_share: float = 0.0
    weak_rating_share: float = 0.0
    stale_share: float = 0.0
    strong_incumbents: int = 0
    iap_share: float = 0.0
    paid_share: float = 0.0
    demand: float = 0.0
    weakness: float = 0.0
    opportunity: float = 0.0
    unserved: bool = False
    reasons: str = ""


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def is_relevant(wanted: set[str], app: dict) -> bool:
    """Whether the app's title and short description name half the phrase."""
    if not wanted:
        return False
    have = tokens(app.get("title")) | tokens(app.get("summary"))
    return len(wanted & have) >= math.ceil(len(wanted) / 2)


def _age_days(updated, now: float) -> float | None:
    if not updated:
        return None
    try:
        return (now - float(updated)) / 86400.0
    except (TypeError, ValueError):
        return None


def score_keyword(phrase: str, apps: list[dict], top: int = 10,
                  now: float | None = None) -> KeywordScore:
    now = now or time.time()
    result = KeywordScore(phrase=phrase)
    apps = [a for a in apps if a][:top]
    result.apps = apps
    if not apps:
        result.unserved = True
        result.reasons = "no apps rank for this phrase"
        return result
    wanted = tokens(phrase)
    relevant = [a for a in apps if is_relevant(wanted, a)]
    result.relevant = relevant
    result.relevant_share = len(relevant) / len(apps)
    if not relevant:
        result.unserved = True
        result.reasons = (f"none of the top {len(apps)} apps names the need; "
                          "demand unknown, judge by hand")
        return result
    n = len(relevant)
    installs = [int(a.get("realInstalls") or 0) for a in relevant]
    ratings = [a.get("score") for a in relevant if a.get("score")]
    weak = stale = dated = strong = iap = paid = 0
    for app in relevant:
        score = app.get("score")
        age = _age_days(app.get("updated"), now)
        if not score or score < WEAK_RATING:
            weak += 1
        if age is not None:
            dated += 1
            if age > STALE_DAYS:
                stale += 1
        if (int(app.get("realInstalls") or 0) >= STRONG_INSTALLS
                and (score or 0) >= STRONG_RATING and age is not None and age <= FRESH_DAYS):
            strong += 1
        if app.get("offersIAP"):
            iap += 1
        if app.get("free") is False:
            paid += 1
    result.installs_total = sum(installs)
    result.installs_median = int(statistics.median(installs))
    result.rating_avg = round(statistics.mean(ratings), 2) if ratings else None
    result.weak_rating_share = weak / n
    # An app whose update date could not be read is left out, not counted stale.
    result.stale_share = stale / dated if dated else 0.0
    result.strong_incumbents = strong
    result.iap_share = iap / n
    result.paid_share = paid / n
    level = math.log10(result.installs_total + 1)
    result.demand = min(max((level - DEMAND_FLOOR) / (DEMAND_CEILING - DEMAND_FLOOR), 0.0), 1.0)
    result.weakness = (WEIGHTS["weak_rating"] * result.weak_rating_share
                       + WEIGHTS["off_topic"] * (1 - result.relevant_share)
                       + WEIGHTS["stale"] * result.stale_share
                       + WEIGHTS["no_incumbent"] * (1 - strong / n))
    result.opportunity = round(100 * result.demand * result.weakness, 1)
    notes = [f"{n}/{len(apps)} apps name the need"]
    if result.weak_rating_share >= 0.5:
        notes.append(f"{weak}/{n} of them rated under {WEAK_RATING} or unrated")
    if result.stale_share >= 0.4:
        notes.append(f"{stale}/{dated} not updated in a year")
    if strong == 0:
        notes.append("no strong incumbent")
    if result.demand < 0.25:
        notes.append("little install demand")
    result.reasons = "; ".join(notes)
    return result
