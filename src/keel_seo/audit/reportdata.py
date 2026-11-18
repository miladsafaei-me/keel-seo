"""Everything numeric the /seo-strategy report shows, in one file.

The report's tables, charts and checklist statuses come from here, so no
number in the document is typed by a model. Statuses of the technical
checklist are set by fixed thresholds; the words around them are written by
the skill's report writers.
"""
from __future__ import annotations

import json
from collections import Counter
from urllib.parse import urlparse

from . import MODULES
from .store import Workspace

REMOVING = ("merge", "redirect", "noindex", "delete", "create")


def _status(value: float, fix: float = 1, critical: float = float("inf")) -> str:
    return "critical" if value >= critical else "fix" if value >= fix else "ok"


def _checklist(technical: dict, overview: dict, conn) -> list:
    pages = max(overview["pages_crawled"], 1)
    sitemap = technical["sitemap"]
    canonical = technical["canonical"]
    redirects = technical["redirects"]
    broken = technical["broken_internal_links"]
    hygiene = technical["url_hygiene"]
    outside = technical["search_console_urls_outside_the_site"]["by_answer"]
    noindex_traffic = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(impr_3m), 0) i FROM inventory WHERE noindex=1 AND impr_3m > 0").fetchone()
    site_impressions = conn.execute("SELECT COALESCE(SUM(impr_3m), 0) i FROM inventory").fetchone()["i"] or 1
    declared_bad = conn.execute(
        "SELECT COUNT(*) n FROM inventory WHERE status != 200 AND source IN ('sitemap', 'link')").fetchone()["n"]
    bad_targets = sum(count for label, count in canonical["points_elsewhere_targets"].items()
                      if label != "target status 200")
    lastmod_days = sitemap["lastmod_top_days"]
    top_day_share = (lastmod_days[0][1] / max(sitemap["urls"], 1)) if lastmod_days else 0
    languages = technical["language"]["lang_attribute_values"]
    psi = technical["pagespeed"] if isinstance(technical["pagespeed"], list) else []
    field = [row for row in psi if row.get("field_page") or row.get("field_origin")]
    weight = technical["html_weight_kb"]["median"]
    gone_with_demand = outside.get("404", 0)

    def row(key: str, group: str, status: str, **numbers) -> dict:
        return {"id": key, "group": group, "status": status, "numbers": numbers}

    rows = [
        row("status", "reach", _status(declared_bad, 1, 0.05 * pages), pages=declared_bad),
        row("broken_links", "reach", _status(broken["targets"], 1, 50), targets=broken["targets"], links=broken["links"]),
        row("redirect_chains", "reach", _status(len(redirects["chains_of_two_or_more"])),
            chains=len(redirects["chains_of_two_or_more"]), redirecting=redirects["pages_redirecting"]),
        row("links_to_redirects", "reach", _status(redirects["internal_links_pointing_at_redirects"], 1, 1000),
            links=redirects["internal_links_pointing_at_redirects"]),
        row("gone_with_demand", "reach", _status(gone_with_demand), pages=gone_with_demand),
        row("canonical_missing", "index", _status(canonical["missing"], 1, 0.2 * pages), pages=canonical["missing"]),
        row("canonical_target", "index", _status(bad_targets), pages=bad_targets,
            pointing_elsewhere=canonical["points_elsewhere"]),
        row("noindex_with_traffic", "index",
            _status(noindex_traffic["n"], 1, noindex_traffic["n"] if noindex_traffic["i"] >= 0.1 * site_impressions else float("inf")),
            pages=noindex_traffic["n"], impressions_3m=round(noindex_traffic["i"]),
            all_noindex=technical["noindex"]["count"]),
        row("robots_vs_sitemap", "index", _status(len(sitemap["robots_disallowed"])), pages=len(sitemap["robots_disallowed"])),
        row("sitemap_clean", "sitemap",
            _status(len(sitemap["not_200"]) + len(sitemap["redirecting"]) + len(sitemap["noindex"])
                    + len(sitemap["canonical_elsewhere"])),
            not_200=len(sitemap["not_200"]), redirecting=len(sitemap["redirecting"]), noindex=len(sitemap["noindex"]),
            canonical_elsewhere=len(sitemap["canonical_elsewhere"]), urls=sitemap["urls"]),
        row("sitemap_complete", "sitemap", _status(sitemap["indexable_pages_missing_from_sitemap"]["count"]),
            pages=sitemap["indexable_pages_missing_from_sitemap"]["count"]),
        row("sitemap_lastmod", "sitemap", "fix" if sitemap["lastmod_share"] < 0.9 or top_day_share > 0.5 else "ok",
            with_lastmod_percent=round(100 * sitemap["lastmod_share"]), busiest_day_percent=round(100 * top_day_share)),
        row("duplicate_content", "duplication", _status(len(technical["exact_duplicate_content"])),
            groups=len(technical["exact_duplicate_content"])),
        row("query_variants", "duplication", _status(hygiene["internal_links_to_query_urls"]["links"], 100),
            links=hygiene["internal_links_to_query_urls"]["links"]),
        row("url_form", "duplication", _status(len(hygiene["uppercase"]) + len(hygiene["non_ascii"])),
            uppercase=len(hygiene["uppercase"]), encoded=len(hygiene["non_ascii"])),
        row("legacy_urls", "duplication", _status(sum(outside.values()), 1),
            answering_200=outside.get("200 not indexable", 0) + outside.get("200 indexable", 0),
            answering_404=outside.get("404", 0)),
        row("raw_html_text", "rendering", _status(technical["thin_raw_html"]["count"]), pages=technical["thin_raw_html"]["count"]),
        row("html_weight", "rendering", _status(weight, 300, 1000), median_kb=weight,
            heaviest_kb=technical["html_weight_kb"]["heaviest"][0]["kb"] if technical["html_weight_kb"]["heaviest"] else 0),
        row("speed_field", "speed", "unmeasured" if not field else "ok", pages_measured=len(field)),
        row("speed_lab", "speed", "unmeasured" if not psi else "ok", pages_measured=len(psi)),
        row("lang_attribute", "language", _status(languages.get("(missing)", 0)), missing=languages.get("(missing)", 0),
            values=len([key for key in languages if key != "(missing)"])),
    ]
    return rows


def _weekly(conn) -> list:
    """Search Console totals per whole week (Monday to Sunday), oldest first; partial weeks are left out."""
    rows = conn.execute(
        "SELECT date(day, '-' || ((CAST(strftime('%w', day) AS INTEGER) + 6) % 7) || ' days') AS week, "
        "COUNT(*), SUM(clicks), SUM(impressions) FROM gsc_day GROUP BY week ORDER BY week").fetchall()
    return [{"week": week, "clicks": round(clicks or 0), "impressions": round(impressions or 0)}
            for week, days, clicks, impressions in rows if days == 7]


def _demand(urls: list) -> dict:
    """Search demand of the last 91 days that sits on the URLs each finding names."""
    demand: dict = {}
    for item in urls:
        for finding_id in item["findings"]:
            entry = demand.setdefault(finding_id, {"impressions_3m": 0, "clicks_3m": 0, "urls_with_impressions": 0})
            entry["impressions_3m"] += round(item.get("impr_3m", 0))
            entry["clicks_3m"] += round(item.get("clicks_3m", 0))
            entry["urls_with_impressions"] += 1 if item.get("impr_3m", 0) > 0 else 0
    return demand


BUCKETS = ((3.5, "1-3"), (10.5, "4-10"), (20.5, "11-20"), (50.5, "21-50"), (float("inf"), "51+"))


def _is_brand(query: str, brand: list) -> bool:
    return any(term in query.lower() for term in brand)


def _bucket(position: float) -> str:
    return next(label for top, label in BUCKETS if position < top)


def _queries(conn, brand: list) -> dict:
    """Brand against non-brand query totals, and non-brand demand by result position, per 91-day window.

    Query rows are anonymised by Google below a privacy threshold, so these totals are smaller
    than the page totals; read them as shares, not as the site's whole traffic.
    """
    out = {}
    for span in ("3m", "prev3m"):
        split = {side: {"clicks": 0, "impressions": 0, "queries": 0} for side in ("brand", "non_brand")}
        positions = {label: {"clicks": 0, "impressions": 0, "queries": 0} for _top, label in BUCKETS}
        for key, clicks, impressions, position in conn.execute(
                "SELECT key, clicks, impressions, position FROM gsc_dim WHERE dim='query' AND span=?", (span,)):
            side = "brand" if _is_brand(key, brand) else "non_brand"
            for target in (split[side], *(() if side == "brand" else (positions[_bucket(position or 99)],))):
                target["clicks"] += round(clicks or 0)
                target["impressions"] += round(impressions or 0)
                target["queries"] += 1
        out[span] = {"split": split, "positions": positions}
    return out


def _near_page_one(conn, brand: list, limit: int = 12) -> list:
    """Non-brand queries of the last 91 days that sit between position 4 and 20, most impressions first."""
    out = []
    rows = conn.execute("SELECT key, clicks, impressions, position FROM gsc_dim WHERE dim='query' AND span='3m' "
                        "AND position >= 3.5 AND position < 20.5 ORDER BY impressions DESC").fetchall()
    for key, clicks, impressions, position in rows:
        if _is_brand(key, brand):
            continue
        page = conn.execute("SELECT url FROM gsc_query_page WHERE span='3m' AND query=? ORDER BY impressions DESC LIMIT 1",
                            (key,)).fetchone()
        family = conn.execute("SELECT family FROM inventory WHERE url=?", (page[0],)).fetchone() if page else None
        out.append({"query": key, "clicks": round(clicks or 0), "impressions": round(impressions or 0),
                    "position": round(position, 1), "page": page[0] if page else "", "family": family[0] if family else ""})
        if len(out) >= limit:
            break
    return out


def _serp_holders(workspace: Workspace, site: str, limit: int = 10) -> dict:
    """How many of the collected result snapshots each domain appears in (top 10), the audited site included."""
    path = workspace.data / "serp.jsonl"
    if not path.exists():
        return {"queries": 0, "domains": [], "own": 0, "engine": ""}
    own = urlparse(site).netloc.removeprefix("www.")
    counts, queries, engine = Counter(), 0, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        queries += 1
        engine = engine or row.get("engine", "")
        counts.update({urlparse(item.get("url", "")).netloc.removeprefix("www.") for item in row.get("results", [])[:10]} - {""})
    return {"queries": queries, "engine": engine, "own": counts.get(own, 0),
            "domains": [{"domain": domain, "queries": count} for domain, count in counts.most_common(limit + 1) if domain != own][:limit]}


def run(workspace: Workspace) -> dict:
    facts = {name: json.loads((workspace.facts / f"{name}.json").read_text(encoding="utf-8"))
             for name in ("overview", "technical", "performance")}
    reconciled_path = workspace.findings / "reconciled.json"
    if not reconciled_path.exists():
        raise SystemExit("run findings reconcile first")
    reconciled = json.loads(reconciled_path.read_text(encoding="utf-8"))
    conn = workspace.db()
    overview, performance = facts["overview"], facts["performance"]

    demand = _demand(reconciled["urls"])
    empty = {"impressions_3m": 0, "clicks_3m": 0, "urls_with_impressions": 0}
    areas: dict = {}
    for module in MODULES:
        findings = [finding for finding in reconciled["findings"] if finding["module"] == module]
        areas[module] = {
            "severity_counts": dict(Counter(finding["severity"] for finding in findings)),
            "pages_touched": sum(finding.get("pages", 0) for finding in findings
                                 if finding.get("targets", {}).get("scope") != "site"),
            "findings": [{key: finding.get(key) for key in (
                "id", "severity", "confidence", "effort", "action", "original_action", "pages", "url_change",
                "verification", "title", "evidence", "recommendation", "expected_effect", "depends_on",
                "merge_into", "strategist_note", "original_severity")} | {"demand": demand.get(finding["id"], empty)}
                for finding in findings],
        }
    proposals: dict = {}
    by_id = {finding["id"]: finding for finding in reconciled["findings"]}
    for item in reconciled["urls"]:
        if item["action"] not in REMOVING:
            continue
        source = next((fid for fid in item["findings"] if by_id.get(fid, {}).get("action") == item["action"]),
                      item["findings"][0])
        group = proposals.setdefault((item["action"], source), {
            "action": item["action"], "finding": source, "family": item["family"], "pages": 0, "impressions_3m": 0,
            "clicks_3m": 0, "examples": [], "merge_into": item.get("merge_into", "")})
        group["pages"] += 1
        group["impressions_3m"] += item.get("impr_3m", 0)
        group["clicks_3m"] += item.get("clicks_3m", 0)
        if len(group["examples"]) < 3:
            group["examples"].append(item["url"])
    events = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(workspace.facts.glob("event-*.json"))]
    data = {
        "project": overview["project"],
        "crawled_at": overview["crawled_at"],
        "counts": {"crawled": overview["pages_crawled"], "indexable": overview["indexable"],
                   "sitemap_urls": overview["sitemap_urls"], "status": overview["status_counts"],
                   "sources": overview["source_counts"],
                   "redirecting": facts["technical"]["redirects"]["pages_redirecting"]},
        "spans": overview["search_console"],
        "monthly": performance["monthly"],
        "weekly": _weekly(conn),
        "families": [{key: family[key] for key in ("family", "pages", "indexable", "clicks_3m", "impr_3m", "impr_16m",
                                                   "clicks_16m", "median_words")}
                     for family in sorted(overview["families"], key=lambda item: -item["impr_16m"])[:12]],
        "brand_share": performance["brand_share_of_query_clicks"],
        "countries": performance["countries"][:6], "devices": performance["devices"],
        "checklist": _checklist(facts["technical"], overview, conn),
        "areas": areas,
        "summary": {key: reconciled["summary"][key] for key in ("findings_kept", "findings_by_severity", "url_actions")},
        "url_proposals": sorted(proposals.values(), key=lambda group: -group["pages"]),
        "events": events,
        "growth": {"queries": _queries(conn, [term.lower() for term in overview["project"].get("brand_terms", [])]),
                   "near_page_one": _near_page_one(conn, [term.lower() for term in overview["project"].get("brand_terms", [])]),
                   "serp_holders": _serp_holders(workspace, overview["project"]["site"])},
        "data_gaps": overview["data_gaps"],
    }
    conn.close()
    workspace.report.mkdir(parents=True, exist_ok=True)
    path = workspace.report / "data.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"file": str(path), "kb": path.stat().st_size // 1024, "checklist": Counter(row["status"] for row in data["checklist"]),
            "url_proposals": [(group["action"], group["finding"], group["pages"]) for group in data["url_proposals"]]}
