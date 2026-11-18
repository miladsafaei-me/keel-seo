"""Narrow reads for the specialist agents.

An agent never loads the database or a whole crawl. It reads its module's
fact file, then asks for one page, one family or one read-only query at a
time, so its context holds only what it is judging.
"""
from __future__ import annotations

import json
import sqlite3

from . import MODULES
from .store import Workspace

TEXT_CHARS = 7000
MAX_ROWS = 400


def _readonly(workspace: Workspace) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{workspace.data / 'audit.sqlite'}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def sql(workspace: Workspace, statement: str, limit: int = 40) -> dict:
    """Run one SELECT against a read-only handle and cap the rows returned."""
    if not statement.lstrip().lower().startswith(("select", "with")):
        return {"error": "only SELECT statements are accepted"}
    conn = _readonly(workspace)
    try:
        cursor = conn.execute(statement)
        rows = [dict(row) for row in cursor.fetchmany(min(limit, MAX_ROWS))]
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    finally:
        conn.close()
    return {"rows": rows, "returned": len(rows), "capped_at": min(limit, MAX_ROWS)}


def page(workspace: Workspace, url: str) -> dict:
    conn = _readonly(workspace)
    try:
        record = conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        if record is None:
            return {"error": f"{url} is not in the crawl"}
        inventory = conn.execute("SELECT * FROM inventory WHERE url=?", (url,)).fetchone()
        final = record["final_url"] or url
        inbound = conn.execute(
            "SELECT anchor, zone, COUNT(*) links, MIN(src) example FROM links WHERE dst IN (?, ?) "
            "GROUP BY anchor, zone ORDER BY links DESC LIMIT 20", (url, final)).fetchall()
        outbound = conn.execute(
            "SELECT dst, anchor, internal FROM links WHERE src=? AND zone IN ('main', 'body') LIMIT 40", (url,)).fetchall()
        queries = conn.execute(
            "SELECT query, ROUND(clicks) clicks, ROUND(impressions) impr, ROUND(position, 1) pos FROM gsc_query_page "
            "WHERE url=? AND span='3m' ORDER BY impressions DESC LIMIT 20", (url,)).fetchall()
        months = conn.execute(
            "SELECT month, ROUND(clicks) clicks, ROUND(impressions) impr FROM gsc_month WHERE url=? ORDER BY month",
            (url,)).fetchall()
    finally:
        conn.close()
    text = record["main_text"] or ""
    return {
        "inventory": dict(inventory) if inventory else {},
        "meta_description": record["meta_desc"],
        "robots_meta": record["robots_meta"], "x_robots": record["x_robots"],
        "redirect_chain": json.loads(record["redirect_chain"] or "[]"),
        "outline": json.loads(record["outline"] or "[]"),
        "schema_issues": json.loads(record["schema_issues"] or "[]"),
        "hreflang": json.loads(record["hreflang"] or "[]"),
        "og": json.loads(record["og"] or "{}"),
        "images": {"count": record["img_count"], "without_alt": record["img_noalt"]},
        "tables": record["tables"], "lists": record["lists"],
        "queries_3m": [dict(row) for row in queries],
        "months": [dict(row) for row in months],
        "inbound_anchors": [dict(row) for row in inbound],
        "outbound_content_links": [dict(row) for row in outbound],
        "text": text[:TEXT_CHARS],
        "text_truncated_from_chars": len(text) if len(text) > TEXT_CHARS else 0,
    }


def family(workspace: Workspace, name: str, limit: int = 40) -> dict:
    return sql(workspace,
               "SELECT url, status, indexable, words, inlinks_content, depth, title, clicks_3m, impr_3m, pos_3m, "
               "clicks_16m, impr_16m, published FROM inventory WHERE family = '" + name.replace("'", "''") +
               "' ORDER BY impr_3m DESC, impr_16m DESC", limit)


def show(workspace: Workspace, module: str, target: str = "", *, limit: int = 40):
    if module == "page":
        return page(workspace, target)
    if module == "family":
        return family(workspace, target, limit)
    if module == "sql":
        return sql(workspace, target, limit)
    if module in MODULES or module == "overview":
        path = workspace.facts / f"{module}.json"
        if not path.exists():
            return {"error": f"{path} does not exist. Run the build command first."}
        return json.loads(path.read_text(encoding="utf-8"))
    return {"error": f"unknown module {module!r}. Use overview, page, family, sql or one of: {', '.join(MODULES)}"}


def serp_plan(workspace: Workspace, *, maximum: int = 25) -> dict:
    """The queries worth a SERP lookup, most demand first, brand queries left out.

    A lookup costs model tokens, so the list is short on purpose: the top
    query of each priority page, then contested and striking-distance queries.
    """
    brand = [term.lower() for term in workspace.project.get("brand_terms", [])]
    conn = _readonly(workspace)
    try:
        rows = conn.execute(
            "SELECT q.query, q.url, ROUND(q.impressions) impr, ROUND(q.position, 1) pos FROM gsc_query_page q "
            "JOIN inventory i ON i.url = q.url WHERE q.span='3m' AND i.indexable=1 AND q.position > 3 "
            "ORDER BY q.impressions DESC LIMIT 600").fetchall()
    finally:
        conn.close()
    plan, seen_queries, per_page = [], set(), {}
    for row in rows:
        query = row["query"].lower()
        if query in seen_queries or any(term in query for term in brand):
            continue
        if per_page.get(row["url"], 0) >= 2:
            continue
        seen_queries.add(query)
        per_page[row["url"]] = per_page.get(row["url"], 0) + 1
        plan.append({"query": row["query"], "our_url": row["url"], "our_position": row["pos"], "impressions_3m": row["impr"]})
        if len(plan) >= maximum:
            break
    path = workspace.data / "serp-plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"queries": len(plan), "file": str(path), "plan": plan,
            "write_results_to": str(workspace.data / "serp.jsonl"),
            "line_format": {"query": "", "engine": "", "collected_at": "YYYY-MM-DD",
                            "results": [{"rank": 1, "url": "", "title": "", "snippet": "", "page_type": ""}],
                            "features": ["ai overview, featured snippet, people also ask, video, ..."]}}
