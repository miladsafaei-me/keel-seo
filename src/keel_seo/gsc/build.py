"""Pure GSC-dashboard transforms — the single source shared by a host's offline
exporter (e.g. SignalBots' ``tools/gsc/export_dashboard.py`` / ``build_windows.py``)
and :mod:`keel_seo.gsc.live` (the runtime live path).

No file IO, no Django, no network: given a registry-shaped ``reg`` plus ``pages`` /
``query_page`` row lists (raw GSC ``keys``/metrics), ``build()`` returns the exact
dashboard payload the view renders. The trend panel's per-window bands are passed IN
(``bands_over_time``) so the caller decides where they come from — the offline tool
reads committed ``window-*.json`` files; the live path pulls three trailing windows.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# ---- Tunable thresholds (surfaced to the dashboard so every filter is legible) ----
# These are the only "magic numbers" the reports use; keeping them here (and echoing
# them into the UI subtitles via ``thresholds()``) means a reader always knows why a
# row was included or dropped.
HIGH_IMPR_MIN = 30      # a QUERY needs at least this much exposure to count as "high impressions"
LOW_CTR_MAX = 0.03      # ... and a CTR under this to count as "low click"
PAGE_HIGH_IMPR_MIN = 100  # a PAGE needs this much exposure to count in the by-page low-click list
PAGE_LOW_CTR_MAX = 0.05   # ... and a CTR under this
TOP_N = 60              # rows shipped per table to the browser (head only; full data stays server-side)
CLUSTER_MEMBER_CAP = 400  # member queries kept per cluster, for sending a cluster to the clustering queue


def thresholds() -> dict:
    """The active thresholds, as percentages where the UI shows them — so a panel can
    render "impressions ≥ 30, CTR < 3%" straight from the source of truth."""
    return {
        "high_impr_min": HIGH_IMPR_MIN,
        "low_ctr_max_pct": round(LOW_CTR_MAX * 100, 1),
        "page_high_impr_min": PAGE_HIGH_IMPR_MIN,
        "page_low_ctr_max_pct": round(PAGE_LOW_CTR_MAX * 100, 1),
        "top_n": TOP_N,
    }


def _is_noise(query: str) -> bool:
    """GSC surfaces raw operator/URL searches that aren't content targets."""
    ql = query.lower()
    return "site:" in ql or ql.startswith("http") or "tinyurl" in ql or ".com/" in ql


_POSITION_BANDS = [
    (1, 3, "1-3", "Page 1 top", "good"),
    (4, 10, "4-10", "Page 1 rest", "opportunity"),
    (11, 20, "11-20", "Page 2 — striking distance", "opportunity"),
    (21, 10_000, "21+", "Deeper", "muted"),
]


def _position_bands(rows: list) -> list:
    """Queries / impressions / clicks grouped by ranking-position band."""
    total_q = len(rows) or 1
    total_i = sum(v["impressions"] for v in rows) or 1
    total_c = sum(v["clicks"] for v in rows) or 1
    out = []
    for lo, hi, label, blurb, tone in _POSITION_BANDS:
        band = [v for v in rows if v.get("position") and lo <= v["position"] <= hi]
        qn = len(band)
        im = sum(v["impressions"] for v in band)
        cl = sum(v["clicks"] for v in band)
        out.append({
            "label": label, "blurb": blurb, "tone": tone,
            "queries": qn, "impressions": im, "clicks": cl,
            "query_pct": round(qn / total_q * 100, 1),
            "impressions_pct": round(im / total_i * 100, 1),
            "clicks_pct": round(cl / total_c * 100, 1),
        })
    return out


_BRAND_RULES = [
    ("Chinese Bot", re.compile(r"chin(e|)se|chinesebot|chinese\s?boat", re.I)),
    ("Quotex", re.compile(r"quotex|qxbroker|\bqx\b", re.I)),
    ("Pocket Option", re.compile(r"pocket\s?option|pocketoption|\bpo\s?trade\b", re.I)),
    ("Olymp Trade", re.compile(r"olymp", re.I)),
    ("IQ Option", re.compile(r"iq\s?option|iqoption", re.I)),
    ("Binomo", re.compile(r"binomo", re.I)),
    ("Deriv", re.compile(r"\bderiv\b", re.I)),
    ("Exnova", re.compile(r"exnova", re.I)),
]


def _brand(query: str) -> str:
    for name, rx in _BRAND_RULES:
        if rx.search(query):
            return name
    return "Other"


def _brand_share(rows_by_query: dict) -> dict:
    """Per-metric brand pie data: top brands + 'Other', with pct, for each of
    queries / impressions / clicks."""
    agg = {}
    for query, v in rows_by_query.items():
        b = agg.setdefault(_brand(query), {"queries": 0, "impressions": 0, "clicks": 0})
        b["queries"] += 1
        b["impressions"] += v["impressions"]
        b["clicks"] += v["clicks"]

    def _slices(metric: str) -> list:
        total = sum(b[metric] for b in agg.values()) or 1
        branded = sorted(
            ((name, b[metric]) for name, b in agg.items() if name != "Other"),
            key=lambda kv: kv[1], reverse=True,
        )
        top = [(n, v) for n, v in branded[:6] if v > 0]
        other = agg.get("Other", {}).get(metric, 0) + sum(v for _n, v in branded[6:])
        rows = [{"brand": n, "value": v, "pct": round(v / total * 100, 1)} for n, v in top]
        if other > 0:
            rows.append({"brand": "Other", "value": other, "pct": round(other / total * 100, 1)})
        return rows

    return {"queries": _slices("queries"), "impressions": _slices("impressions"), "clicks": _slices("clicks")}


_CLUSTER_STOP = {
    "the", "a", "an", "for", "to", "of", "in", "on", "at", "and", "or", "with",
    "how", "is", "are", "do", "does", "can", "what", "which", "vs", "my", "your",
    "me", "you", "it", "this", "that", "best", "top", "no",
}

_CLUSTER_QUALIFIERS = {
    "signal", "signals", "bot", "bots", "robot", "robots", "trading", "trade",
    "trader", "free", "download", "apk", "app", "ai", "pro", "max", "generator",
    "online", "live", "real", "new", "software", "tool", "tools", "account",
    "strategy", "tutorial", "guide", "review", "legit", "legitimate", "scam",
    "hack", "vip", "premium", "paid", "channel", "group", "link", "com", "auto",
}


def _stem(t: str) -> str:
    """Naive singularization so plural/singular tokens cluster together (signals->signal)."""
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _kw_tokens(kw: str) -> list:
    out = []
    for t in re.findall(r"[a-z0-9]+", kw.lower()):
        s = _stem(t)
        if len(s) > 1 and s not in _CLUSTER_STOP:
            out.append(s)
    return out


def cluster_keywords(keywords: list) -> dict:
    """Lexical (no-LLM) keyword clustering keyed on the distinctive entity token."""
    from collections import Counter

    freq: Counter = Counter()
    for kw in keywords:
        freq.update(set(_kw_tokens(kw)))

    def label(kw: str) -> str:
        toks = _kw_tokens(kw)
        if not toks:
            return "other"
        by_freq = sorted(set(toks), key=lambda t: (-freq[t], t))
        entities = [t for t in by_freq if t not in _CLUSTER_QUALIFIERS]
        anchors = (entities + [t for t in by_freq if t in _CLUSTER_QUALIFIERS])[:2]
        return " ".join(sorted(anchors)) if anchors else "other"

    return {kw: label(kw) for kw in keywords}


def _aggregate_by_cluster(rows: list, clusters: dict, metrics: list) -> list:
    """Sum a table's metrics per keyword cluster (+ keyword count + CTR)."""
    agg: dict = {}
    for r in rows:
        c = clusters.get(r.get("query", ""), "other")
        a = agg.setdefault(c, {"cluster": c, "keywords": 0, **{m: 0 for m in metrics}})
        a["keywords"] += 1
        for m in metrics:
            a[m] += r.get(m, 0)
    for a in agg.values():
        if "impressions" in a:
            a["ctr"] = round(a.get("clicks", 0) / a["impressions"] * 100, 2) if a["impressions"] else 0.0
    return sorted(agg.values(), key=lambda a: a.get(metrics[0], 0), reverse=True)


def _pages_high_impr_low_click(pages: list) -> list:
    """Which URLs surface a lot but capture few clicks (PAGE_HIGH_IMPR_MIN / PAGE_LOW_CTR_MAX)."""
    out = []
    for r in pages or []:
        keys = r.get("keys", [])
        impr = int(r.get("impressions", 0))
        ctr = r.get("ctr", 0.0)
        if keys and impr >= PAGE_HIGH_IMPR_MIN and ctr < PAGE_LOW_CTR_MAX:
            out.append({
                "page": keys[0], "impressions": impr, "clicks": int(r.get("clicks", 0)),
                "ctr": round(ctr * 100, 2), "position": round(r.get("position", 0.0), 1),
            })
    out.sort(key=lambda r: r["impressions"], reverse=True)
    return out


def _first_directory(page: str) -> str:
    """First path segment after the root domain, e.g. .../tools/x -> 'tools'."""
    path = urlsplit(page).path.strip("/")
    return path.split("/")[0] if path else "(home)"


def _directory_share(query_page: list) -> list:
    """Keyword / click / impression share by first URL directory (top 9 + Other)."""
    agg: dict = {}
    seen: dict = {}
    for r in query_page or []:
        keys = r.get("keys", [])
        if len(keys) < 2:
            continue
        page, query = keys[0], keys[1]
        d = _first_directory(page)
        a = agg.setdefault(d, {"clicks": 0, "impressions": 0})
        a["clicks"] += int(r.get("clicks", 0))
        a["impressions"] += int(r.get("impressions", 0))
        seen.setdefault(d, set()).add(query)
    rows = [{"directory": d, "queries": len(seen[d]), "clicks": a["clicks"], "impressions": a["impressions"]}
            for d, a in agg.items()]
    rows.sort(key=lambda r: r["clicks"], reverse=True)
    top, tail = rows[:9], rows[9:]
    if tail:
        top.append({"directory": "Other",
                    "queries": sum(r["queries"] for r in tail),
                    "clicks": sum(r["clicks"] for r in tail),
                    "impressions": sum(r["impressions"] for r in tail)})
    tq = sum(r["queries"] for r in top) or 1
    tc = sum(r["clicks"] for r in top) or 1
    ti = sum(r["impressions"] for r in top) or 1
    for r in top:
        r["query_pct"] = round(r["queries"] / tq * 100, 1)
        r["clicks_pct"] = round(r["clicks"] / tc * 100, 1)
        r["impressions_pct"] = round(r["impressions"] / ti * 100, 1)
    return top


def _primary_page_by_query(query_page: list) -> dict:
    """query -> (page, impressions, clicks, position) of its highest-impression page."""
    best: dict = {}
    for r in query_page or []:
        keys = r.get("keys", [])
        if len(keys) < 2:
            continue
        page, query = keys[0], keys[1]
        impr = int(r.get("impressions", 0))
        cur = best.get(query)
        if cur is None or impr > cur["impressions"]:
            best[query] = {
                "page": page, "impressions": impr,
                "clicks": int(r.get("clicks", 0)), "position": round(r.get("position", 0.0), 1),
            }
    return best


def _pct_delta(cur: float, prev: float) -> "float | None":
    """Percent change cur-vs-prev. None means 'no baseline' (prev is zero) — the
    caller renders that as 'new' rather than a fake +100%."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def period_deltas(cur: dict, prev: dict) -> dict:
    """Period-over-period deltas for the KPI tiles. ``cur``/``prev`` carry
    clicks/impressions/queries/pages/ctr/position (ctr as a fraction, position as the
    impression-weighted average). Position improves when it goes DOWN, so its
    ``improved`` flag inverts the sign."""
    out = {}
    for k in ("clicks", "impressions", "queries", "pages"):
        c, p = cur.get(k, 0), prev.get(k, 0)
        pct = _pct_delta(c, p)
        out[k] = {"cur": c, "prev": p, "pct": pct,
                  "abs_pct": abs(pct) if pct is not None else None, "up": c >= p}
    c_ctr, p_ctr = cur.get("ctr", 0.0), prev.get("ctr", 0.0)
    out["ctr"] = {"cur": round(c_ctr * 100, 2), "prev": round(p_ctr * 100, 2),
                  "pp": round((c_ctr - p_ctr) * 100, 2), "up": c_ctr >= p_ctr}
    c_pos, p_pos = cur.get("position", 0.0), prev.get("position", 0.0)
    out["position"] = {"cur": round(c_pos, 1), "prev": round(p_pos, 1),
                       "delta": round(c_pos - p_pos, 1), "improved": bool(p_pos) and c_pos <= p_pos}
    return out


def movers_from(cur_map: dict, prev_map: dict, top: int = 10, min_delta: int = 1) -> dict:
    """Biggest gainers / losers between two periods. ``*_map`` = {key: clicks}."""
    keys = set(cur_map) | set(prev_map)
    rows = []
    for k in keys:
        c, p = cur_map.get(k, 0), prev_map.get(k, 0)
        d = c - p
        if abs(d) >= min_delta:
            rows.append({"key": k, "cur": c, "prev": p, "delta": d, "pct": _pct_delta(c, p)})
    gainers = sorted((r for r in rows if r["delta"] > 0), key=lambda r: r["delta"], reverse=True)[:top]
    losers = sorted((r for r in rows if r["delta"] < 0), key=lambda r: r["delta"])[:top]
    return {"gainers": gainers, "losers": losers}


def cannibalization(query_page: list, min_impr: int = 20, top: int = 25) -> list:
    """Queries that more than one of our URLs competes for — Google splitting signals
    across pages. Each row: the query + its competing pages (impressions >= min_impr),
    ranked by page count then impressions. The action is consolidate / canonicalize."""
    byq: dict = {}
    for r in query_page or []:
        keys = r.get("keys", [])
        if len(keys) < 2:
            continue
        page, query = keys[0], keys[1]
        byq.setdefault(query, []).append({
            "page": page, "clicks": int(r.get("clicks", 0)),
            "impressions": int(r.get("impressions", 0)), "position": round(r.get("position", 0.0), 1),
        })
    out = []
    for query, pgs in byq.items():
        competing = [p for p in pgs if p["impressions"] >= min_impr]
        if len(competing) >= 2 and not _is_noise(query):
            competing.sort(key=lambda p: p["impressions"], reverse=True)
            out.append({
                "query": query, "pages": competing, "n_pages": len(competing),
                "impressions": sum(p["impressions"] for p in competing),
                "clicks": sum(p["clicks"] for p in competing),
            })
    out.sort(key=lambda r: (r["n_pages"], r["impressions"]), reverse=True)
    return out[:top]


def _pos_bucket(pos: float) -> "tuple | None":
    if not pos:
        return None
    if pos <= 10:
        n = max(1, int(round(pos)))
        return (n, str(n))
    if pos <= 20:
        return (15, "11-20")
    return (25, "21+")


def ctr_curve(q_values: list, min_impr: int = 30, top: int = 20) -> dict:
    """The site's actual CTR-by-position curve (impression-weighted), plus the queries
    that rank on page 1 but earn far fewer clicks than their position predicts —
    title/meta/SERP-feature opportunities the raw low-CTR list can't rank by expectation."""
    from collections import defaultdict

    agg = defaultdict(lambda: {"impr": 0, "clicks": 0})
    for v in q_values:
        b = _pos_bucket(v.get("position", 0))
        if not b:
            continue
        agg[b[0]]["impr"] += v.get("impressions", 0)
        agg[b[0]]["clicks"] += v.get("clicks", 0)
    order = list(range(1, 11)) + [15, 25]
    labels = {**{n: str(n) for n in range(1, 11)}, 15: "11-20", 25: "21+"}
    curve = []
    expected = {}
    for k in order:
        a = agg.get(k)
        if not a or not a["impr"]:
            continue
        ctr = a["clicks"] / a["impr"]
        expected[k] = ctr
        curve.append({"label": labels[k], "ctr_pct": round(ctr * 100, 2), "impressions": a["impr"]})

    under = []
    for v in q_values:
        pos = v.get("position", 0)
        impr = v.get("impressions", 0)
        b = _pos_bucket(pos)
        if not b or pos > 10 or impr < min_impr or _is_noise(v.get("_query", "")):
            continue
        exp = expected.get(b[0], 0)
        act = v.get("ctr", 0.0)
        if exp and act < exp * 0.6:
            under.append({
                "query": v.get("_query", ""), "position": round(pos, 1),
                "impressions": impr, "clicks": v.get("clicks", 0),
                "ctr": round(act * 100, 2), "expected_ctr": round(exp * 100, 2),
                "gap": round((exp - act) * impr, 0),  # clicks left on the table
                "market": v.get("market", ""),
            })
    under.sort(key=lambda r: r["gap"], reverse=True)
    return {"curve": curve, "underperformers": under[:top]}


def build(reg: dict, pages: list, query_page: list, bands_over_time: "dict | None" = None,
          prev_query_keys: "set | None" = None) -> dict:
    """The full dashboard payload. ``bands_over_time`` (the trend panel) is supplied by
    the caller; when omitted the trend renders empty rather than reading any files.

    ``prev_query_keys`` is the set of queries seen in the immediately-preceding
    equal-length period. When given, "new" queries are those absent from it — a real
    period-over-period definition that never inflates as the enrichment map ages. When
    omitted (the offline / full-registry path), "new" falls back to the registry's
    ``first_seen == latest`` marker.
    """
    q = reg["queries"]
    latest = reg.get("updated", "")
    rows = list(q.values())
    total_clicks = sum(v["clicks"] for v in rows)
    total_impr = sum(v["impressions"] for v in rows)

    position_bands = _position_bands(rows)
    bands_over_time = bands_over_time if bands_over_time is not None else {"windows": [], "bands": []}
    brand_share = _brand_share(q)
    directory_share = _directory_share(query_page)

    hilc = [
        {"query": k, "impressions": v["impressions"], "clicks": v["clicks"],
         "ctr": round(v["ctr"] * 100, 2), "position": v["position"], "market": v.get("market", "")}
        for k, v in q.items()
        if v["impressions"] >= HIGH_IMPR_MIN and v["ctr"] < LOW_CTR_MAX and not _is_noise(k)
    ]
    hilc.sort(key=lambda r: r["impressions"], reverse=True)

    # "New" = demand not present in the previous equal-length period (the honest
    # period-over-period definition). Only when no previous-period set is supplied
    # (offline / full-registry path) do we fall back to the registry first_seen marker.
    def _is_new(key: str) -> bool:
        if prev_query_keys is not None:
            return key not in prev_query_keys
        return q[key].get("first_seen") == latest

    new_rows = [(k, v) for k, v in q.items()
                if _is_new(k) and v["impressions"] > 0 and not _is_noise(k)]
    new_top = sorted(new_rows, key=lambda kv: kv[1]["impressions"], reverse=True)
    new_queries = [
        {"query": k, "impressions": v["impressions"], "clicks": v["clicks"],
         "position": v["position"], "market": v.get("market", "")}
        for k, v in new_top
    ]

    by_market: dict = {}
    for v in rows:
        m = v.get("market", "cross-market")
        b = by_market.setdefault(m, {"queries": 0, "clicks": 0, "impressions": 0})
        b["queries"] += 1
        b["clicks"] += v["clicks"]
        b["impressions"] += v["impressions"]
    by_market_list = sorted(
        [{"market": m, **b} for m, b in by_market.items()],
        key=lambda r: r["clicks"], reverse=True,
    )

    top_pages = []
    for r in sorted(pages or [], key=lambda r: r.get("clicks", 0), reverse=True):
        keys = r.get("keys", [])
        page = keys[0] if keys else ""
        top_pages.append({
            "page": page, "directory": _first_directory(page),
            "clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
            "ctr": round(r.get("ctr", 0.0) * 100, 2), "position": round(r.get("position", 0.0), 1),
        })

    primary = _primary_page_by_query(query_page)
    dedicated = []
    for query, info in primary.items():
        qrec = q.get(query, {})
        if info["impressions"] < HIGH_IMPR_MIN or _is_noise(query):
            continue
        if info["position"] > 10:
            dedicated.append({
                "query": query, "impressions": info["impressions"], "clicks": info["clicks"],
                "position": info["position"], "page": info["page"],
                "reason": "below-page-1",
                "market": qrec.get("market", ""),
            })
    dedicated.sort(key=lambda r: r["impressions"], reverse=True)

    cl_hilc = cluster_keywords([r["query"] for r in hilc])
    for r in hilc:
        r["cluster"] = cl_hilc.get(r["query"], "other")
    hilc_by_cluster = _aggregate_by_cluster(hilc, cl_hilc, ["impressions", "clicks"])

    cl_new = cluster_keywords([r["query"] for r in new_queries])
    for r in new_queries:
        r["cluster"] = cl_new.get(r["query"], "other")
    new_by_cluster = _aggregate_by_cluster(new_queries, cl_new, ["impressions", "clicks"])

    cl_ded = cluster_keywords([r["query"] for r in dedicated])
    for r in dedicated:
        r["cluster"] = cl_ded.get(r["query"], "other")
    dedicated_by_cluster = _aggregate_by_cluster(dedicated, cl_ded, ["impressions", "clicks"])
    # Carry each cluster's member queries, not just its counts. Sending a whole cluster
    # to the clustering queue needs the keywords themselves, and the detail table above
    # is TOP_N-capped — so without this the only list we could recover is the visible
    # head of one table, which is not the cluster. Capped per cluster so one broad
    # cluster cannot balloon the payload.
    _members: dict = {}
    for r in dedicated:
        _members.setdefault(r["cluster"], []).append({
            "keyword": r["query"], "impressions": r["impressions"],
            "clicks": r["clicks"], "position": r["position"],
        })
    for row in dedicated_by_cluster:
        members = sorted(
            _members.get(row["cluster"], []),
            key=lambda m: m["impressions"], reverse=True,
        )
        row["queries"] = members[:CLUSTER_MEMBER_CAP]

    hilc_by_page = _pages_high_impr_low_click(pages)

    cannibal = cannibalization(query_page)
    ctr = ctr_curve([{**v, "_query": k} for k, v in q.items()])

    # Cluster aggregations above run over the FULL lists (so per-cluster sums are
    # complete); only the per-row tables shipped to the browser are capped to TOP_N —
    # the dashboard shows the head, the *_count fields carry the true totals.
    return {
        "meta": {
            "property": reg.get("site", ""),
            "updated": latest,
            "generated_from": "registry.json + pages.json + query-page.json",
        },
        "totals": {
            "queries": len(rows),
            "clicks": total_clicks,
            "impressions": total_impr,
            "pages": len(pages or []),
            "new_queries": len(new_rows),
        },
        "position_bands": position_bands,
        "position_bands_over_time": bands_over_time,
        "brand_share": brand_share,
        "directory_share": directory_share,
        "high_impressions_low_clicks": hilc[:TOP_N],
        "hilc_by_cluster": hilc_by_cluster[:TOP_N],
        "hilc_by_page": hilc_by_page[:TOP_N],
        "new_by_cluster": new_by_cluster[:TOP_N],
        "dedicated_by_cluster": dedicated_by_cluster[:TOP_N],
        "high_impressions_low_clicks_count": len(hilc),
        "new_queries": new_queries[:TOP_N],
        "by_market": by_market_list,
        "top_pages": top_pages[:TOP_N],
        "dedicated_content_candidates": dedicated[:TOP_N],
        "dedicated_content_candidates_count": len(dedicated),
        "cannibalization": cannibal,
        "cannibalization_count": len(cannibal),
        "ctr_curve": ctr["curve"],
        "ctr_underperformers": ctr["underperformers"],
    }
