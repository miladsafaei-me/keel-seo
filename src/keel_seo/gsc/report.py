"""The Search Console report: pure transforms, one structure for every property.

Every property gets the same report, whatever its size, owner or permission level:
the same date ranges, the same comparison rules, the same tables and the same
insight panels. Nothing here reads a file, a setting or the network. The fetch layer
(:mod:`keel_seo.gsc.report_api`) hands in raw Search Analytics rows and this module
turns them into the payload the template renders.

Rules every figure follows, because each one was a bug in the dashboard this replaces:

* Totals come from the ``date`` dimension, never from summed query rows. Google
  hides rare queries from every query-level breakdown, so query rows undercount.
* A day Google is still processing (on or after the API's ``firstIncompleteDate``)
  is never part of a period. The comparison period always has exactly the same
  number of days as the current one.
* Every panel built from query rows states how much of the traffic those rows
  cover, and the brand split carries the hidden share as its own row.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
import statistics
from urllib.parse import urlsplit

# Rows kept per table, the same ceiling Search Console's own tables show.
TABLE_ROWS = 1000
# The API's ceiling for one request; a dimension that returns this many rows was cut.
ROW_CAP = 25000
# Search Console keeps sixteen months of data.
HISTORY_MONTHS = 16
# A position bucket with fewer impressions than this is too small to read a CTR from.
CTR_MIN_SAMPLE = 100
# The query+page pull is the slowest one on a large property (99 s for sixteen months
# on a 70k-clicks-a-day site, 5 s for 28 days), so it covers at most this many days.
QUERY_PAGE_MAX_DAYS = 28

RANGES = [
    ("7d", "Last 7 days"),
    ("28d", "Last 28 days"),
    ("3m", "Last 3 months"),
    ("6m", "Last 6 months"),
    ("12m", "Last 12 months"),
    ("16m", "Last 16 months"),
]
RANGE_LABELS = dict(RANGES)
DEFAULT_RANGE = "28d"

COMPARES = [
    ("previous", "Previous period"),
    ("year", "Same period last year"),
    ("none", "No comparison"),
]
COMPARE_LABELS = dict(COMPARES)
DEFAULT_COMPARE = "previous"

SEARCH_TYPES = [("web", "Web"), ("image", "Image"), ("video", "Video"), ("news", "News")]
SEARCH_TYPE_LABELS = dict(SEARCH_TYPES)

# The four filters Search Console offers, with the operators it offers for each.
FILTER_DIMENSIONS = {
    "query": ("Query", ("contains", "notContains", "equals")),
    "page": ("Page", ("contains", "notContains", "equals")),
    "country": ("Country", ("equals",)),
    "device": ("Device", ("equals",)),
}
OPERATOR_LABELS = {"contains": "contains", "notContains": "does not contain", "equals": "is"}
DEVICES = ("DESKTOP", "MOBILE", "TABLET")

POSITION_BANDS = [
    (1, 3, "1-3", "Top of page 1"),
    (4, 10, "4-10", "Rest of page 1"),
    (11, 20, "11-20", "Page 2"),
    (21, 10_000, "21+", "Page 3 and deeper"),
]

# A locale first segment (/en, /fr, /pt-br): the site section is the segment after it.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2,4})?$", re.I)


# ---- dates ----

def add_months(day: dt.date, months: int) -> dt.date:
    """The same day ``months`` later (or earlier), clamped to the month's last day."""
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def earliest_date(today: dt.date) -> dt.date:
    """The first day Search Console still holds data for."""
    return add_months(today, -HISTORY_MONTHS) + dt.timedelta(days=1)


def parse_date(raw) -> "dt.date | None":
    try:
        return dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def resolve_range(key: str, last_complete: dt.date, today: dt.date,
                  start: "str | None" = None, end: "str | None" = None) -> dict:
    """The current period: a preset ending on the last complete day, or a custom span
    clamped to what Search Console holds. Returns ``{key, label, start, end, days}``."""
    first = earliest_date(today)
    s, e = parse_date(start), parse_date(end)
    if key == "custom" and s and e:
        e = min(e, last_complete)
        s = max(min(s, e), first)
        label = "Custom"
    else:
        key = key if key in RANGE_LABELS else DEFAULT_RANGE
        e = last_complete
        if key.endswith("d"):
            s = e - dt.timedelta(days=int(key[:-1]) - 1)
        else:
            s = add_months(e, -int(key[:-1])) + dt.timedelta(days=1)
        s = max(s, first)
        label = RANGE_LABELS[key]
    return {"key": key, "label": label, "start": s, "end": e, "days": (e - s).days + 1}


def resolve_compare(mode: str, current: dict, today: dt.date) -> "dict | None":
    """The comparison period, always exactly as long as the current one, or None when
    there is none or Search Console no longer holds it."""
    mode = mode if mode in COMPARE_LABELS else DEFAULT_COMPARE
    if mode == "none":
        return None
    days = current["days"]
    if mode == "year":
        e = add_months(current["end"], -12)
    else:
        e = current["start"] - dt.timedelta(days=1)
    s = e - dt.timedelta(days=days - 1)
    if s < earliest_date(today):
        return {"mode": mode, "label": COMPARE_LABELS[mode], "available": False}
    return {"mode": mode, "label": COMPARE_LABELS[mode], "available": True,
            "start": s, "end": e, "days": days}


def fmt_span(start: dt.date, end: dt.date) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{start:%b} {start.day} – {end.day}, {end.year}"
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


# ---- rows ----

def totals_of(rows: list) -> dict:
    """Clicks, impressions, CTR (a percent) and the impression-weighted position."""
    clicks = sum(int(r.get("clicks", 0)) for r in rows)
    impr = sum(int(r.get("impressions", 0)) for r in rows)
    wpos = sum(float(r.get("position", 0.0)) * int(r.get("impressions", 0)) for r in rows)
    return {"clicks": clicks, "impressions": impr,
            "ctr": round(clicks / impr * 100, 2) if impr else 0.0,
            "position": round(wpos / impr, 1) if impr else None}


def _row(r: dict) -> dict:
    impr = int(r.get("impressions", 0))
    clicks = int(r.get("clicks", 0))
    return {"clicks": clicks, "impressions": impr,
            "ctr": round(clicks / impr * 100, 2) if impr else 0.0,
            "position": round(float(r.get("position", 0.0)), 1) if impr else None}


def keyed(rows: list) -> dict:
    """``{first key: metrics}`` for one-dimension rows."""
    out = {}
    for r in rows or []:
        keys = r.get("keys") or []
        if keys:
            out[keys[0]] = _row(r)
    return out


def in_period(rows: list, start: dt.date, end: dt.date) -> list:
    """Date rows that fall inside [start, end]."""
    s, e = start.isoformat(), end.isoformat()
    return [r for r in rows or [] if s <= (r.get("keys") or [""])[0] <= e]


def change(cur, prev, kind: str) -> "dict | None":
    """One figure's change. ``pct`` for counts, ``pp`` for CTR, ``places`` for position
    (positive = moved up). ``good`` says whether the change is an improvement."""
    if prev is None or cur is None:
        return None
    if kind == "ctr":
        diff = round(cur - prev, 2)
        return {"value": diff, "unit": "pp", "good": diff > 0, "flat": diff == 0}
    if kind == "position":
        if not prev or not cur:
            return None
        diff = round(prev - cur, 1)
        return {"value": diff, "unit": "places", "good": diff > 0, "flat": diff == 0}
    if not prev:
        return {"value": None, "unit": "new", "good": cur > 0, "flat": cur == 0}
    pct = round((cur - prev) / prev * 100, 1)
    return {"value": pct, "unit": "%", "good": pct > 0, "flat": pct == 0}


def changes(cur: dict, prev: "dict | None") -> dict:
    if not prev:
        return {}
    return {k: change(cur.get(k), prev.get(k), k if k in ("ctr", "position") else "count")
            for k in ("clicks", "impressions", "ctr", "position")}


# ---- the performance chart ----

def daily_series(cur_rows: list, prev_rows: list, current: dict, compare: "dict | None") -> list:
    """One entry per day of the current period, with the comparison day at the same
    offset (day 1 against day 1), so the two lines overlay."""
    cur = keyed(cur_rows)
    prev = keyed(prev_rows) if compare and compare.get("available") else {}
    zero = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None}
    out = []
    for i in range(current["days"]):
        day = current["start"] + dt.timedelta(days=i)
        entry = {"date": day.isoformat(), **cur.get(day.isoformat(), zero)}
        if prev or (compare and compare.get("available")):
            pday = compare["start"] + dt.timedelta(days=i)
            p = prev.get(pday.isoformat(), zero)
            entry["prev"] = {"date": pday.isoformat(), **p}
        out.append(entry)
    return out


# ---- breakdown tables ----

def table(cur_rows: list, prev_rows: "list | None", limit: int = TABLE_ROWS) -> dict:
    """Search Console's breakdown table for one dimension: the top rows by clicks, each
    with its comparison-period figures. ``total`` is the full row count Google sent,
    ``cut`` says whether Google's row ceiling cut it."""
    prev = keyed(prev_rows) if prev_rows is not None else None
    rows = []
    for key, m in keyed(cur_rows).items():
        row = {"key": key, **m}
        if prev is not None:
            row["prev"] = prev.get(key)
        rows.append(row)
    rows.sort(key=lambda r: (-r["clicks"], -r["impressions"], r["key"]))
    return {"rows": rows[:limit], "total": len(rows), "cut": len(cur_rows or []) >= ROW_CAP}


def date_table(series: list) -> dict:
    rows = []
    for d in reversed(series):
        row = {"key": d["date"], **{k: d[k] for k in ("clicks", "impressions", "ctr", "position")}}
        if "prev" in d:
            row["prev"] = {k: d["prev"][k] for k in ("clicks", "impressions", "ctr", "position")}
        rows.append(row)
    return {"rows": rows, "total": len(rows), "cut": False}


# ---- coverage of the query rows ----

def coverage(query_rows: list, totals: dict) -> dict:
    """How much of the true traffic the query rows describe. Google leaves rare
    queries out of every query breakdown, so the rest is hidden, not lost."""
    q = totals_of(query_rows or [])
    tc, ti = totals.get("clicks", 0), totals.get("impressions", 0)
    return {
        "clicks": q["clicks"], "impressions": q["impressions"],
        "clicks_pct": round(min(q["clicks"], tc) / tc * 100, 1) if tc else None,
        "impressions_pct": round(min(q["impressions"], ti) / ti * 100, 1) if ti else None,
        "cut": len(query_rows or []) >= ROW_CAP,
    }


# ---- brand ----

def brand_rules(spec) -> list:
    """Compile ``[(label, regex), ...]``; anything else means no brand rules."""
    out = []
    for item in spec or []:
        try:
            label, rx = item
            out.append((label, re.compile(rx, re.I)))
        except (TypeError, ValueError, re.error):
            continue
    return out


def brand_split(query_rows: list, totals: dict, rules: list) -> "dict | None":
    """Branded, non-branded and hidden traffic. Hidden is what the query rows do not
    describe: rare queries Google keeps out of every query breakdown."""
    if not rules:
        return None
    b = {"clicks": 0, "impressions": 0, "queries": 0}
    n = {"clicks": 0, "impressions": 0, "queries": 0}
    for r in query_rows or []:
        q = (r.get("keys") or [""])[0]
        bucket = b if any(rx.search(q) for _l, rx in rules) else n
        bucket["clicks"] += int(r.get("clicks", 0))
        bucket["impressions"] += int(r.get("impressions", 0))
        bucket["queries"] += 1
    hidden = {"clicks": max(0, totals.get("clicks", 0) - b["clicks"] - n["clicks"]),
              "impressions": max(0, totals.get("impressions", 0) - b["impressions"] - n["impressions"]),
              "queries": None}
    rows = [("brand", "Branded", b), ("nonbrand", "Non-branded", n), ("hidden", "Hidden by Google", hidden)]
    tc, ti = totals.get("clicks", 0) or 0, totals.get("impressions", 0) or 0
    return {
        "names": [label for label, _rx in rules],
        "rows": [{"key": k, "label": label, **v,
                  "clicks_pct": round(v["clicks"] / tc * 100, 1) if tc else 0.0,
                  "impressions_pct": round(v["impressions"] / ti * 100, 1) if ti else 0.0}
                 for k, label, v in rows],
    }


# ---- ranking ----

def position_bands(query_rows: list, prev_rows: "list | None" = None) -> list:
    """Queries, impressions and clicks per position band, over the query rows."""
    def agg(rows):
        out = {label: {"queries": 0, "impressions": 0, "clicks": 0} for _lo, _hi, label, _b in POSITION_BANDS}
        for r in rows or []:
            pos = float(r.get("position", 0.0))
            for lo, hi, label, _b in POSITION_BANDS:
                if lo <= round(pos) <= hi or (label == "1-3" and 0 < pos < 1):
                    a = out[label]
                    a["queries"] += 1
                    a["impressions"] += int(r.get("impressions", 0))
                    a["clicks"] += int(r.get("clicks", 0))
                    break
        return out

    cur = agg(query_rows)
    prev = agg(prev_rows) if prev_rows is not None else None
    tq = sum(v["queries"] for v in cur.values()) or 1
    ti = sum(v["impressions"] for v in cur.values()) or 1
    tc = sum(v["clicks"] for v in cur.values()) or 1
    out = []
    for _lo, _hi, label, blurb in POSITION_BANDS:
        v = cur[label]
        out.append({"label": label, "blurb": blurb, **v,
                    "queries_pct": round(v["queries"] / tq * 100, 1),
                    "impressions_pct": round(v["impressions"] / ti * 100, 1),
                    "clicks_pct": round(v["clicks"] / tc * 100, 1),
                    "prev_queries": prev[label]["queries"] if prev else None})
    return out


def _ctr_bucket(pos: float) -> "str | None":
    if not pos:
        return None
    if pos < 10.5:
        return str(max(1, int(round(pos))))
    if pos < 20.5:
        return "11-20"
    return "21+"


CTR_BUCKETS = [str(n) for n in range(1, 11)] + ["11-20", "21+"]


def ctr_curve(query_rows: list) -> list:
    """The property's own click rate at each position. A bucket under
    ``CTR_MIN_SAMPLE`` impressions is flagged as too small to read."""
    agg = {b: {"clicks": 0, "impressions": 0} for b in CTR_BUCKETS}
    for r in query_rows or []:
        b = _ctr_bucket(float(r.get("position", 0.0)))
        if b:
            agg[b]["clicks"] += int(r.get("clicks", 0))
            agg[b]["impressions"] += int(r.get("impressions", 0))
    return [{"bucket": b, **a, "ctr": round(a["clicks"] / a["impressions"] * 100, 2) if a["impressions"] else None,
             "small": a["impressions"] < CTR_MIN_SAMPLE}
            for b, a in agg.items() if a["impressions"]]


# ---- opportunities ----

def _is_noise(query: str) -> bool:
    ql = query.lower()
    return "site:" in ql or ql.startswith("http") or ".com/" in ql


def min_impressions(days: int) -> int:
    """The impressions a query needs before it counts as an opportunity: enough to
    matter, scaled to the length of the period."""
    return max(10, round(days * 0.7))


def landing_pages(qp_rows: list) -> dict:
    """query -> the page that earns most of its impressions."""
    best: dict = {}
    for r in qp_rows or []:
        keys = r.get("keys") or []
        if len(keys) < 2:
            continue
        q, page, impr = keys[0], keys[1], int(r.get("impressions", 0))
        if q not in best or impr > best[q][1]:
            best[q] = (page, impr)
    return {q: v[0] for q, v in best.items()}


def striking_distance(query_rows: list, days: int, pages: dict, limit: int = 200) -> list:
    """Queries that rank just off the top (positions 4 to 20) with real demand."""
    floor = min_impressions(days)
    out = []
    for r in query_rows or []:
        q = (r.get("keys") or [""])[0]
        m = _row(r)
        if m["position"] and 3.5 <= m["position"] <= 20.5 and m["impressions"] >= floor and not _is_noise(q):
            out.append({"key": q, **m, "page": pages.get(q)})
    out.sort(key=lambda x: (-x["impressions"], x["position"]))
    return out[:limit]


def low_ctr(query_rows: list, curve: list, days: int, pages: dict, limit: int = 200) -> list:
    """Page-one queries earning well under the property's own click rate for their
    position; ``missed`` estimates the clicks that gap costs."""
    expected = {c["bucket"]: c["ctr"] for c in curve if not c["small"] and c["ctr"]}
    floor = min_impressions(days)
    out = []
    for r in query_rows or []:
        q = (r.get("keys") or [""])[0]
        m = _row(r)
        pos = m["position"]
        if not pos or pos > 10.5 or m["impressions"] < floor or _is_noise(q):
            continue
        exp = expected.get(_ctr_bucket(pos))
        if exp and m["ctr"] < exp * 0.6:
            missed = round((exp - m["ctr"]) / 100 * m["impressions"])
            if missed >= 1:
                out.append({"key": q, **m, "expected_ctr": exp, "missed": missed, "page": pages.get(q)})
    out.sort(key=lambda x: -x["missed"])
    return out[:limit]


def cannibalization(qp_rows: list, limit: int = 200) -> list:
    """Queries where two or more of the property's pages each take a real share of
    the impressions, so Google splits the query between them."""
    byq: dict = {}
    for r in qp_rows or []:
        keys = r.get("keys") or []
        if len(keys) >= 2:
            byq.setdefault(keys[0], []).append({"page": keys[1], **_row(r)})
    out = []
    for q, pages in byq.items():
        total = sum(p["impressions"] for p in pages)
        competing = [p for p in pages if p["impressions"] >= 10 and p["impressions"] >= total * 0.1]
        if len(competing) >= 2 and not _is_noise(q):
            competing.sort(key=lambda p: -p["impressions"])
            out.append({"key": q, "pages": competing, "count": len(competing),
                        "impressions": total, "clicks": sum(p["clicks"] for p in pages)})
    out.sort(key=lambda x: (-x["impressions"], -x["count"]))
    return out[:limit]


def new_and_lost(cur_rows: list, prev_rows: "list | None", limit: int = 200) -> dict:
    """Queries seen now but not in the comparison period, and queries that earned
    clicks then and none now."""
    if prev_rows is None:
        return {"new": [], "lost": []}
    cur, prev = keyed(cur_rows), keyed(prev_rows)
    new = [{"key": q, **m} for q, m in cur.items() if q not in prev and not _is_noise(q)]
    new.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
    lost = [{"key": q, **m, "now": cur.get(q)} for q, m in prev.items()
            if m["clicks"] > 0 and (cur.get(q) or {}).get("clicks", 0) == 0 and not _is_noise(q)]
    lost.sort(key=lambda x: (-x["clicks"], -x["impressions"]))
    return {"new": new[:limit], "lost": lost[:limit]}


def movers(cur_rows: list, prev_rows: "list | None", top: int = 10) -> dict:
    """The biggest click gains and losses between the two periods."""
    if prev_rows is None:
        return {"up": [], "down": []}
    cur, prev = keyed(cur_rows), keyed(prev_rows)
    rows = []
    for k in set(cur) | set(prev):
        c = cur.get(k, {}).get("clicks", 0)
        p = prev.get(k, {}).get("clicks", 0)
        if c != p:
            rows.append({"key": k, "clicks": c, "prev_clicks": p, "delta": c - p})
    up = sorted((r for r in rows if r["delta"] > 0), key=lambda r: (-r["delta"], r["key"]))[:top]
    down = sorted((r for r in rows if r["delta"] < 0), key=lambda r: (r["delta"], r["key"]))[:top]
    return {"up": up, "down": down}


# ---- site sections ----

def section_of(page: str) -> str:
    """The site section a URL belongs to: its first path segment, or its first two
    when the first is a locale (/en/brokers), so a one-language site is not one row."""
    parts = [p for p in urlsplit(page).path.split("/") if p]
    if not parts:
        return "/"
    if _LOCALE_SEGMENT.match(parts[0]) and len(parts) > 1:
        return f"/{parts[0]}/{parts[1]}"
    return "/" + parts[0]


def sections(page_rows: list, prev_rows: "list | None", top: int = 15) -> dict:
    def agg(rows):
        out: dict = {}
        for r in rows or []:
            keys = r.get("keys") or []
            if not keys:
                continue
            s = out.setdefault(section_of(keys[0]), {"clicks": 0, "impressions": 0, "pages": 0})
            s["clicks"] += int(r.get("clicks", 0))
            s["impressions"] += int(r.get("impressions", 0))
            s["pages"] += 1
        return out

    cur, prev = agg(page_rows), agg(prev_rows) if prev_rows is not None else None
    tc = sum(v["clicks"] for v in cur.values()) or 0
    ti = sum(v["impressions"] for v in cur.values()) or 0
    rows = [{"key": k, **v,
             "clicks_pct": round(v["clicks"] / tc * 100, 1) if tc else 0.0,
             "impressions_pct": round(v["impressions"] / ti * 100, 1) if ti else 0.0,
             "prev": prev.get(k) if prev is not None else None}
            for k, v in cur.items()]
    rows.sort(key=lambda r: (-r["clicks"], -r["impressions"], r["key"]))
    head, tail = rows[:top], rows[top:]
    if tail:
        other = {"key": f"{len(tail)} other sections", "other": True,
                 "clicks": sum(r["clicks"] for r in tail), "impressions": sum(r["impressions"] for r in tail),
                 "pages": sum(r["pages"] for r in tail), "prev": None}
        other["clicks_pct"] = round(other["clicks"] / tc * 100, 1) if tc else 0.0
        other["impressions_pct"] = round(other["impressions"] / ti * 100, 1) if ti else 0.0
        head.append(other)
    return {"rows": head, "total": len(rows)}


# ---- sixteen-month history and sudden breaks ----

def weekly(history_rows: list) -> list:
    """The daily history folded into weeks starting on Monday. A week with fewer than
    seven days of data (the first and the last) is marked partial."""
    weeks: dict = {}
    for r in history_rows or []:
        d = parse_date((r.get("keys") or [""])[0])
        if not d:
            continue
        monday = d - dt.timedelta(days=d.weekday())
        w = weeks.setdefault(monday, {"clicks": 0, "impressions": 0, "days": 0, "wpos": 0.0})
        w["clicks"] += int(r.get("clicks", 0))
        w["impressions"] += int(r.get("impressions", 0))
        w["wpos"] += float(r.get("position", 0.0)) * int(r.get("impressions", 0))
        w["days"] += 1
    out = []
    for monday in sorted(weeks):
        w = weeks[monday]
        out.append({"week": monday.isoformat(), "clicks": w["clicks"], "impressions": w["impressions"],
                    "ctr": round(w["clicks"] / w["impressions"] * 100, 2) if w["impressions"] else 0.0,
                    "position": round(w["wpos"] / w["impressions"], 1) if w["impressions"] else None,
                    "partial": w["days"] < 7})
    return out


# A break needs a real baseline: this many impressions (or clicks) a day before it.
EVENT_MIN_BASELINE = {"impressions": 20, "clicks": 5}
EVENT_DROP = 0.5    # the next seven days average at most half the baseline
EVENT_SPIKE = 2.0   # or at least twice it
EVENT_BEFORE = 28   # baseline: the median day of the four weeks before
EVENT_AFTER = 7


def events(history_rows: list, last_complete: dt.date) -> list:
    """Sudden breaks in the daily history: a day after which the level of impressions
    or clicks halves or doubles against the four weeks before, and stays there for a
    week. Each says when, by how much, and whether the level has come back."""
    by_day = {r["keys"][0]: r for r in history_rows or [] if r.get("keys")}
    if not by_day:
        return []
    first = min(parse_date(d) for d in by_day)
    days = [(first + dt.timedelta(days=i)) for i in range((last_complete - first).days + 1)]
    found = []
    for metric in ("impressions", "clicks"):
        vals = [int(by_day.get(d.isoformat(), {}).get(metric, 0)) for d in days]
        i = EVENT_BEFORE
        while i + EVENT_AFTER <= len(vals):
            base = statistics.median(vals[i - EVENT_BEFORE:i])
            if base >= EVENT_MIN_BASELINE[metric]:
                after = sum(vals[i:i + EVENT_AFTER]) / EVENT_AFTER
                kind = None
                if after <= base * EVENT_DROP and vals[i] <= base * 0.75:
                    kind = "drop"
                elif after >= base * EVENT_SPIKE and vals[i] >= base * 1.5:
                    kind = "rise"
                if kind:
                    recent = vals[-14:]
                    now = sum(recent) / len(recent)
                    back = now >= base * 0.8 if kind == "drop" else now <= base * 1.25
                    found.append({
                        "metric": metric, "kind": kind, "date": days[i].isoformat(),
                        "before": round(base, 1), "after": round(after, 1), "now": round(now, 1),
                        "change_pct": round((after - base) / base * 100),
                        "recovered": back,
                    })
                    i += EVENT_BEFORE
                    continue
            i += 1
    # One break seen in both metrics within a few days is one event: keep impressions.
    found.sort(key=lambda e: (e["date"], e["metric"] != "impressions"))
    merged: list = []
    for e in found:
        d = parse_date(e["date"])
        twin = next((m for m in merged if m["kind"] == e["kind"]
                     and abs((parse_date(m["date"]) - d).days) <= 3), None)
        if twin:
            twin.setdefault("also", []).append(e)
            continue
        merged.append(e)
    merged.sort(key=lambda e: e["date"], reverse=True)
    return merged


# ---- sitemaps ----

def sitemaps(entries: list) -> list:
    out = []
    for e in entries or []:
        submitted = sum(int(c.get("submitted", 0) or 0) for c in e.get("contents") or [])
        out.append({
            "path": e.get("path", ""),
            "index": bool(e.get("isSitemapsIndex")),
            "submitted_at": _day((e.get("lastSubmitted") or "")[:10]) if e.get("lastSubmitted") else "",
            "read_at": _day((e.get("lastDownloaded") or "")[:10]) if e.get("lastDownloaded") else "",
            "pending": bool(e.get("isPending")),
            "errors": int(e.get("errors", 0) or 0),
            "warnings": int(e.get("warnings", 0) or 0),
            "urls": submitted,
        })
    out.sort(key=lambda s: s["path"])
    return out


# ---- words for the page ----

def compact(value, kind: str = "count") -> str:
    """A figure the way Search Console's tiles print it: 67, 1.73K, 12.3M, 3.9%, 15.8."""
    if value is None:
        return "-"
    if kind == "ctr":
        return f"{value:.1f}%"
    if kind == "position":
        return f"{value:.1f}"
    v = float(value)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            n = v / div
            digits = 2 if n < 10 else (1 if n < 100 else 0)
            return f"{n:.{digits}f}".rstrip("0").rstrip(".") + suffix
    return f"{int(v):,}"


def _day(iso: str) -> str:
    d = parse_date(iso)
    return f"{d:%b} {d.day}, {d.year}" if d else iso


def _per_day(value: float) -> str:
    return f"{value:,.0f}" if value >= 10 else f"{value:,.1f}"


def event_sentence(e: dict) -> str:
    verb = "fell" if e["kind"] == "drop" else "rose"
    metric = e["metric"].capitalize()
    text = (f"{metric} {verb} {abs(e['change_pct'])}% on {_day(e['date'])}: from "
            f"{_per_day(e['before'])} a day to {_per_day(e['after'])} a day.")
    if e["recovered"]:
        text += " The level has since come back."
    else:
        text += f" The last two weeks average {_per_day(e['now'])} a day."
    return text


def alerts(payload: dict, recent_days: int = 120) -> list:
    """The breaks a reader must not miss: any inside the range shown, and any in the
    last four months that has not come back."""
    start = payload["range"]["start"]
    last = parse_date(payload["last_complete"])
    recent = (last - dt.timedelta(days=recent_days)).isoformat() if last else start
    out = []
    for e in payload.get("events") or []:
        if e["date"] >= start or (e["date"] >= recent and not e["recovered"]):
            out.append({**e, "text": event_sentence(e), "severe": e["kind"] == "drop" and not e["recovered"]})
    return out


def notes(payload: dict) -> list:
    """What the reader should know about the data itself, in plain words."""
    out = []
    partial = payload.get("partial_days") or []
    last = _day(payload["last_complete"])
    if partial:
        names = [f"{parse_date(d):%b} {parse_date(d).day}" for d in partial]
        days = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        out.append(f"Figures run to {last}, the last day Google has finished. "
                   f"Google is still processing {days}, so those days are not counted yet.")
    else:
        out.append(f"Figures run to {last}, the last day Google has finished.")
    comp = payload.get("compare")
    if comp and comp.get("available"):
        out.append(f"The comparison period, {comp['span']}, has the same {payload['range']['days']} days as the range.")
    elif comp and not comp.get("available"):
        out.append("Search Console keeps 16 months of data, so there is no earlier period of this length to compare with.")
    cov = payload.get("coverage") or {}
    if cov.get("clicks_pct") is not None:
        out.append(f"The query tables cover {cov['clicks_pct']}% of clicks and {cov['impressions_pct']}% of impressions. "
                   "Google leaves very rare queries out of every query table to protect searchers; "
                   "their traffic is in the totals and in the Hidden row of the brand split.")
    cut = [name for name, t in (payload.get("tables") or {}).items() if t and t.get("cut")]
    if cut:
        out.append(f"Google returns at most {ROW_CAP:,} rows per request, and the {' and '.join(cut)} "
                   "tables reached that ceiling, so they hold the top rows only.")
    out.append(f"Landing pages and competing pages use the last {QUERY_PAGE_MAX_DAYS} days of the range "
               f"({payload['query_page_span']}). An opportunity needs at least "
               f"{payload['min_impressions']} impressions in the range.")
    return out
