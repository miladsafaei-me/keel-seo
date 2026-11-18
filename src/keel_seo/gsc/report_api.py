"""Fetches what the Search Console report needs and assembles it, for any property.

One entry point, :func:`build_report`, takes a property (its site, the service-account
key that reads it and its brand list) and the reader's choices (range, comparison,
search type, filters). It sends the Search Analytics requests side by side, each
capped at one page of Google's row ceiling so a very large property loads in seconds,
then hands the rows to :mod:`keel_seo.gsc.report`. The result is cached per property,
choice set and last complete day, so a repeat look costs nothing until Google adds a
day.

Requests go through ``google-auth``'s ``AuthorizedSession`` (the ``[gsc]`` extra),
one session per worker thread, with the same retry rule as the rest of the client:
429 and 5xx answers are retried with backoff.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import report as R

logger = logging.getLogger(__name__)

API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 4
WORKERS = 6
# Bump when the payload shape changes, so a deploy never serves an old-shape entry.
CACHE_VER = "r1"
CACHE_TTL = 6 * 3600
FRESHNESS_TTL = 3600

_local = threading.local()


class ReportError(RuntimeError):
    """Google refused or failed a request; the message is safe to show staff."""


def _session(key_path: str):
    """One AuthorizedSession per thread and key."""
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    if key_path not in sessions:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(key_path, scopes=[SCOPE])
        sessions[key_path] = AuthorizedSession(creds)
    return sessions[key_path]


def _call(key_path: str, method: str, url: str, body: "dict | None" = None) -> dict:
    last = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = _session(key_path).request(method, url, json=body, timeout=150)
        except Exception as exc:  # noqa: BLE001: a dropped connection is retried like a 503
            last = str(exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            last = f"{resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in RETRY_STATUSES:
                break
        if attempt < MAX_RETRIES:
            time.sleep(min(20, 2 ** attempt) + random.random())
    raise ReportError(f"Search Console request failed ({last})")


def _base(site: str) -> str:
    return API.format(site=quote(site, safe=""))


def query(prop: dict, start: dt.date, end: dt.date, dimensions: list, *,
          search_type: str = "web", filters: "list | None" = None, limit: int = R.ROW_CAP) -> dict:
    """One Search Analytics request: ``{rows, metadata}``. One page of rows only, at
    most Google's ceiling; :mod:`keel_seo.gsc.report` marks a table that hit it."""
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
            "type": search_type, "dataState": "all", "rowLimit": limit}
    if filters:
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
    data = _call(prop["credentials"], "POST", _base(prop["site"]) + "/searchAnalytics/query", body)
    return {"rows": data.get("rows", []), "metadata": data.get("metadata") or {}}


def _cache():
    try:
        from django.core.cache import cache

        return cache
    except Exception:  # noqa: BLE001: outside Django there is simply no cache
        return None


def last_complete_day(prop: dict, today: "dt.date | None" = None) -> dict:
    """The last day Google has finished, and the days after it it is still filling.
    Read from the API's ``firstIncompleteDate``; cached for an hour."""
    today = today or dt.date.today()
    cache = _cache()
    ckey = f"gscreport:fresh:{prop['site']}:{_generation(prop['site'])}:{today}"
    hit = cache.get(ckey) if cache else None
    if hit:
        return {"last": dt.date.fromisoformat(hit["last"]), "partial": hit["partial"]}
    got = query(prop, today - dt.timedelta(days=10), today, ["date"], limit=20)
    dates = sorted(r["keys"][0] for r in got["rows"] if r.get("keys"))
    first_incomplete = R.parse_date(got["metadata"].get("firstIncompleteDate"))
    if first_incomplete:
        last = first_incomplete - dt.timedelta(days=1)
    elif dates:
        last = dt.date.fromisoformat(dates[-1])
    else:
        last = today - dt.timedelta(days=3)
    partial = [d for d in dates if d > last.isoformat()]
    if cache:
        cache.set(ckey, {"last": last.isoformat(), "partial": partial}, FRESHNESS_TTL)
    return {"last": last, "partial": partial}


def api_filters(filters: dict) -> list:
    """The reader's filters as Search Analytics filter objects."""
    out = []
    for dim, f in (filters or {}).items():
        if dim in R.FILTER_DIMENSIONS and f.get("value"):
            out.append({"dimension": dim, "operator": f.get("op", "contains"), "expression": f["value"]})
    return out


def _history(prop: dict, today: dt.date, last: dt.date, search_type: str, flt: list) -> list:
    return query(prop, R.earliest_date(today), last, ["date"], search_type=search_type, filters=flt)["rows"]


def history(prop: dict, *, today: "dt.date | None" = None) -> dict:
    """Sixteen months of daily totals plus the breaks found in them: what the
    overview cards draw and warn about. Cached like a report."""
    today = today or dt.date.today()
    fresh = last_complete_day(prop, today)
    last = fresh["last"]
    cache = _cache()
    ckey = f"gscreport:{CACHE_VER}:history:{prop['site']}:{_generation(prop['site'])}:{last}"
    hit = cache.get(ckey) if cache else None
    if hit is not None:
        return hit
    rows = _history(prop, today, last, "web", [])
    out = {"rows": rows, "events": R.events(rows, last), "last": last.isoformat()}
    if cache:
        cache.set(ckey, out, CACHE_TTL)
    return out


def summary(prop: dict, *, today: "dt.date | None" = None) -> dict:
    """The overview card of one property: the last 28 complete days against the 28
    before, the daily line of both, and its recent breaks. The same periods and the
    same rules as the report's default view, so the two never disagree."""
    today = today or dt.date.today()
    h = history(prop, today=today)
    last = dt.date.fromisoformat(h["last"])
    current = R.resolve_range("28d", last, today)
    compare = R.resolve_compare("previous", current, today)
    cur_rows = R.in_period(h["rows"], current["start"], current["end"])
    prev_rows = R.in_period(h["rows"], compare["start"], compare["end"]) if compare.get("available") else []
    cur, prev = R.totals_of(cur_rows), R.totals_of(prev_rows) if compare.get("available") else None
    recent = (last - dt.timedelta(days=120)).isoformat()
    return {
        "start": current["start"], "end": current["end"], "days": current["days"],
        "totals": cur, "changes": R.changes(cur, prev),
        "series": R.daily_series(cur_rows, prev_rows, current, compare),
        "events": [e for e in h["events"] if e["date"] >= recent],
    }


def build_report(prop: dict, *, range_key: str = R.DEFAULT_RANGE, start: "str | None" = None,
                 end: "str | None" = None, compare: str = R.DEFAULT_COMPARE,
                 search_type: str = "web", filters: "dict | None" = None,
                 today: "dt.date | None" = None) -> dict:
    """The whole report payload for one property and one set of choices."""
    today = today or dt.date.today()
    search_type = search_type if search_type in R.SEARCH_TYPE_LABELS else "web"
    filters = {k: v for k, v in (filters or {}).items() if k in R.FILTER_DIMENSIONS and v.get("value")}
    fresh = last_complete_day(prop, today)
    last = fresh["last"]
    current = R.resolve_range(range_key, last, today, start, end)
    comp = R.resolve_compare(compare, current, today)
    with_prev = bool(comp and comp.get("available"))

    flt_key = "|".join(f"{k}:{v.get('op')}:{v['value']}" for k, v in sorted(filters.items()))
    cache = _cache()
    ckey = (f"gscreport:{CACHE_VER}:{prop['site']}:{_generation(prop['site'])}:{current['start']}:{current['end']}:"
            f"{comp['mode'] if comp else 'none'}:{search_type}:{flt_key}:{last}")
    if len(ckey) > 230:
        import hashlib

        ckey = "gscreport:" + hashlib.sha256(ckey.encode()).hexdigest()
    hit = cache.get(ckey) if cache else None
    if hit is not None:
        return hit

    flt = api_filters(filters)
    s, e = current["start"], current["end"]
    qp_start = max(s, e - dt.timedelta(days=R.QUERY_PAGE_MAX_DAYS - 1))
    # Search appearance cannot be combined with another dimension or filtered by one.
    jobs = {
        "date": (s, e, ["date"]), "query": (s, e, ["query"]), "page": (s, e, ["page"]),
        "country": (s, e, ["country"]), "device": (s, e, ["device"]),
        "query_page": (qp_start, e, ["query", "page"]),
    }
    if not flt:
        jobs["appearance"] = (s, e, ["searchAppearance"])
    if with_prev:
        ps, pe = comp["start"], comp["end"]
        for dim in ("date", "query", "page", "country", "device"):
            jobs["prev_" + dim] = (ps, pe, [dim])
        if not flt:
            jobs["prev_appearance"] = (ps, pe, ["searchAppearance"])

    def run(item):
        name, (a, b, dims) = item
        return name, query(prop, a, b, dims, search_type=search_type, filters=flt)["rows"]

    started = time.monotonic()
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(run, item) for item in jobs.items()]
        futures.append(pool.submit(lambda: ("history", _history(prop, today, last, search_type, flt))))
        futures.append(pool.submit(_sitemaps, prop))
        got = dict(f.result() for f in futures)
    took = round(time.monotonic() - started, 1)

    prev = (lambda name: got.get("prev_" + name)) if with_prev else (lambda name: None)
    totals = R.totals_of(got["date"])
    prev_totals = R.totals_of(got["prev_date"]) if with_prev else None
    series = R.daily_series(got["date"], got.get("prev_date") or [], current, comp)
    curve = R.ctr_curve(got["query"])
    pages = R.landing_pages(got["query_page"])
    cov = R.coverage(got["query"], totals)
    brand = R.brand_split(got["query"], totals, R.brand_rules(prop.get("brands")))
    prev_brand = (R.brand_split(got["prev_query"], prev_totals, R.brand_rules(prop.get("brands")))
                  if with_prev else None)
    if brand and prev_brand:
        prev_by_key = {r["key"]: r for r in prev_brand["rows"]}
        for row in brand["rows"]:
            row["prev"] = prev_by_key.get(row["key"])

    tables = {
        "query": R.table(got["query"], prev("query")),
        "page": R.table(got["page"], prev("page")),
        "country": R.table(got["country"], prev("country")),
        "device": R.table(got["device"], prev("device")),
        "appearance": R.table(got.get("appearance") or [], prev("appearance")) if not flt else None,
        "date": R.date_table(series),
    }
    _label_rows(tables)
    nl = R.new_and_lost(got["query"], prev("query"))
    history_rows = got["history"]

    payload = {
        "site": prop["site"],
        "range": {**current, "start": current["start"].isoformat(), "end": current["end"].isoformat(),
                  "span": R.fmt_span(current["start"], current["end"])},
        "compare": ({**comp, "start": comp["start"].isoformat(), "end": comp["end"].isoformat(),
                     "span": R.fmt_span(comp["start"], comp["end"])} if with_prev else comp),
        "search_type": search_type,
        "filters": filters,
        "last_complete": last.isoformat(),
        "last_complete_label": R._day(last.isoformat()),
        "partial_days": fresh["partial"],
        "totals": totals,
        "prev_totals": prev_totals,
        "changes": R.changes(totals, prev_totals),
        "series": series,
        "tables": tables,
        "coverage": cov,
        "brand": brand,
        "bands": R.position_bands(got["query"], prev("query")),
        "ctr_curve": curve,
        "opportunities": {
            "striking": R.striking_distance(got["query"], current["days"], pages),
            "low_ctr": R.low_ctr(got["query"], curve, current["days"], pages),
            "cannibal": R.cannibalization(got["query_page"]),
            "new": nl["new"],
            "lost": nl["lost"],
        },
        "query_page_span": R.fmt_span(qp_start, e),
        "min_impressions": R.min_impressions(current["days"]),
        "movers": {"query": R.movers(got["query"], prev("query")), "page": R.movers(got["page"], prev("page"))},
        "sections": R.sections(got["page"], prev("page")),
        "history": R.weekly(history_rows),
        "events": R.events(history_rows, last),
        "sitemaps": got["sitemaps"],
        "fetch_seconds": took,
        "built_at": dt.datetime.now().isoformat(timespec="minutes"),
    }
    if cache:
        cache.set(ckey, payload, CACHE_TTL)
    return payload


def _label_rows(tables: dict) -> None:
    """Readable names for coded keys, as Search Console shows them."""
    from .countries import name

    for row in tables["country"]["rows"]:
        row["label"] = name(row["key"])
    for row in tables["device"]["rows"]:
        row["label"] = row["key"].capitalize()
    for row in (tables.get("appearance") or {}).get("rows", []):
        row["label"] = row["key"].replace("_", " ").capitalize()


def _sitemaps(prop: dict):
    try:
        data = _call(prop["credentials"], "GET", _base(prop["site"]) + "/sitemaps")
        return "sitemaps", R.sitemaps(data.get("sitemap", []))
    except ReportError as exc:
        logger.warning("gsc report: sitemaps for %s failed: %s", prop["site"], exc)
        return "sitemaps", None


def _generation(site: str) -> int:
    """A per-property counter inside every cache key: bumping it retires them all."""
    cache = _cache()
    return int(cache.get(f"gscreport:gen:{site}", 0)) if cache else 0


def forget(prop: dict) -> None:
    """Retire every cached answer for the property, so the next look asks Google."""
    cache = _cache()
    if cache:
        cache.set(f"gscreport:gen:{prop['site']}", _generation(prop["site"]) + 1, None)
