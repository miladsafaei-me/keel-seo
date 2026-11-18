"""Pull Search Console data for the audit.

Six spans are read so a page is judged on more than one window: the last 28
days against the 28 before, the last 91 days against the 91 before and against
the same 91 days a year earlier, and the whole 16 months Search Console keeps.
Every span is a multiple of 7 so no window counts a weekday more often.
"""
from __future__ import annotations

import datetime as dt
import os

from .parse import normalize_url
from .store import Workspace, set_meta

HISTORY_DAYS = 483


def _spans(last: dt.date) -> dict:
    def back(days_from: int, days_to: int) -> tuple[str, str]:
        return ((last - dt.timedelta(days=days_from)).isoformat(),
                (last - dt.timedelta(days=days_to)).isoformat())
    return {
        "28d": back(27, 0), "prev28d": back(55, 28),
        "3m": back(90, 0), "prev3m": back(181, 91), "yoy3m": back(90 + 364, 364),
        "16m": back(HISTORY_DAYS - 1, 0),
    }


def run(workspace: Workspace, *, max_query_rows: int = 250000) -> dict:
    project = workspace.project
    if project.get("gsc_credentials"):
        os.environ["GSC_CREDENTIALS"] = os.path.expanduser(project["gsc_credentials"])
    from keel_seo.gsc import analytics

    site = project["gsc_property"]
    last = dt.date.today() - dt.timedelta(days=3)
    spans = _spans(last)
    conn = workspace.db()
    for table in ("gsc_page", "gsc_query_page", "gsc_month", "gsc_dim", "gsc_day"):
        conn.execute(f"DELETE FROM {table}")

    counts: dict = {}
    for span, (start, end) in spans.items():
        rows = analytics.fetch_all(site, start_date=start, end_date=end, dimensions=("page",))
        merged: dict = {}
        for row in rows:
            url = normalize_url(row["keys"][0])
            clicks, impressions, weighted = merged.get(url, (0, 0, 0))
            merged[url] = (clicks + row["clicks"], impressions + row["impressions"],
                           weighted + row["position"] * row["impressions"])
        conn.executemany(
            "INSERT OR REPLACE INTO gsc_page(url, span, clicks, impressions, position) VALUES (?,?,?,?,?)",
            [(url, span, c, i, w / i if i else 0) for url, (c, i, w) in merged.items()])
        counts[f"pages_{span}"] = len(merged)

    for span in ("3m", "16m"):
        start, end = spans[span]
        rows = analytics.fetch_all(site, start_date=start, end_date=end,
                                   dimensions=("query", "page"), max_rows=max_query_rows)
        conn.executemany(
            "INSERT INTO gsc_query_page(query, url, span, clicks, impressions, position) VALUES (?,?,?,?,?,?)",
            [(row["keys"][0], normalize_url(row["keys"][1]), span, row["clicks"], row["impressions"],
              row["position"]) for row in rows])
        counts[f"query_page_{span}"] = len(rows)

    month = last.replace(day=1)
    first = last - dt.timedelta(days=HISTORY_DAYS - 1)
    while month >= first.replace(day=1):
        month_end = min((month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1), last)
        month_start = max(month, first)
        rows = analytics.fetch_all(site, start_date=month_start.isoformat(), end_date=month_end.isoformat(),
                                   dimensions=("page",))
        merged = {}
        for row in rows:
            url = normalize_url(row["keys"][0])
            clicks, impressions = merged.get(url, (0, 0))
            merged[url] = (clicks + row["clicks"], impressions + row["impressions"])
        conn.executemany("INSERT OR REPLACE INTO gsc_month(month, url, clicks, impressions) VALUES (?,?,?,?)",
                         [(f"{month:%Y-%m}", url, c, i) for url, (c, i) in merged.items()])
        month = (month - dt.timedelta(days=1)).replace(day=1)

    start, end = spans["16m"]
    days = analytics.fetch_all(site, start_date=start, end_date=end, dimensions=("date",))
    conn.executemany("INSERT OR REPLACE INTO gsc_day(day, clicks, impressions, position) VALUES (?,?,?,?)",
                     [(row["keys"][0], row["clicks"], row["impressions"], row["position"]) for row in days])
    counts["days"] = len(days)

    for dim in ("country", "device", "searchAppearance", "query"):
        for span in ("3m", "prev3m"):
            start, end = spans[span]
            rows = analytics.fetch_all(site, start_date=start, end_date=end, dimensions=(dim,), max_rows=25000)
            conn.executemany(
                "INSERT OR REPLACE INTO gsc_dim(dim, key, span, clicks, impressions, position) VALUES (?,?,?,?,?,?)",
                [(dim, row["keys"][0], span, row["clicks"], row["impressions"], row["position"]) for row in rows])
    conn.commit()
    set_meta(conn, "gsc_spans", {span: list(pair) for span, pair in spans.items()})
    set_meta(conn, "gsc_counts", counts)
    conn.close()
    return {"spans": spans, **counts}
