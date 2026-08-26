#!/usr/bin/env python3
"""Recurring Search Console measurement — rolling windows, de-biased.

Answers "what changed since last time" for one property, and is the deterministic
half of the ``/seo-pulse`` skill: it runs no model, and every figure it writes can
be re-derived from its cache. Two runs on one cache are byte-identical.

    export GSC_SITE=sc-domain:example.com
    export GSC_CREDENTIALS=~/.config/keel-seo/service-account.json
    python -m keel_seo.gsc.pulse --days 28 --out-dir docs/seo/pulse

Windows are resolved from the last **finalised** day (Search Console lags 2-3 days),
never from today, and are written into the output so a number can always be traced
back to its span. ``dataState=final`` throughout.

What it measures, and why each one is shaped the way it is: site totals come from the
complete ``date`` dimension because the query dimension withholds a *different* share
in each window; position is reported unweighted over the matched keyword set because
the impression-weighted average improves simply by losing keywords; every CTR cohort
is gated at a minimum impression count because CTR is unreadable on a handful of
impressions. The consuming skill carries the full list of traps.

Output (``--out-dir``):
    <end>-facts.json   every measurement of this run
    history.json       one headline row per run, for the run-over-run trend
The previous run's facts file, if present, is diffed into ``facts["vs_previous_run"]``.

Host-specific input is limited to ``--content-prefixes``: the first path segments
whose children are individual articles rather than sections (``blog``, ``news``, a
glossary), so the family rollup groups them as ``/blog/*`` instead of one row per URL.
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, re, sys, time
from collections import defaultdict
from pathlib import Path

SITE = os.environ.get("GSC_SITE", "")
CRED = os.path.expanduser(os.environ.get("GSC_CREDENTIALS", "~/.config/keel-seo/service-account.json"))

# Search-operator strings ("site:", '"foo" -site:reddit.com') are scraper traffic, not
# users: they carry impressions and no clicks. Excluded from every keyword measurement.
JUNK = re.compile(r"(^|\s)-?site:")
# CTR is unreadable below this many impressions in the window: a keyword with 3
# impressions and 1 click reads as 33%. Every CTR cohort below is gated on it.
CTR_FLOOR = 50
# A keyword is "winnable" at these thresholds — ranked but below the click cliff.
GAP_POS = (3.5, 20.0)
GAP_IMPR_DAY = 3.0

short = lambda u: re.sub(r"^https?://[^/]+", "", u).split("#")[0].split("?")[0].rstrip("/") or "/"


def client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        CRED, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def pull(svc, start, end, dims, search_type="web", row_limit=100000):
    rows, sr = [], 0
    while True:
        body = {"startDate": start, "endDate": end, "dimensions": dims, "rowLimit": 25000,
                "startRow": sr, "type": search_type, "dataState": "final"}
        for attempt in range(5):
            try:
                r = svc.searchanalytics().query(siteUrl=SITE, body=body).execute(); break
            except Exception:
                if attempt == 4: raise
                time.sleep(2 * (attempt + 1))
        batch = r.get("rows", []); rows.extend(batch)
        if len(batch) < 25000 or len(rows) >= row_limit: break
        sr += 25000
    return rows


def last_final_day(svc, today: dt.date) -> str:
    """GSC finalises with a lag. Ask it rather than assuming a fixed offset."""
    rows = pull(svc, (today - dt.timedelta(days=12)).isoformat(), today.isoformat(), ["date"])
    if not rows:
        raise SystemExit("no finalised data in the last 12 days — check the property and credentials")
    return max(r["keys"][0] for r in rows)


def cached(cache: Path, name, fn):
    p = cache / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    data = fn()
    p.write_text(json.dumps(data))
    print(f"  pulled {name}: {len(data)} rows", file=sys.stderr)
    return data


def keyagg(rows, days, by="query"):
    """Aggregate page x query rows to one entity per key.

    `top` is the page holding the most impressions for that query — an attribution
    heuristic, not a Google-stated owner. `npages` is how many of our URLs Google
    showed for it, which is the cannibalisation signal.
    """
    out = defaultdict(lambda: {"i": 0, "c": 0, "pw": 0.0, "pages": defaultdict(int)})
    for r in rows:
        page, query = r["keys"]
        if JUNK.search(query): continue
        k = short(page) if by == "page" else query
        e = out[k]
        e["i"] += r["impressions"]; e["c"] += r["clicks"]
        e["pw"] += r["position"] * r["impressions"]
        e["pages"][short(page)] += r["impressions"]
    for k, e in out.items():
        e["pos"] = e["pw"] / e["i"] if e["i"] else 0
        e["ctr"] = e["c"] / e["i"] * 100 if e["i"] else 0
        e["ipd"] = e["i"] / days; e["cpd"] = e["c"] / days
        e["npages"] = len(e["pages"])
        e["top"] = max(e["pages"], key=e["pages"].get) if e["pages"] else None
    return out


def corr(xs, ys):
    n = len(xs)
    if n < 3: return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n); sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    return 0.0 if sx == 0 or sy == 0 else round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * sx * sy), 2)


def family(path: str, content_prefixes: frozenset) -> str:
    """Page family = the shape of the URL, not its first segment alone.

    Directory verdicts are wrong when one directory holds several products, so a
    family keeps two segments where the second names a section rather than one item.
    `content_prefixes` are the directories whose children are individual articles —
    they collapse to one row so 200 posts do not become 200 families.
    """
    parts = [p for p in path.split("/") if p]
    if not parts: return "/"
    if parts[0] in content_prefixes and len(parts) > 1:
        return f"/{parts[0]}/*"
    if len(parts) == 1: return f"/{parts[0]}"
    if len(parts) >= 3: return f"/{parts[0]}/{parts[1]}/*"
    return f"/{parts[0]}/*"


def fetch(cache: Path, cur, prev, hist_start, end):
    cache.mkdir(parents=True, exist_ok=True)
    names = {"daily_web": None, "daily_image": None, "pq_cur": None, "pq_prev": None,
             "page_cur": None, "page_prev": None}
    need = [n for n in names if not (cache / f"{n}.json").exists()]
    svc = client() if need else None
    d = {}
    d["daily_web"] = cached(cache, "daily_web", lambda: pull(svc, hist_start, end, ["date"]))
    d["daily_image"] = cached(cache, "daily_image", lambda: pull(svc, hist_start, end, ["date"], "image"))
    d["pq_cur"] = cached(cache, "pq_cur", lambda: pull(svc, *cur, ["page", "query"]))
    d["pq_prev"] = cached(cache, "pq_prev", lambda: pull(svc, *prev, ["page", "query"]))
    d["page_cur"] = cached(cache, "page_cur", lambda: pull(svc, *cur, ["page"]))
    d["page_prev"] = cached(cache, "page_prev", lambda: pull(svc, *prev, ["page"]))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28, help="length of each window (>=28 keeps weekday noise out)")
    ap.add_argument("--end", default=None, help="last day of the current window (default: last finalised day)")
    ap.add_argument("--out-dir", default="docs/seo/pulse")
    ap.add_argument("--cache", default=None, help="default: $GSC_CACHE_DIR (else ~/.cache/keel-seo-gsc) + /pulse-<end>-<days>d")
    ap.add_argument("--history-days", type=int, default=180, help="daily site series length")
    ap.add_argument("--content-prefixes", default="",
                    help="comma-separated first segments whose children are single articles, e.g. blog,news")
    a = ap.parse_args()
    if not SITE:
        raise SystemExit("set $GSC_SITE (e.g. sc-domain:example.com)")
    prefixes = frozenset(p.strip("/ ") for p in a.content_prefixes.split(",") if p.strip())

    end = a.end
    if end is None:
        end = last_final_day(client(), dt.date.today())
    e = dt.date.fromisoformat(end)
    n = a.days
    cur = ((e - dt.timedelta(days=n - 1)).isoformat(), e.isoformat())
    prev = ((e - dt.timedelta(days=2 * n - 1)).isoformat(), (e - dt.timedelta(days=n)).isoformat())
    hist_start = (e - dt.timedelta(days=a.history_days - 1)).isoformat()
    cache = Path(a.cache or os.path.expanduser(os.environ.get("GSC_CACHE_DIR", "~/.cache/keel-seo-gsc") + f"/pulse-{end}-{n}d"))
    raw = fetch(cache, cur, prev, hist_start, end)

    F = {"meta": {"site": SITE, "generated_for_end_date": end, "window_days": n,
                  "current": cur, "previous": prev, "history_from": hist_start,
                  "data_state": "final", "cache": str(cache),
                  "content_prefixes": sorted(prefixes)}}

    # ---- site level from the date dimension: the only dimension with no withholding
    daily = {r["keys"][0]: r for r in raw["daily_web"]}
    def win(rows_by_date, s, ee):
        sel = [v for k, v in rows_by_date.items() if s <= k <= ee]
        c = sum(v["clicks"] for v in sel); i = sum(v["impressions"] for v in sel); d = len(sel)
        return {"days": d, "clicks_day": round(c / d, 1) if d else 0,
                "impr_day": round(i / d, 1) if d else 0,
                "ctr": round(c / i * 100, 2) if i else 0,
                "clicks_total": c, "impr_total": i}
    F["site"] = {"current": win(daily, *cur), "previous": win(daily, *prev)}
    F["site"]["delta_pct"] = {
        k: (round((F["site"]["current"][k] - F["site"]["previous"][k]) / F["site"]["previous"][k] * 100, 1)
            if F["site"]["previous"][k] else None)
        for k in ("clicks_day", "impr_day", "ctr")}
    img = {r["keys"][0]: r for r in raw["daily_image"]}
    F["image_search"] = {"current": win(img, *cur), "previous": win(img, *prev)}
    F["daily_series"] = [{"date": k, "clicks": v["clicks"], "impressions": v["impressions"]}
                         for k, v in sorted(daily.items())]
    weekly = defaultdict(lambda: {"clicks": 0, "impressions": 0, "days": 0})
    for k, v in sorted(daily.items()):
        wk = (dt.date.fromisoformat(k) - dt.timedelta(days=dt.date.fromisoformat(k).weekday())).isoformat()
        weekly[wk]["clicks"] += v["clicks"]; weekly[wk]["impressions"] += v["impressions"]; weekly[wk]["days"] += 1
    F["weekly_series"] = [{"week_start": k, "days": v["days"],
                           "clicks_day": round(v["clicks"] / v["days"], 1),
                           "impr_day": round(v["impressions"] / v["days"], 1),
                           "ctr": round(v["clicks"] / v["impressions"] * 100, 2) if v["impressions"] else 0}
                          for k, v in sorted(weekly.items())]

    # ---- how much of the truth the query dimension shows (it differs by window)
    pqc = [r for r in raw["pq_cur"] if not JUNK.search(r["keys"][1])]
    pqp = [r for r in raw["pq_prev"] if not JUNK.search(r["keys"][1])]
    sc, sp = F["site"]["current"], F["site"]["previous"]
    F["withholding"] = {
        "current_clicks_visible_pct": round(sum(r["clicks"] for r in pqc) / sc["clicks_total"] * 100, 1) if sc["clicks_total"] else None,
        "previous_clicks_visible_pct": round(sum(r["clicks"] for r in pqp) / sp["clicks_total"] * 100, 1) if sp["clicks_total"] else None,
        "note": "keyword-level deltas carry this bias; site totals above come from the complete date dimension"}
    F["operator_queries_excluded"] = len({r["keys"][1] for r in raw["pq_cur"] if JUNK.search(r["keys"][1])})

    # ---- keyword cohorts
    B = keyagg(raw["pq_prev"], n); A = keyagg(raw["pq_cur"], n)
    ALL = sorted(set(B) | set(A))          # sorted: set order is not stable across processes
    def cohort(q):
        b, aa = B.get(q), A.get(q)
        if b and not aa: return "vanished"
        if aa and not b: return "new"
        d = aa["pos"] - b["pos"]
        return "improved" if d <= -1 else "demoted" if d >= 1 else "held"
    CO = {q: cohort(q) for q in ALL}
    F["keyword_cohorts"] = {}
    for k in ("vanished", "demoted", "held", "improved", "new"):
        qs = [q for q in ALL if CO[q] == k]
        F["keyword_cohorts"][k] = {
            "n": len(qs),
            "impr_day_before": round(sum(B[q]["ipd"] for q in qs if q in B), 1),
            "impr_day_after": round(sum(A[q]["ipd"] for q in qs if q in A), 1),
            "clicks_day_before": round(sum(B[q]["cpd"] for q in qs if q in B), 1),
            "clicks_day_after": round(sum(A[q]["cpd"] for q in qs if q in A), 1)}
    van = [q for q in ALL if CO[q] == "vanished"]
    zero = [q for q in van if B[q]["c"] == 0]
    F["vanished_split"] = {"total": len(van), "never_clicked": len(zero),
        "never_clicked_pct": round(len(zero) / len(van) * 100) if van else 0,
        "with_clicks": len(van) - len(zero),
        "with_clicks_clicks_day": round(sum(B[q]["cpd"] for q in van if B[q]["c"] > 0), 1),
        "median_impr_day_of_vanished": round(sorted(B[q]["ipd"] for q in van)[len(van) // 2], 2) if van else 0}

    # ---- position is only comparable on the SAME keywords, one vote each
    both = [q for q in ALL if q in A and q in B]
    F["position_matched"] = {
        "n_keywords": len(both),
        "unweighted_before": round(sum(B[q]["pos"] for q in both) / len(both), 2) if both else None,
        "unweighted_after": round(sum(A[q]["pos"] for q in both) / len(both), 2) if both else None,
        "impression_weighted_before": round(sum(B[q]["pos"] * B[q]["i"] for q in both) / sum(B[q]["i"] for q in both), 2) if both else None,
        "impression_weighted_after": round(sum(A[q]["pos"] * A[q]["i"] for q in both) / sum(A[q]["i"] for q in both), 2) if both else None,
        "note": "quote the unweighted figure across a traffic change; the weighted one improves simply by losing keywords"}

    # ---- survival by pre-period size
    F["volume_strata"] = []
    for lab, lo, hi in [("<1", 0, 1), ("1-3", 1, 3), ("3-10", 3, 10), ("10-30", 10, 30),
                        ("30-100", 30, 100), ("100+", 100, 1e9)]:
        qs = [q for q in ALL if q in B and lo <= B[q]["ipd"] < hi]
        if not qs: continue
        ib = sum(B[q]["ipd"] for q in qs); cb = sum(B[q]["cpd"] for q in qs)
        F["volume_strata"].append({"band": lab, "n": len(qs), "impr_day": round(ib, 1),
            "vanished_pct": round(len([q for q in qs if CO[q] == "vanished"]) / len(qs) * 100),
            "impr_kept_pct": round(sum(A[q]["ipd"] for q in qs if q in A) / ib * 100) if ib else 0,
            "clicks_kept_pct": round(sum(A[q]["cpd"] for q in qs if q in A) / cb * 100) if cb else 0})

    # ---- the CTR noise floor, recomputed each run rather than assumed
    F["ctr_noise"] = []
    for lab, lo, hi in [("1-9", 1, 10), ("10-49", 10, 50), ("50-199", 50, 200),
                        ("200-999", 200, 1000), ("1000+", 1000, 1e9)]:
        qs = [q for q in ALL if q in B and lo <= B[q]["i"] < hi]
        hc = [q for q in qs if B[q]["ctr"] >= 20]; thin = [q for q in hc if B[q]["c"] <= 2]
        F["ctr_noise"].append({"impressions_band": lab, "n": len(qs), "reading_ctr_20pct_plus": len(hc),
            "of_those_resting_on_2_clicks_or_fewer_pct": round(len(thin) / len(hc) * 100) if hc else 0})

    SOLID = [q for q in ALL if q in B and B[q]["i"] >= CTR_FLOOR]
    F["ctr_measurable_set"] = {"n": len(SOLID), "min_window_impressions": CTR_FLOOR}
    surv = [q for q in SOLID if q in A]
    F["correlations"] = {
        "ctr_vs_position_before": corr([B[q]["ctr"] for q in SOLID], [B[q]["pos"] for q in SOLID]),
        "survival": {"log_impressions": corr([math.log10(B[q]["i"]) for q in SOLID], [1 if q in A else 0 for q in SOLID]),
                     "ctr": corr([B[q]["ctr"] for q in SOLID], [1 if q in A else 0 for q in SOLID]),
                     "position": corr([B[q]["pos"] for q in SOLID], [1 if q in A else 0 for q in SOLID])},
        "note": "correlation, not causation — survival and CTR share impressions as a common cause"}

    # ---- CTR against our own position curve: the honest title/meta signal
    CURVE = [(1, 2), (2, 3), (3, 5), (5, 8), (8, 15), (15, 30), (30, 100)]
    exp = {}
    for lo, hi in CURVE:
        qs = [q for q in ALL if q in A and A[q]["i"] >= CTR_FLOOR and lo <= A[q]["pos"] < hi]
        i = sum(A[q]["i"] for q in qs); c = sum(A[q]["c"] for q in qs)
        exp[(lo, hi)] = round(c / i * 100, 2) if i else 0
    F["position_ctr_curve"] = [{"band": f"{lo}-{hi}", "ctr": v} for (lo, hi), v in exp.items()]
    def ec(p):
        for (lo, hi), v in exp.items():
            if lo <= p < hi: return v
        return 0.2
    NOW = [q for q in ALL if q in A and A[q]["i"] >= CTR_FLOOR]
    res = sorted(((q, A[q]["ctr"] - ec(A[q]["pos"])) for q in NOW), key=lambda x: (x[1], x[0]))
    F["ctr_residual_worst"] = [{"query": q, "page": A[q]["top"], "position": round(A[q]["pos"], 1),
        "ctr": round(A[q]["ctr"], 1), "expected_ctr": ec(A[q]["pos"]), "impr_day": round(A[q]["ipd"], 1),
        "clicks_day_if_expected": round(A[q]["ipd"] * ec(A[q]["pos"]) / 100, 2)}
        for q, r in res if A[q]["i"] >= 80 and A[q]["pos"] <= 20][:20]

    # ---- quadrants, keywords and pages, on a matched position basis
    PB = keyagg(raw["pq_prev"], n, by="page"); PA = keyagg(raw["pq_cur"], n, by="page")
    def quads(Bd, Ad, minimp):
        S = [k for k in Bd if Bd[k]["i"] >= minimp]
        if not S: return {"n": 0, "cells": {}}
        med = sorted(Bd[k]["ipd"] for k in S)[len(S) // 2]
        tests = {"hi_impr_hi_ctr": lambda x: x["ipd"] >= med and x["ctr"] >= 10,
                 "hi_impr_lo_ctr": lambda x: x["ipd"] >= med and x["ctr"] < 10,
                 "lo_impr_hi_ctr": lambda x: x["ipd"] < med and x["ctr"] >= 10,
                 "lo_impr_lo_ctr": lambda x: x["ipd"] < med and x["ctr"] < 10}
        out = {"median_impr_day": round(med, 1), "n": len(S), "cells": {}}
        for name, t in tests.items():
            ks = [k for k in S if t(Bd[k])]; sv = [k for k in ks if k in Ad]
            gone = [k for k in ks if k not in Ad]
            out["cells"][name] = {"n": len(ks), "survived": len(sv), "vanished": len(gone),
                "pos_before_matched": round(sum(Bd[k]["pos"] for k in sv) / len(sv), 1) if sv else None,
                "pos_after_matched": round(sum(Ad[k]["pos"] for k in sv) / len(sv), 1) if sv else None,
                "impr_day": round(sum(Bd[k]["ipd"] for k in ks), 1),
                "clicks_day_before": round(sum(Bd[k]["cpd"] for k in ks), 1),
                "clicks_day_after": round(sum(Ad[k]["cpd"] for k in sv), 1) if sv else 0,
                "examples": sorted(ks, key=lambda k: (-Bd[k]["ipd"], k))[:5]}
        return out
    F["quadrants_keywords"] = quads(B, A, CTR_FLOOR)
    F["quadrants_pages"] = quads(PB, PA, CTR_FLOOR)

    # ---- page inventory: the dilution ratio the 2026-08 lessons turn on
    def pagemap(rows, days):
        d = defaultdict(lambda: {"c": 0.0, "i": 0.0})
        for r in rows:                      # the same path can appear twice (http/https, trailing slash)
            k = short(r["keys"][0])
            d[k]["c"] += r["clicks"] / days; d[k]["i"] += r["impressions"] / days
        return d
    pb, pa = pagemap(raw["page_prev"], n), pagemap(raw["page_cur"], n)
    def inv(m):
        tot = sum(v["c"] for v in m.values())
        top10 = sorted((v["c"] for v in m.values()), reverse=True)[:10]
        return {"urls_with_impressions": len(m),
                "urls_with_any_click": len([u for u in m if m[u]["c"] > 0]),
                "urls_over_1_click_day": len([u for u in m if m[u]["c"] >= 1]),
                "zero_click_urls": len([u for u in m if m[u]["c"] == 0]),
                "zero_click_pct": round(len([u for u in m if m[u]["c"] == 0]) / len(m) * 100) if m else 0,
                "clicks_day": round(tot, 1),
                "top10_share_pct": round(sum(top10) / tot * 100) if tot else 0}
    F["page_inventory"] = {"current": inv(pa), "previous": inv(pb)}
    allp = sorted(set(pb) | set(pa))
    F["top_pages"] = sorted([{"url": u, "clicks_day": round(pa[u]["c"], 1),
        "clicks_day_before": round(pb[u]["c"], 1), "impr_day": round(pa[u]["i"], 1),
        "impr_day_before": round(pb[u]["i"], 1),
        "ctr": round(pa[u]["c"] / pa[u]["i"] * 100, 1) if pa[u]["i"] else 0} for u in allp
        if max(pb[u]["c"], pa[u]["c"]) >= 0.5], key=lambda x: (-x["clicks_day"], x["url"]))[:25]
    movers = sorted(({"url": u, "clicks_day": round(pa[u]["c"], 1), "clicks_day_before": round(pb[u]["c"], 1),
                      "delta": round(pa[u]["c"] - pb[u]["c"], 1),
                      "impr_day": round(pa[u]["i"], 1), "impr_day_before": round(pb[u]["i"], 1)}
                     for u in allp), key=lambda x: (x["delta"], x["url"]))
    F["page_losers"] = [m for m in movers if m["delta"] <= -0.3][:20]
    F["page_gainers"] = [m for m in reversed(movers) if m["delta"] >= 0.3][:20]

    # ---- families, so a verdict is passed on a page shape rather than one URL
    fam = defaultdict(lambda: {"urls": 0, "clicks_day": 0.0, "impr_day": 0.0,
                               "clicks_day_before": 0.0, "impr_day_before": 0.0, "zero_click_urls": 0})
    for u in allp:
        f = fam[family(u, prefixes)]
        f["urls"] += 1
        f["clicks_day"] += pa[u]["c"]; f["impr_day"] += pa[u]["i"]
        f["clicks_day_before"] += pb[u]["c"]; f["impr_day_before"] += pb[u]["i"]
        if pa[u]["c"] == 0: f["zero_click_urls"] += 1
    F["families"] = sorted(({"family": k, "urls": v["urls"], "zero_click_urls": v["zero_click_urls"],
        "clicks_day": round(v["clicks_day"], 1), "clicks_day_before": round(v["clicks_day_before"], 1),
        "impr_day": round(v["impr_day"], 1), "impr_day_before": round(v["impr_day_before"], 1),
        "clicks_per_url_day": round(v["clicks_day"] / v["urls"], 2) if v["urls"] else 0,
        "ctr": round(v["clicks_day"] / v["impr_day"] * 100, 1) if v["impr_day"] else 0}
        for k, v in fam.items()), key=lambda x: (-x["clicks_day"], x["family"]))

    # ---- cannibalisation, discovered rather than hand-listed
    pairimp = defaultdict(lambda: {"shared": 0, "impr_day": 0.0, "clicks_day": 0.0, "queries": []})
    for q in ALL:
        if q not in A: continue          # A is a defaultdict — never index it to test
        e = A[q]
        if e["npages"] < 2 or e["i"] < 10: continue
        ps = sorted(e["pages"], key=lambda p: -e["pages"][p])[:2]
        key = tuple(sorted(ps))
        d = pairimp[key]
        d["shared"] += 1; d["impr_day"] += e["ipd"]; d["clicks_day"] += e["cpd"]
        if len(d["queries"]) < 5: d["queries"].append(q)
    def pagepos(page, qs, agg):
        i = sum(agg[q]["pages"].get(page, 0) for q in qs if q in agg)
        return i
    F["cannibalisation"] = []
    for (p1, p2), d in sorted(pairimp.items(), key=lambda kv: (-kv[1]["impr_day"], kv[0]))[:15]:
        if d["shared"] < 3: continue
        qs = [q for q in ALL if q in A and p1 in A[q]["pages"] and p2 in A[q]["pages"]]
        prevqs = [q for q in ALL if q in B and p1 in B[q]["pages"] and p2 in B[q]["pages"]]
        F["cannibalisation"].append({
            "page_a": p1, "page_b": p2, "shared_queries": d["shared"],
            "shared_impr_day": round(d["impr_day"], 1), "shared_clicks_day": round(d["clicks_day"], 2),
            "a_impr_share_pct": round(pagepos(p1, qs, A) / (pagepos(p1, qs, A) + pagepos(p2, qs, A)) * 100) if qs else None,
            "a_impr_share_pct_before": round(pagepos(p1, prevqs, B) / (pagepos(p1, prevqs, B) + pagepos(p2, prevqs, B)) * 100) if prevqs else None,
            "examples": d["queries"]})

    # ---- the winnable set: ranked, below the click cliff, with real demand
    gap = sorted([q for q in ALL if q in A and A[q]["ipd"] >= GAP_IMPR_DAY and GAP_POS[0] <= A[q]["pos"] <= GAP_POS[1]],
                 key=lambda q: (-A[q]["ipd"], q))
    F["striking_distance"] = [{"query": q, "page": A[q]["top"], "impr_day": round(A[q]["ipd"], 1),
        "clicks_day": round(A[q]["cpd"], 2), "position": round(A[q]["pos"], 1),
        "our_urls": A[q]["npages"]} for q in gap[:40]]
    gp = defaultdict(lambda: {"n": 0, "impr_day": 0.0, "clicks_day": 0.0})
    for q in gap:
        g = gp[A[q]["top"]]; g["n"] += 1; g["impr_day"] += A[q]["ipd"]; g["clicks_day"] += A[q]["cpd"]
    F["striking_distance_by_page"] = sorted(({"page": k, "n": v["n"], "impr_day": round(v["impr_day"], 1),
        "clicks_day": round(v["clicks_day"], 2)} for k, v in gp.items()), key=lambda x: (-x["impr_day"], x["page"]))[:15]
    F["striking_distance_total"] = {"n": len(gap), "impr_day": round(sum(A[q]["ipd"] for q in gap), 1),
        "clicks_day_now": round(sum(A[q]["cpd"] for q in gap), 1),
        "clicks_day_at_top3_ctr": round(sum(A[q]["ipd"] * ec(2.5) / 100 for q in gap), 1),
        "note": "a ceiling, not a forecast: it assumes every one of these reaches top-3 CTR"}

    # ---- named lists, for decisions
    def row(q, src, dst=None):
        e = src[q]
        r = {"query": q, "page": e["top"], "impr_day": round(e["ipd"], 1),
             "clicks_day": round(e["cpd"], 2), "position": round(e["pos"], 1), "our_urls": e["npages"]}
        if dst and q in dst:
            r.update({"impr_day_after": round(dst[q]["ipd"], 1), "clicks_day_after": round(dst[q]["cpd"], 2),
                      "position_after": round(dst[q]["pos"], 1), "page_after": dst[q]["top"]})
        return r
    vc = sorted([q for q in van if B[q]["c"] > 0], key=lambda q: (-B[q]["cpd"], q))
    F["list_vanished_with_clicks"] = [row(q, B) for q in vc[:30]]
    byp = defaultdict(lambda: {"n": 0, "clicks_day": 0.0, "impr_day": 0.0})
    for q in vc:
        x = byp[B[q]["top"]]; x["n"] += 1; x["clicks_day"] += B[q]["cpd"]; x["impr_day"] += B[q]["ipd"]
    F["vanished_with_clicks_by_page"] = sorted(({"page": k, "n": v["n"],
        "clicks_day": round(v["clicks_day"], 2), "impr_day": round(v["impr_day"], 1)}
        for k, v in byp.items()), key=lambda x: (-x["clicks_day"], x["page"]))[:12]
    dem = sorted([q for q in ALL if CO[q] == "demoted"], key=lambda q: (-(B[q]["cpd"] - A[q]["cpd"]), q))
    F["list_demoted"] = [row(q, B, A) for q in dem[:25]]
    imp = sorted([q for q in ALL if CO[q] == "improved"], key=lambda q: (-A[q]["cpd"], q))
    F["list_improved"] = [row(q, B, A) for q in imp[:25]]
    F["list_new_queries"] = [row(q, A) for q in sorted([q for q in ALL if CO[q] == "new"],
                                                       key=lambda q: (-A[q]["ipd"], q))[:25]]

    # ---- run-over-run: what moved since the previous pulse, not just the previous window
    out_dir = Path(a.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{end}-facts.json"
    prior = sorted(p for p in out_dir.glob("*-facts.json") if p.name != out.name)
    if prior:
        old = json.loads(prior[-1].read_text())
        os_, ns = old.get("site", {}).get("current", {}), F["site"]["current"]
        F["vs_previous_run"] = {"previous_run_end": old.get("meta", {}).get("generated_for_end_date"),
            "previous_run_file": prior[-1].name,
            "clicks_day": [os_.get("clicks_day"), ns["clicks_day"]],
            "impr_day": [os_.get("impr_day"), ns["impr_day"]],
            "ctr": [os_.get("ctr"), ns["ctr"]],
            "urls_with_any_click": [old.get("page_inventory", {}).get("current", {}).get("urls_with_any_click"),
                                    F["page_inventory"]["current"]["urls_with_any_click"]],
            "zero_click_pct": [old.get("page_inventory", {}).get("current", {}).get("zero_click_pct"),
                               F["page_inventory"]["current"]["zero_click_pct"]],
            "striking_distance_n": [old.get("striking_distance_total", {}).get("n"),
                                    F["striking_distance_total"]["n"]],
            "note": "windows differ between runs — compare per-day figures only"}

    out.write_text(json.dumps(F, indent=1))
    hist_path = out_dir / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    hist = [h for h in hist if h.get("end") != end]
    hist.append({"end": end, "window_days": n, "clicks_day": sc["clicks_day"], "impr_day": sc["impr_day"],
                 "ctr": sc["ctr"], "urls_with_any_click": F["page_inventory"]["current"]["urls_with_any_click"],
                 "zero_click_pct": F["page_inventory"]["current"]["zero_click_pct"],
                 "striking_distance_n": F["striking_distance_total"]["n"],
                 "keywords_tracked": len(ALL)})
    hist.sort(key=lambda h: h["end"])
    hist_path.write_text(json.dumps(hist, indent=1))
    print(f"wrote {out} ({out.stat().st_size} bytes) and {hist_path} ({len(hist)} runs)")
    print(f"window {cur[0]}..{cur[1]} vs {prev[0]}..{prev[1]}  "
          f"clicks/day {sp['clicks_day']} -> {sc['clicks_day']} ({F['site']['delta_pct']['clicks_day']}%)")
    return F


if __name__ == "__main__":
    main()
