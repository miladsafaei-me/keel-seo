"""Live Search Console path for the keel_seo.gsc dashboard.

When ``GSC_LIVE=1`` and a service-account key is mounted, this pulls a dashboard for
ANY date range straight from the Search Console API at view time — the free-calendar
and preset windows both flow through here. Raw GSC carries no market/cluster
enrichment, so per-query metrics are joined with the host's committed enrichment map
(``query_enrichment.json`` under ``KEEL_SEO["gsc_data_dir"]``, exported from the
registry) before the shared :mod:`keel_seo.gsc.build` transform runs. Each computed
range is cached (per start/end) so a repeat load — and the user's 1-2x/day refresh
cadence — costs one pull, not one per hit.

Falls back cleanly: if live is disabled or a pull fails, the caller serves the
committed per-window snapshot instead.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from django.core.cache import cache

from .. import config
from . import build as gsc_build


def _enrich_json_path():
    return config.gsc_data_dir() / "query_enrichment.json"


DATA_LAG_DAYS = 2                 # GSC finalizes ~2 days late
CACHE_TTL = 12 * 3600             # 12h — matches the 1-2x/day whole-data refresh
# Bump when the cached payload SHAPE changes, so a deploy never serves an old-shape
# entry (Redis survives deploys; a stale hit silently drops new panels). v2 = Phase 1.
# v6 = fresh data (dataState=all) + range extends to today + empty-tail trim.
# v7 = per-day CTR/position in the series + CTR/position headline totals (Performance chart).
# v8 = trim PARTIAL (fresh) trailing days, not just empty ones; totals over kept days.
# v9 = per-directory daily series (dir_series) for the Performance chart's directory filter.
# v10 = directory tag on each ranking page (top_pages) so the pages list filters by directory.
# v11 = period-over-period "new queries" (prev-period set, not enrichment marker),
#       impression-weighted ranking-trend bands, TOP_N-capped row tables, dropped
#       unused pareto/top_queries.
CACHE_VER = "v11"
_ENRICH_FIELDS = ("market", "cluster_id", "cluster_topic", "content_type", "role")

_enrich_cache: "dict | None" = None


def _creds_path() -> str:
    return os.environ.get("GSC_CREDENTIALS", "")


def live_enabled() -> bool:
    """Live querying is on only when explicitly enabled AND the key is actually present."""
    if os.environ.get("GSC_LIVE") != "1":
        return False
    p = _creds_path()
    return bool(p) and Path(p).exists() and bool(os.environ.get("GSC_SITE"))


def data_lag_end() -> str:
    """The latest date GSC has finalized data for (today minus the lag)."""
    return (dt.date.today() - dt.timedelta(days=DATA_LAG_DAYS)).isoformat()


def _fmt_label(start: str, end: str) -> str:
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    if (s.year, s.month) == (e.year, e.month):
        return f"{s:%b} {s.day} – {e.day}, {e.year}"
    if s.year == e.year:
        return f"{s:%b} {s.day} – {e:%b} {e.day}, {e.year}"
    return f"{s:%b} {s.day}, {s.year} – {e:%b} {e.day}, {e.year}"


def _enrichment() -> dict:
    global _enrich_cache
    if _enrich_cache is None:
        try:
            _enrich_cache = json.loads(_enrich_json_path().read_text()).get("queries", {})
        except (FileNotFoundError, ValueError):
            _enrich_cache = {}
    return _enrich_cache


def _service():
    from . import connector as gsc

    service, _ = gsc._load_service()
    return service


def _pull(service, site: str, start: str, end: str, dimensions: list) -> list:
    rows: list = []
    start_row = 0
    while True:
        # dataState="all" includes GSC's not-yet-finalized "fresh" data (the last ~2-3
        # days), matching what the GSC UI serves for a recent custom range — so the
        # dashboard reaches right up to today instead of stopping at the finalized lag.
        body = {"startDate": start, "endDate": end, "dimensions": dimensions,
                "dataState": "all",
                "rowLimit": 25000, "startRow": start_row}
        resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
        page = resp.get("rows", [])
        rows.extend(page)
        if len(page) < 25000:
            break
        start_row += 25000
    return rows


def _date_axis(start: str, end: str) -> list:
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [(s + dt.timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


def _totals(q_rows: list, n_pages: int) -> dict:
    """Range totals from query rows: clicks/impressions/queries + CTR and the
    impression-weighted average position (the only correct way to average position)."""
    clicks = sum(int(r.get("clicks", 0)) for r in q_rows)
    impr = sum(int(r.get("impressions", 0)) for r in q_rows)
    wpos = sum(r.get("position", 0.0) * int(r.get("impressions", 0)) for r in q_rows)
    return {
        "clicks": clicks, "impressions": impr, "queries": len(q_rows), "pages": n_pages,
        "ctr": (clicks / impr) if impr else 0.0,
        "position": (wpos / impr) if impr else 0.0,
    }


def _date_totals(date_rows: list) -> dict:
    """TRUE range totals from the date dimension. GSC drops rare (anonymized) queries
    from the query-dimension breakdown, so summing query rows undercounts clicks by
    ~25%+; the date dimension carries every click, matching the GSC UI's headline."""
    clicks = sum(int(r.get("clicks", 0)) for r in date_rows)
    impr = sum(int(r.get("impressions", 0)) for r in date_rows)
    wpos = sum(r.get("position", 0.0) * int(r.get("impressions", 0)) for r in date_rows)
    return {"clicks": clicks, "impressions": impr,
            "ctr": (clicks / impr) if impr else 0.0,
            "position": (wpos / impr) if impr else 0.0}


def _clicks_map(rows: list) -> dict:
    return {r["keys"][0]: int(r.get("clicks", 0)) for r in rows if r.get("keys")}


def _by_date(rows: list) -> dict:
    return {r["keys"][0]: {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                           "ctr": round(r.get("ctr", 0.0) * 100, 2), "position": round(r.get("position", 0.0), 1)}
            for r in rows if r.get("keys")}


def _tail_cutoff(series: list) -> int:
    """Length to truncate the daily series to, dropping the trailing PARTIAL days.

    With dataState="all" the last day or two are fresh and incomplete, so their
    impressions sit far below a normal recent day. Compare each trailing day against the
    median impressions of the recent window (robust to the site's growth ramp) and cut
    off any tail day under 35% of it — that removes a barely-started "today" while
    keeping a genuine (even if slightly light) last complete day. Always keeps >=1 day.
    """
    recent = sorted(s["c_impr"] for s in series[-21:] if s["c_impr"] > 0)
    if not recent:
        return len(series)
    baseline = recent[len(recent) // 2]
    threshold = baseline * 0.35
    i = len(series)
    while i > 1 and series[i - 1]["c_impr"] < threshold:
        i -= 1
    return i


def _comparison(service, site: str, start: str, end: str, cur_q: list, cur_p: list) -> dict:
    """Period-over-period block: KPI deltas, query/page movers, and a daily time series
    with the previous equal-length period aligned day-for-day for overlay."""
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    prev_end = (dt.date.fromisoformat(start) - dt.timedelta(days=1))
    prev_start = prev_end - dt.timedelta(days=days - 1)
    ps, pe = prev_start.isoformat(), prev_end.isoformat()

    prev_q = _pull(service, site, ps, pe, ["query"])
    prev_p = _pull(service, site, ps, pe, ["page"])
    cur_d = _pull(service, site, start, end, ["date"])
    prev_d = _pull(service, site, ps, pe, ["date"])

    prev_t = {**_totals(prev_q, len(prev_p)), **_date_totals(prev_d)}
    movers = {
        "queries": gsc_build.movers_from(_clicks_map(cur_q), _clicks_map(prev_q)),
        "pages": gsc_build.movers_from(_clicks_map(cur_p), _clicks_map(prev_p)),
    }

    cur_axis, prev_axis = _date_axis(start, end), _date_axis(ps, pe)
    cur_bd, prev_bd = _by_date(cur_d), _by_date(prev_d)
    series = []
    for i, d in enumerate(cur_axis):
        c = cur_bd.get(d, {})
        pd = prev_bd.get(prev_axis[i], {}) if i < len(prev_axis) else {}
        series.append({"date": d, "c_clicks": c.get("clicks", 0), "c_impr": c.get("impressions", 0),
                       "c_ctr": c.get("ctr", 0.0), "c_pos": c.get("position", 0.0),
                       "p_clicks": pd.get("clicks", 0), "p_impr": pd.get("impressions", 0)})
    # dataState="all" reaches today, but the last day or two arrive only PARTIALLY (fresh
    # data still trickling in) and read far below a normal day — showing them as a cliff
    # to ~0 is misleading. Cut the series at the last COMPLETE day and treat that as the
    # range end, so the chart + headline totals stop where real data actually does.
    series = series[:_tail_cutoff(series)]
    data_end = series[-1]["date"] if series else end

    # Headline clicks/impressions/CTR/position from the date dim over the KEPT days only
    # (a partial "today" must never drag the totals down); queries/pages counts stay
    # row-based (distinct KNOWN queries / pages over the pulled range).
    kept = {s["date"] for s in series}
    cur_d_kept = [r for r in cur_d if (r.get("keys") or [None])[0] in kept]
    cur_t = {**_totals(cur_q, len(cur_p)), **_date_totals(cur_d_kept)}
    deltas = gsc_build.period_deltas(cur_t, prev_t)

    has_prev = any(s["p_clicks"] or s["p_impr"] for s in series)
    return {"kpi_deltas": deltas, "movers": movers, "true_totals": cur_t, "data_end": data_end,
            "prev_query_rows": prev_q,
            "time_series": {"days": series, "has_prev": has_prev, "prev_start": ps, "prev_end": pe}}


def _bands_live(service, site: str, end: str) -> dict:
    """Trend panel: each position band's share across three trailing 30-day windows
    ending at ``end`` — the live counterpart of the exporter's window-*.json bands."""
    e = dt.date.fromisoformat(end)
    windows = [("0-30d", 0), ("30-60d", 30), ("60-90d", 60)]
    loaded = []
    for label, back in windows:
        w_end = e - dt.timedelta(days=back)
        w_start = w_end - dt.timedelta(days=29)
        rows = _pull(service, site, w_start.isoformat(), w_end.isoformat(), ["query"])
        loaded.append((label, rows))
    bands = []
    for lo, hi, label, _blurb, _tone in gsc_build._POSITION_BANDS:
        shares = []
        for _wl, rows in loaded:
            # Impression-weighted share: a band's slice of total exposure, not a raw
            # query count — so one 10k-impression query outweighs a 1-impression one.
            total = sum(int(r.get("impressions", 0)) for r in rows)
            in_band = sum(int(r.get("impressions", 0)) for r in rows
                          if lo <= r.get("position", 0) <= hi)
            shares.append(round(in_band / total * 100, 1) if total else 0.0)
        bands.append({"label": label, "shares": shares})
    return {"windows": [w[0] for w in windows], "bands": bands}


def _dir_daily_series(dp_rows: list, axis: list, top: int = 15) -> dict:
    """Per-URL-directory daily series from a date×page pull, keyed by first path segment.

    The page dimension is not query-anonymized, so summing pages is complete. Returns
    ``{slug: {label, days:[{date, c_clicks, c_impr, c_ctr, c_pos}], totals:{...}}}`` over
    the shared ``axis`` (same dates as the "all" series so charts overlay identically),
    ordered by clicks desc and capped to ``top`` directories.
    """
    acc: dict = {}          # dir -> date -> {clicks, impr, wpos}
    tot: dict = {}          # dir -> {clicks, impr, wpos}
    for r in dp_rows:
        k = r.get("keys") or []
        if len(k) < 2:
            continue
        date, page = k[0], k[1]
        d = gsc_build._first_directory(page)
        clicks, impr = int(r.get("clicks", 0)), int(r.get("impressions", 0))
        wpos = r.get("position", 0.0) * impr
        dd = acc.setdefault(d, {}).setdefault(date, {"clicks": 0, "impr": 0, "wpos": 0.0})
        dd["clicks"] += clicks; dd["impr"] += impr; dd["wpos"] += wpos
        t = tot.setdefault(d, {"clicks": 0, "impr": 0, "wpos": 0.0})
        t["clicks"] += clicks; t["impr"] += impr; t["wpos"] += wpos

    out: dict = {}
    for d, t in sorted(tot.items(), key=lambda kv: kv[1]["clicks"], reverse=True)[:top]:
        bydate = acc.get(d, {})
        days = []
        for date in axis:
            e = bydate.get(date, {})
            impr, clicks = e.get("impr", 0), e.get("clicks", 0)
            days.append({"date": date, "c_clicks": clicks, "c_impr": impr,
                         "c_ctr": round(clicks / impr * 100, 2) if impr else 0.0,
                         "c_pos": round(e.get("wpos", 0.0) / impr, 1) if impr else 0.0})
        timpr = t["impr"]
        out[d] = {"label": "Home" if d == "(home)" else "/" + d, "days": days,
                  "totals": {"clicks": t["clicks"], "impressions": timpr,
                             "ctr": round(t["clicks"] / timpr * 100, 2) if timpr else 0.0,
                             "position": round(t["wpos"] / timpr, 1) if timpr else 0.0}}
    return out


def build_range(start: str, end: str, window: str = "custom") -> dict:
    """A full dashboard payload for [start, end], computed live and cached per range."""
    site = os.environ["GSC_SITE"]
    ckey = f"gscdash:{CACHE_VER}:{site}:{start}:{end}"
    hit = cache.get(ckey)
    if hit is not None:
        return hit

    service = _service()
    q_rows = _pull(service, site, start, end, ["query"])
    p_rows = _pull(service, site, start, end, ["page"])
    qp_rows = _pull(service, site, start, end, ["page", "query"])
    enrich = _enrichment()

    queries: dict = {}
    for r in q_rows:
        keys = r.get("keys", [])
        if not keys:
            continue
        query = keys[0]
        e = enrich.get(query, {})
        queries[query] = {
            "clicks": int(r.get("clicks", 0)),
            "impressions": int(r.get("impressions", 0)),
            "ctr": round(r.get("ctr", 0.0), 4),
            "position": round(r.get("position", 0.0), 1),
            "market": e.get("market") or "cross-market",
            "cluster_id": e.get("cluster_id"),
            "cluster_topic": e.get("cluster_topic"),
            "content_type": e.get("content_type"),
            "role": e.get("role"),
            "first_seen": e.get("first_seen") or end,   # unseen-before => new to this range
            "last_seen": end,
        }
    reg = {"site": site, "updated": end, "queries": queries}
    # The comparison pull gives us the previous equal-length period's query set, which
    # `build` uses for an honest period-over-period "new queries" definition (a query is
    # new iff it did not appear in the prior period) instead of the enrichment-map marker.
    comp = _comparison(service, site, start, end, q_rows, p_rows)
    prev_keys = {r["keys"][0] for r in comp["prev_query_rows"] if r.get("keys")}
    payload = gsc_build.build(reg, p_rows, qp_rows,
                              bands_over_time=_bands_live(service, site, end),
                              prev_query_keys=prev_keys)
    payload.update({k: comp[k] for k in ("kpi_deltas", "movers", "time_series")})
    # Correct the headline clicks/impressions to the TRUE totals (query-dim sums undercount).
    tt = comp["true_totals"]
    payload["totals"]["clicks"] = tt["clicks"]
    payload["totals"]["impressions"] = tt["impressions"]
    # CTR/position from the date dimension too, so the Performance boxes stay internally
    # consistent (ctr == clicks/impr on the corrected totals, not the query-dim subset).
    # CTR is stored as a PERCENT (× 100) to match the per-day series and the box/axis
    # formatters — _date_totals returns it as a 0..1 fraction.
    payload["totals"]["ctr"] = round(tt["ctr"] * 100, 2)
    payload["totals"]["position"] = tt["position"]

    # Per-URL-directory daily series for the Performance chart's directory filter. One
    # extra date×page pull, aggregated by first path segment over the kept date axis, so
    # the dropdown can re-scope the chart client-side with no reload.
    axis = [d["date"] for d in comp["time_series"]["days"]]
    if axis:
        dp_rows = _pull(service, site, start, end, ["date", "page"])
        payload["dir_series"] = _dir_daily_series(dp_rows, axis)

    # Label/meta reflect the true last-populated date, not the requested end (which may
    # sit a day or two ahead of GSC's freshest data).
    eff_end = comp.get("data_end") or end
    days = (dt.date.fromisoformat(eff_end) - dt.date.fromisoformat(start)).days + 1
    label = f"All time · {_fmt_label(start, eff_end)}" if window == "full" else _fmt_label(start, eff_end)
    payload.setdefault("meta", {}).update({
        "window": window, "start": start, "end": eff_end, "days": days,
        "range_label": label, "live": True,
    })
    cache.set(ckey, payload, CACHE_TTL)
    return payload
