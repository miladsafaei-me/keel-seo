"""A dated step change, measured: the days before it against the days after.

When the daily line breaks on one day, the specialists can describe the break
but only a matched pair of windows says what it is. This pulls the same span
on both sides of the date and cuts it by page family, device, country, query
and position, so the report can say what the data rules out. It states no
cause: the Manual actions and Page indexing reports are not in the API.
"""
from __future__ import annotations

import datetime as dt
import os
from collections import Counter, defaultdict

from .parse import normalize_url
from .store import Workspace


def run(workspace: Workspace, day: str, *, days: int = 14) -> dict:
    project = workspace.project
    if project.get("gsc_credentials"):
        os.environ["GSC_CREDENTIALS"] = os.path.expanduser(project["gsc_credentials"])
    from keel_seo.gsc import analytics

    site = project["gsc_property"]
    pivot = dt.date.fromisoformat(day)
    before = ((pivot - dt.timedelta(days=days)).isoformat(), (pivot - dt.timedelta(days=1)).isoformat())
    after = ((pivot + dt.timedelta(days=1)).isoformat(), (pivot + dt.timedelta(days=days)).isoformat())
    brand = [term.lower() for term in project.get("brand_terms", [])]

    def pull(dimension: str, span: tuple) -> dict:
        rows = analytics.fetch_all(site, start_date=span[0], end_date=span[1], dimensions=(dimension,))
        return {row["keys"][0]: (row["clicks"], row["impressions"], row["position"]) for row in rows}

    def pair(dimension: str, limit: int) -> list:
        first, second = pull(dimension, before), pull(dimension, after)
        keys = sorted(first, key=lambda key: -first[key][1])[:limit]
        return [{"key": key, "impressions_before": round(first[key][1]),
                 "impressions_after": round(second.get(key, (0, 0, 0))[1])} for key in keys]

    conn = workspace.db()
    inventory = {row["url"]: (row["family"], row["noindex"], row["indexable"]) for row in conn.execute(
        "SELECT url, family, noindex, indexable FROM inventory")}
    daily = [dict(row) for row in conn.execute(
        "SELECT day, ROUND(clicks) clicks, ROUND(impressions) impressions FROM gsc_day WHERE day BETWEEN ? AND ?",
        ((pivot - dt.timedelta(days=7)).isoformat(), (pivot + dt.timedelta(days=7)).isoformat()))]
    conn.close()

    pages_before = {normalize_url(url): value for url, value in pull("page", before).items()}
    pages_after = {normalize_url(url): value for url, value in pull("page", after).items()}
    families: dict = defaultdict(lambda: [0, 0, 0, 0])
    groups = {"noindex today": [0, 0], "indexable today": [0, 0], "other or not crawled": [0, 0]}
    for index, pages in enumerate((pages_before, pages_after)):
        for url, (clicks, impressions, _position) in pages.items():
            family, noindex, indexable = inventory.get(url, ("(not crawled)", 0, 0))
            families[family][index * 2] += clicks
            families[family][index * 2 + 1] += impressions
            label = "noindex today" if noindex else "indexable today" if indexable else "other or not crawled"
            groups[label][index] += impressions

    queries_before, queries_after = pull("query", before), pull("query", after)
    buckets_before, buckets_after = Counter(), Counter()
    for source, buckets in ((queries_before, buckets_before), (queries_after, buckets_after)):
        for _query, (_clicks, impressions, position) in source.items():
            buckets["1 to 10" if position <= 10 else "11 to 20" if position <= 20 else "21 and deeper"] += impressions

    def brand_total(source: dict) -> list:
        hits = [value for query, value in source.items() if any(term in query.lower() for term in brand)]
        return [round(sum(hit[0] for hit in hits)), round(sum(hit[1] for hit in hits))]

    def total(pages: dict) -> dict:
        return {"clicks": round(sum(value[0] for value in pages.values())),
                "impressions": round(sum(value[1] for value in pages.values())), "pages": len(pages)}

    lost = sorted(queries_before, key=lambda query: -queries_before[query][1])[:15]
    result = {
        "date": day, "days_each_side": days, "before": list(before), "after": list(after),
        "daily": daily,
        "totals": {"before": total(pages_before), "after": total(pages_after)},
        "queries_reported": {"before": len(queries_before), "after": len(queries_after)},
        "brand_queries_clicks_impressions": {"before": brand_total(queries_before), "after": brand_total(queries_after)},
        "impressions_by_position": {"before": {key: round(value) for key, value in buckets_before.items()},
                                    "after": {key: round(value) for key, value in buckets_after.items()}},
        "impressions_by_page_state": {key: [round(value) for value in pair_] for key, pair_ in groups.items()},
        "families": [{"family": family, "clicks_before": round(values[0]), "impressions_before": round(values[1]),
                      "clicks_after": round(values[2]), "impressions_after": round(values[3])}
                     for family, values in sorted(families.items(), key=lambda item: -item[1][1])[:12]],
        "devices": pair("device", 3), "countries": pair("country", 8),
        "largest_queries_before": [{"query": query, "impressions_before": round(queries_before[query][1]),
                                    "position_before": round(queries_before[query][2], 1),
                                    "impressions_after": round(queries_after.get(query, (0, 0, 0))[1])}
                                   for query in lost],
        "note": "This states what changed, never why. The Manual actions and Page indexing reports are only in "
                "the Search Console interface.",
    }
    workspace.write_fact(f"event-{day}", result)
    return result
