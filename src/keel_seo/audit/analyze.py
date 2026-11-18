"""Join the crawl with Search Console and build every candidate list.

Output is two things. The ``inventory`` table holds one row per URL with every
signal, so an agent can ask it a narrow question through ``facts sql``. The
fact files (``facts/<module>.json``) hold what each specialist reads first:
family-level summaries and the short lists worth a judgment.
"""
from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from .store import Workspace, get_meta, set_meta

GENERIC_ANCHORS = {
    "read more", "learn more", "click here", "here", "more", "view", "view all", "see all",
    "see more", "details", "view details", "continue reading", "read review", "visit",
    "visit site", "open", "next", "previous", "back", "home", "link", "this", "more info",
}
EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025,
                9: 0.02, 10: 0.018}
INTENT_PATTERNS = (
    ("transactional", r"\b(login|log in|sign ?up|register|download|open account|bonus|promo|coupon|buy|price|pricing)\b"),
    ("commercial", r"\b(best|top|review|reviews|vs|versus|compare|comparison|alternative|alternatives|legit|scam|safe|trusted|rating)\b"),
    ("informational", r"\b(what|how|why|when|who|guide|tutorial|meaning|definition|explained|example|examples|is it)\b"),
)
LANG_SEGMENT = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
WORD = re.compile(r"[^\W_]+", re.UNICODE)
INVENTORY_COLUMNS = (
    "url TEXT PRIMARY KEY", "family TEXT", "source TEXT", "status INTEGER", "final_url TEXT",
    "hops INTEGER", "indexable INTEGER", "noindex INTEGER", "canonical TEXT", "canonical_self INTEGER",
    "in_sitemap INTEGER", "lastmod TEXT", "depth INTEGER", "inlinks INTEGER", "inlinks_content INTEGER",
    "outlinks INTEGER", "pagerank REAL", "words INTEGER", "title TEXT", "title_len INTEGER",
    "desc_len INTEGER", "h1_count INTEGER", "h1 TEXT", "schema_types TEXT", "author TEXT",
    "published TEXT", "modified TEXT", "bytes INTEGER",
    "clicks_28d REAL", "impr_28d REAL", "clicks_prev28d REAL", "impr_prev28d REAL",
    "clicks_3m REAL", "impr_3m REAL", "pos_3m REAL", "clicks_prev3m REAL", "impr_prev3m REAL",
    "clicks_yoy3m REAL", "impr_yoy3m REAL", "clicks_16m REAL", "impr_16m REAL", "pos_16m REAL",
    "queries_3m INTEGER", "top_query TEXT",
)


def _median(values) -> float:
    values = [value for value in values if value is not None]
    return round(statistics.median(values), 1) if values else 0


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def assign_families(urls: list, overrides: list) -> dict:
    """Group URLs into page families by the shape of their path.

    A path is its language segment (when there is one), its section, then one
    placeholder per deeper segment. A deeper segment stays literal when it is
    one of a few values that repeat under many parents (``/review`` under
    every broker), or one of a handful of values that each hold several pages
    (a category). Where dozens of values repeat, they are names, not a
    vocabulary, and collapse to a placeholder. ``overrides``
    are ``{"name", "pattern"}`` regex rows from project.json and win.
    """
    compiled = [(row["name"], re.compile(row["pattern"])) for row in overrides or []]
    split: dict = {}
    for url in urls:
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
        head = 2 if segments and LANG_SEGMENT.match(segments[0]) else 1
        split[url] = (segments[:head], segments[head:])
    patterns = {url: list(head) for url, (head, _tail) in split.items()}
    for index in range(max((len(tail) for _head, tail in split.values()), default=0)):
        parents: dict = defaultdict(lambda: defaultdict(set))
        children: dict = defaultdict(lambda: defaultdict(set))
        for url, (_head, tail) in split.items():
            if index < len(tail):
                prefix = tuple(patterns[url])
                parents[prefix][tail[index]].add(tuple(tail[:index]))
                if index + 1 < len(tail):
                    children[prefix][tail[index]].add(tail[index + 1])
        repeating = {prefix: sum(1 for found in group.values() if len(found) >= 5)
                     for prefix, group in parents.items()}
        for url, (_head, tail) in split.items():
            if index < len(tail):
                prefix, segment = tuple(patterns[url]), tail[index]
                repeats = len(parents[prefix][segment]) >= 5 and repeating[prefix] <= 8
                few = len(parents[prefix]) <= 12 and len(children[prefix][segment]) >= 3
                patterns[url].append(segment if repeats or few else "{}")
    families: dict = {}
    for url in split:
        name = next((label for label, pattern in compiled if pattern.search(url)), "")
        families[url] = name or "/" + "/".join(patterns[url])
    sizes = Counter(families.values())
    for url, name in families.items():
        if sizes[name] < 3 and not any(name == label for label, _pattern in compiled):
            head = split[url][0]
            lang = head[0] if len(head) == 2 else ""
            families[url] = f"/{lang}/(single pages)" if lang else "/(single pages)"
    return families


def _graph(pages: dict, links: list) -> tuple[dict, dict, dict, dict, dict]:
    """Inlink counts, crawl depth and internal PageRank over resolved links."""
    resolve = {url: (row["final_url"] or url) for url, row in pages.items()}
    edges: dict = defaultdict(set)
    content_in: dict = defaultdict(set)
    all_in: dict = defaultdict(set)
    for src, dst, zone in links:
        target = resolve.get(dst, dst)
        if target not in pages or target == src:
            continue
        edges[src].add(target)
        all_in[target].add(src)
        if zone in ("main", "body"):
            content_in[target].add(src)
    home = min((url for url in pages if urlsplit(url).path in ("/", "")), default=None, key=len)
    depth: dict = {}
    if home:
        frontier = [resolve.get(home, home)]
        depth[frontier[0]] = 0
        while frontier:
            following = []
            for node in frontier:
                for target in edges.get(node, ()):
                    if target not in depth:
                        depth[target] = depth[node] + 1
                        following.append(target)
            frontier = following
    nodes = list(pages)
    rank = {node: 1.0 / len(nodes) for node in nodes} if nodes else {}
    for _round in range(20):
        fresh = {node: 0.15 / len(nodes) for node in nodes}
        for node in nodes:
            targets = edges.get(node)
            if not targets:
                continue
            portion = 0.85 * rank[node] / len(targets)
            for target in targets:
                fresh[target] += portion
        rank = fresh
    return ({url: len(srcs) for url, srcs in all_in.items()},
            {url: len(srcs) for url, srcs in content_in.items()}, depth, rank,
            {url: len(targets) for url, targets in edges.items()})


def _robots_blocks(robots_txt: str) -> list:
    """Disallow rules of the ``*`` group as compiled patterns."""
    rules: list = []
    active = False
    for line in robots_txt.splitlines():
        line = line.split("#", 1)[0].strip()
        name, colon, value = line.partition(":")
        if not colon:
            continue
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            active = value == "*"
        elif active and name in ("disallow", "allow") and value:
            pattern = re.escape(value).replace(r"\*", ".*")
            pattern = pattern[:-2] + "$" if pattern.endswith(r"\$") else pattern
            rules.append((name, len(value), re.compile("^" + pattern)))
    return rules


def _robots_disallowed(rules: list, url: str) -> bool:
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    best = max((rule for rule in rules if rule[2].match(target)), key=lambda rule: (rule[1], rule[0] == "allow"),
               default=None)
    return bool(best and best[0] == "disallow")


def _intent(query: str) -> str:
    lowered = query.lower()
    for label, pattern in INTENT_PATTERNS:
        if re.search(pattern, lowered):
            return label
    return "unclassified"


def _shingles(text: str, size: int = 5) -> set:
    tokens = [token.lower() for token in WORD.findall(text)]
    return {hash(tuple(tokens[index:index + size])) for index in range(max(len(tokens) - size + 1, 0))}


def _top(rows: list, limit: int) -> list:
    return rows[:limit]


class Audit:
    """One build pass. Holds the joined state every fact file is cut from."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.project = workspace.project
        self.conn = workspace.db()
        self.brand = [term.lower() for term in self.project.get("brand_terms", [])]

    def load(self) -> None:
        conn = self.conn
        self.pages = {row["url"]: dict(row) for row in conn.execute(
            "SELECT * FROM pages WHERE fetched_at != ''")}
        self.sitemap = {row["url"]: row["lastmod"] for row in conn.execute("SELECT url, lastmod FROM sitemap_urls")}
        self.links = [(row["src"], row["dst"], row["zone"]) for row in conn.execute(
            "SELECT src, dst, zone FROM links WHERE internal=1")]
        self.gsc: dict = defaultdict(dict)
        for row in conn.execute("SELECT url, span, clicks, impressions, position FROM gsc_page"):
            self.gsc[row["url"]][row["span"]] = (row["clicks"], row["impressions"], row["position"])
        self.queries: dict = defaultdict(list)
        for row in conn.execute(
                "SELECT query, url, clicks, impressions, position FROM gsc_query_page WHERE span='3m'"):
            self.queries[row["url"]].append((row["query"], row["clicks"], row["impressions"], row["position"]))
        for rows in self.queries.values():
            rows.sort(key=lambda item: -item[2])
        self.spans = get_meta(conn, "gsc_spans", {})
        self.robots_txt = get_meta(conn, "robots_txt", "") or ""
        self.has_gsc = bool(self.gsc)

    def build_inventory(self) -> None:
        conn = self.conn
        families = assign_families(list(self.pages), self.project.get("families"))
        inlinks, content_in, depth, rank, outlinks = _graph(self.pages, self.links)
        rules = _robots_blocks(self.robots_txt)
        linked = {dst for _src, dst, _zone in self.links}
        conn.execute("DROP TABLE IF EXISTS inventory")
        conn.execute(f"CREATE TABLE inventory ({', '.join(INVENTORY_COLUMNS)})")
        self.rows: dict = {}
        for url, page in self.pages.items():
            final = page["final_url"] or url
            hops = len(json.loads(page["redirect_chain"] or "[]"))
            noindex = int("noindex" in (page["robots_meta"] or "") or "noindex" in (page["x_robots"] or ""))
            canonical = page["canonical"] or ""
            canonical_self = int(not canonical or canonical == final)
            html = "html" in (page["content_type"] or "")
            indexable = int(page["status"] == 200 and html and hops == 0 and not noindex and canonical_self
                            and not _robots_disallowed(rules, url))
            h1 = json.loads(page["h1"] or "[]")
            spans = self.gsc.get(url, {})

            def metric(span: str, index: int) -> float:
                return round(spans.get(span, (0, 0, 0))[index], 2)

            page_queries = self.queries.get(url, [])
            row = {
                "url": url, "family": families[url],
                "source": "sitemap" if url in self.sitemap else "link" if url in linked else page["source"],
                "status": page["status"],
                "final_url": final, "hops": hops, "indexable": indexable, "noindex": noindex,
                "canonical": canonical, "canonical_self": canonical_self,
                "in_sitemap": int(url in self.sitemap), "lastmod": self.sitemap.get(url, ""),
                "depth": depth.get(final, -1), "inlinks": inlinks.get(final, 0),
                "inlinks_content": content_in.get(final, 0), "outlinks": outlinks.get(url, 0),
                "pagerank": round(rank.get(final, 0) * len(self.pages), 3), "words": page["word_count"],
                "title": page["title"], "title_len": len(page["title"] or ""),
                "desc_len": len(page["meta_desc"] or ""), "h1_count": len(h1), "h1": h1[0] if h1 else "",
                "schema_types": ",".join(json.loads(page["schema_types"] or "[]")), "author": page["author"],
                "published": (page["published"] or "")[:10], "modified": (page["modified"] or "")[:10],
                "bytes": page["bytes"],
                "clicks_28d": metric("28d", 0), "impr_28d": metric("28d", 1),
                "clicks_prev28d": metric("prev28d", 0), "impr_prev28d": metric("prev28d", 1),
                "clicks_3m": metric("3m", 0), "impr_3m": metric("3m", 1), "pos_3m": metric("3m", 2),
                "clicks_prev3m": metric("prev3m", 0), "impr_prev3m": metric("prev3m", 1),
                "clicks_yoy3m": metric("yoy3m", 0), "impr_yoy3m": metric("yoy3m", 1),
                "clicks_16m": metric("16m", 0), "impr_16m": metric("16m", 1), "pos_16m": metric("16m", 2),
                "queries_3m": len(page_queries), "top_query": page_queries[0][0] if page_queries else "",
            }
            self.rows[url] = row
        placeholders = ", ".join("?" for _column in INVENTORY_COLUMNS)
        conn.executemany(f"INSERT INTO inventory VALUES ({placeholders})",
                         [tuple(row.values()) for row in self.rows.values()])
        conn.executemany("UPDATE pages SET family=? WHERE url=?",
                         [(row["family"], url) for url, row in self.rows.items()])
        conn.commit()
        self.by_family: dict = defaultdict(list)
        for row in self.rows.values():
            self.by_family[row["family"]].append(row)

    def read_inventory(self) -> None:
        """Reuse the inventory table of an earlier build instead of rebuilding it."""
        self.rows = {row["url"]: dict(row) for row in self.conn.execute("SELECT * FROM inventory")}
        self.by_family = defaultdict(list)
        for row in self.rows.values():
            self.by_family[row["family"]].append(row)

    def brief(self, row: dict, *extra: str) -> dict:
        keys = ("url", "family", "words", "inlinks_content", "clicks_3m", "impr_3m", "pos_3m",
                "clicks_16m", "impr_16m", *extra)
        return {key: row[key] for key in keys}

    def family_table(self) -> list:
        table = []
        for name, rows in self.by_family.items():
            indexable = [row for row in rows if row["indexable"]]
            table.append({
                "family": name, "pages": len(rows), "indexable": len(indexable),
                "median_words": _median(row["words"] for row in indexable),
                "clicks_3m": round(sum(row["clicks_3m"] for row in rows)),
                "impr_3m": round(sum(row["impr_3m"] for row in rows)),
                "clicks_prev3m": round(sum(row["clicks_prev3m"] for row in rows)),
                "impr_prev3m": round(sum(row["impr_prev3m"] for row in rows)),
                "clicks_16m": round(sum(row["clicks_16m"] for row in rows)),
                "impr_16m": round(sum(row["impr_16m"] for row in rows)),
                "zero_impr_16m_share": _share(sum(1 for row in indexable if not row["impr_16m"]), len(indexable)),
                "zero_click_16m_share": _share(sum(1 for row in indexable if not row["clicks_16m"]), len(indexable)),
                "median_inlinks_content": _median(row["inlinks_content"] for row in indexable),
                "examples": [row["url"] for row in sorted(rows, key=lambda row: -row["impr_16m"])[:3]],
            })
        return sorted(table, key=lambda item: -item["pages"])

    def overview(self) -> dict:
        rows = list(self.rows.values())
        totals = {}
        for span in ("28d", "prev28d", "3m", "prev3m", "yoy3m", "16m"):
            hit = self.conn.execute(
                "SELECT SUM(clicks) c, SUM(impressions) i, COUNT(*) n FROM gsc_page WHERE span=?", (span,)).fetchone()
            totals[span] = {"clicks": round(hit["c"] or 0), "impressions": round(hit["i"] or 0),
                            "pages_with_impressions": hit["n"], "dates": self.spans.get(span)}
        return {
            "project": {key: self.project.get(key) for key in ("name", "site", "language", "business",
                                                                 "brand_terms", "competitors", "notes")},
            "crawled_at": get_meta(self.conn, "crawled_at"),
            "crawl_summary": get_meta(self.conn, "crawl_summary"),
            "pages_crawled": len(rows),
            "status_counts": dict(Counter(str(row["status"]) for row in rows)),
            "source_counts": dict(Counter(row["source"] for row in rows)),
            "indexable": sum(row["indexable"] for row in rows),
            "sitemap_urls": len(self.sitemap),
            "search_console": totals if self.has_gsc else "no Search Console data in this run",
            "families": self.family_table(),
            "data_gaps": self.gaps(),
            "how_to_drill_down": (
                "python -m keel_seo.audit facts <workspace> page <url> for one page's full record; "
                "facts <workspace> family <name> for a family's pages; "
                "facts <workspace> sql \"SELECT ... FROM inventory ...\" for any narrow question "
                "(tables: inventory, pages, links, gsc_query_page, gsc_month, gsc_day, gsc_dim, sitemap_urls)."),
        }

    def gaps(self) -> list:
        gaps = ["Backlink data: none. No off-page module runs; never state anything about referring domains."]
        if not self.has_gsc:
            gaps.append("Search Console: no data. Every traffic statement is unknown.")
        if not (self.workspace.data / "serp.jsonl").exists():
            gaps.append("SERP snapshots: none collected yet (data/serp.jsonl is missing).")
        if not self.conn.execute("SELECT COUNT(*) n FROM psi").fetchone()["n"]:
            gaps.append("PageSpeed: not collected (run the psi command).")
        gaps.append("Rendering: only raw server HTML was read; content that needs JavaScript is not seen.")
        gaps.append("Index coverage: URL Inspection needs a Full user in Search Console; impressions are the proxy.")
        return gaps

    def technical(self) -> dict:
        rows = list(self.rows.values())
        resolve = {url: row["final_url"] for url, row in self.rows.items()}
        status = {url: row["status"] for url, row in self.rows.items()}
        broken: dict = defaultdict(list)
        to_redirect: dict = defaultdict(int)
        for src, dst, _zone in self.links:
            if dst not in status:
                continue
            if status[dst] >= 400 or status[dst] == 0:
                broken[dst].append(src)
            elif self.rows[dst]["hops"]:
                to_redirect[dst] += 1
        not_ok = sorted((row for row in rows if row["status"] != 200), key=lambda row: -row["impr_16m"])
        canonical_other = [row for row in rows if row["status"] == 200 and not row["canonical_self"]]
        canonical_targets = Counter()
        for row in canonical_other:
            target_status = status.get(row["canonical"])
            canonical_targets["target not crawled" if target_status is None else f"target status {target_status}"] += 1
        rules = _robots_blocks(self.robots_txt)
        duplicates: dict = defaultdict(list)
        for url, page in self.pages.items():
            if page["text_hash"] and self.rows[url]["indexable"] and page["word_count"] >= 50:
                duplicates[page["text_hash"]].append(url)
        html_rows = [row for row in rows if row["status"] == 200 and "html" in (self.pages[row["url"]]["content_type"] or "")]
        hreflang_pages = sum(1 for url in self.pages if json.loads(self.pages[url]["hreflang"] or "[]"))
        psi = [json.loads(row["payload"]) | {"url": row["url"], "family": row["family"]}
               for row in self.conn.execute("SELECT * FROM psi")]
        gsc_sources = Counter()
        for row in rows:
            if row["source"] == "gsc":
                label = str(row["status"]) if row["status"] != 200 else (
                    "200 indexable" if row["indexable"] else "200 not indexable")
                gsc_sources[label] += 1
        query_links = Counter()
        for row in self.conn.execute("SELECT dst FROM links WHERE internal=1 AND dst LIKE '%?%'"):
            query_links[urlsplit(row["dst"]).path] += 1
        return {
            "status_counts": dict(Counter(str(row["status"]) for row in rows)),
            "not_200": {"count": len(not_ok),
                        "by_source": dict(Counter(f"{row['source']}:{row['status']}" for row in not_ok)),
                        "top": [self.brief(row, "status", "final_url", "source", "inlinks") for row in _top(not_ok, 60)]},
            "sitemap": {
                "urls": len(self.sitemap),
                "problems": get_meta(self.conn, "sitemap_problems", []),
                "not_200": [url for url in self.sitemap if status.get(url) not in (200, None)][:60],
                "redirecting": [url for url in self.sitemap if url in self.rows and self.rows[url]["hops"]][:60],
                "noindex": [url for url in self.sitemap if url in self.rows and self.rows[url]["noindex"]][:60],
                "canonical_elsewhere": [url for url in self.sitemap
                                        if url in self.rows and self.rows[url]["status"] == 200
                                        and not self.rows[url]["canonical_self"]][:60],
                "robots_disallowed": [url for url in self.sitemap if _robots_disallowed(rules, url)][:60],
                "indexable_pages_missing_from_sitemap": {
                    "count": sum(1 for row in rows if row["indexable"] and not row["in_sitemap"]),
                    "by_family": dict(Counter(row["family"] for row in rows if row["indexable"] and not row["in_sitemap"])),
                    "top": [self.brief(row) for row in sorted(
                        (row for row in rows if row["indexable"] and not row["in_sitemap"]),
                        key=lambda row: -row["impr_16m"])[:30]]},
                "lastmod_share": _share(sum(1 for value in self.sitemap.values() if value), len(self.sitemap)),
                "lastmod_top_days": Counter(value[:10] for value in self.sitemap.values() if value).most_common(8),
            },
            "redirects": {
                "pages_redirecting": sum(1 for row in rows if row["hops"]),
                "chains_of_two_or_more": [{"url": row["url"], "hops": row["hops"], "final_url": row["final_url"]}
                                          for row in rows if row["hops"] >= 2][:40],
                "internal_links_pointing_at_redirects": sum(to_redirect.values()),
                "most_linked_redirects": [{"url": url, "links": count, "final_url": resolve[url]}
                                          for url, count in Counter(to_redirect).most_common(30)],
            },
            "broken_internal_links": {
                "targets": len(broken), "links": sum(len(srcs) for srcs in broken.values()),
                "top": [{"url": url, "status": status[url], "links": len(srcs), "example_sources": srcs[:3]}
                        for url, srcs in sorted(broken.items(), key=lambda item: -len(item[1]))[:50]],
            },
            "canonical": {
                "missing": sum(1 for row in html_rows if not row["canonical"]),
                "missing_by_family": dict(Counter(row["family"] for row in html_rows if not row["canonical"])),
                "points_elsewhere": len(canonical_other),
                "points_elsewhere_targets": dict(canonical_targets),
                "points_elsewhere_top": [{"url": row["url"], "canonical": row["canonical"], "impr_16m": row["impr_16m"],
                                          "in_sitemap": row["in_sitemap"]}
                                         for row in sorted(canonical_other, key=lambda row: -row["impr_16m"])[:40]],
            },
            "noindex": {
                "count": sum(row["noindex"] for row in rows),
                "by_family": dict(Counter(row["family"] for row in rows if row["noindex"])),
                "with_search_traffic": [self.brief(row) for row in sorted(
                    (row for row in rows if row["noindex"] and row["impr_3m"]), key=lambda row: -row["impr_3m"])[:30]],
            },
            "robots_txt": {"status": get_meta(self.conn, "robots_status"), "text": self.robots_txt[:6000]},
            "language": {"lang_attribute_values": dict(Counter(self.pages[row["url"]]["lang"] or "(missing)" for row in html_rows)),
                         "pages_with_hreflang": hreflang_pages},
            "thin_raw_html": {
                "note": "200 pages whose raw HTML carries under 60 words of main content; check them rendered.",
                "count": sum(1 for row in html_rows if row["words"] < 60),
                "by_family": dict(Counter(row["family"] for row in html_rows if row["words"] < 60)),
                "top": [self.brief(row) for row in sorted(
                    (row for row in html_rows if row["words"] < 60), key=lambda row: -row["impr_16m"])[:30]]},
            "html_weight_kb": {
                "median": round(_median(row["bytes"] for row in html_rows) / 1024),
                "by_family_median": {name: round(_median(row["bytes"] for row in family if row["status"] == 200) / 1024)
                                     for name, family in self.by_family.items() if len(family) >= 5},
                "heaviest": [{"url": row["url"], "kb": round(row["bytes"] / 1024)}
                             for row in sorted(html_rows, key=lambda row: -row["bytes"])[:15]]},
            "url_hygiene": {
                "uppercase": [row["url"] for row in rows if re.search(r"[A-Z]", urlsplit(row["url"]).path)][:30],
                "underscore": [row["url"] for row in rows if "_" in urlsplit(row["url"]).path][:30],
                "non_ascii": [row["url"] for row in rows if not row["url"].isascii() or "%" in row["url"]][:30],
                "over_115_chars": sum(1 for row in rows if len(row["url"]) > 115),
                "internal_links_to_query_urls": {"links": sum(query_links.values()),
                                                 "by_path": query_links.most_common(20)},
            },
            "exact_duplicate_content": [
                {"pages": len(urls), "urls": urls[:6]}
                for urls in sorted((urls for urls in duplicates.values() if len(urls) > 1), key=len, reverse=True)[:30]],
            "search_console_urls_outside_the_site": {
                "note": "URLs that earned impressions in 16 months but are neither in a sitemap nor linked; "
                        "crawled to learn what they answer now.",
                "by_answer": dict(gsc_sources),
                "top": [self.brief(row, "status", "final_url", "indexable") for row in sorted(
                    (row for row in rows if row["source"] == "gsc"), key=lambda row: -row["impr_16m"])[:50]]},
            "pagespeed": psi or "not collected",
        }

    def architecture(self) -> dict:
        rows = [row for row in self.rows.values() if row["indexable"]]
        orphans = [row for row in rows if not row["inlinks"]]
        unreachable = [row for row in rows if row["depth"] < 0]
        chrome_only = [row for row in rows if row["inlinks"] and not row["inlinks_content"]]
        anchors: dict = defaultdict(lambda: defaultdict(int))
        generic = total = nofollow = 0
        matrix: dict = defaultdict(int)
        sitewide: dict = defaultdict(set)
        resolve = {url: row["final_url"] for url, row in self.rows.items()}
        for row in self.conn.execute("SELECT src, dst, anchor, rel, zone FROM links WHERE internal=1"):
            if row["src"] not in self.rows:
                continue
            target = resolve.get(row["dst"], row["dst"])
            if row["zone"] in ("nav", "header", "footer"):
                sitewide[(row["zone"], target)].add(row["src"])
                continue
            total += 1
            nofollow += int("nofollow" in row["rel"])
            text = row["anchor"].lower().strip()
            if not text or text in GENERIC_ANCHORS:
                generic += 1
                continue
            if target in self.rows:
                anchors[text][target] += 1
                matrix[(self.rows[row["src"]]["family"], self.rows[target]["family"])] += 1
        conflicts = []
        for text, targets in anchors.items():
            live = {url: count for url, count in targets.items() if self.rows[url]["indexable"]}
            if len(live) >= 2 and sum(live.values()) >= 6 and len(text) >= 4:
                conflicts.append({"anchor": text, "targets": len(live), "links": sum(live.values()),
                                  "top_targets": sorted(live.items(), key=lambda item: -item[1])[:5]})
        conflicts.sort(key=lambda item: -item["links"])
        html_pages = max(sum(1 for row in self.rows.values() if row["status"] == 200), 1)
        money = [path.rstrip("/") for path in self.project.get("business", {}).get("money_pages", [])]
        by_rank = sorted(rows, key=lambda row: -row["pagerank"])
        return {
            "depth_distribution": dict(sorted(Counter(
                "unreachable" if row["depth"] < 0 else "7 or more" if row["depth"] >= 7 else str(row["depth"])
                for row in rows).items())),
            "depth_note": "Clicks from the homepage through any link. Unreachable means no chain of links from "
                          "the homepage leads to the page, even if other unreachable pages link to it.",
            "unreachable_from_home": {
                "count": len(unreachable), "by_family": dict(Counter(row["family"] for row in unreachable)),
                "top": [self.brief(row, "inlinks") for row in sorted(unreachable, key=lambda row: -row["impr_16m"])[:30]]},
            "deepest_chains": [self.brief(row, "depth") for row in sorted(rows, key=lambda row: -row["depth"])[:5]],
            "orphans": {"count": len(orphans), "by_family": dict(Counter(row["family"] for row in orphans)),
                        "top": [self.brief(row) for row in sorted(orphans, key=lambda row: -row["impr_16m"])[:40]]},
            "linked_only_from_navigation_or_footer": {
                "count": len(chrome_only), "by_family": dict(Counter(row["family"] for row in chrome_only))},
            "deep_pages": {"depth_5_or_more": sum(1 for row in rows if row["depth"] >= 5),
                           "by_family": dict(Counter(row["family"] for row in rows if row["depth"] >= 5)),
                           "note": "A long tail of depths usually means pages reached only through a chain of "
                                   "next and previous links."},
            "content_inlinks_by_family": {name: {"median": _median(row["inlinks_content"] for row in family if row["indexable"]),
                                                 "zero": sum(1 for row in family if row["indexable"] and not row["inlinks_content"])}
                                          for name, family in self.by_family.items()},
            "money_pages": [self.brief(row, "inlinks", "depth", "pagerank") for row in self.rows.values()
                            if urlsplit(row["url"]).path.rstrip("/") in money],
            "strongest_pages_by_internal_rank": [self.brief(row, "pagerank", "inlinks") for row in by_rank[:20]],
            "earning_pages_with_weak_links": [self.brief(row, "pagerank", "depth") for row in sorted(
                (row for row in rows if row["inlinks_content"] <= 2 and row["impr_3m"] >= 50),
                key=lambda row: -row["impr_3m"])[:30]],
            "strong_pages_earning_nothing": [self.brief(row, "pagerank", "inlinks") for row in by_rank[:150]
                                             if not row["impr_3m"]][:20],
            "anchor_conflicts": {"note": "One anchor text used for several destination pages, in page content.",
                                 "count": len(conflicts), "top": conflicts[:35]},
            "anchor_quality": {"content_links": total, "generic_or_empty_share": _share(generic, total),
                               "nofollow_internal": nofollow},
            "sitewide_links": [{"zone": zone, "url": url, "on_share_of_pages": _share(len(srcs), html_pages)}
                               for (zone, url), srcs in sorted(sitewide.items(), key=lambda item: -len(item[1]))[:80]
                               if len(srcs) >= html_pages * 0.5],
            "family_link_matrix": [{"from": src, "to": dst, "content_links": count}
                                   for (src, dst), count in sorted(matrix.items(), key=lambda item: -item[1])[:60]],
            "outlink_extremes": [{"url": row["url"], "internal_outlinks": row["outlinks"]}
                                 for row in sorted(rows, key=lambda row: -row["outlinks"])[:10]],
        }

    def inventory_facts(self) -> dict:
        families = []
        for name, rows in self.by_family.items():
            indexable = [row for row in rows if row["indexable"]]
            if not indexable:
                continue
            median = _median(row["words"] for row in indexable)
            thin = [row for row in indexable if row["words"] < max(150, 0.4 * median)]
            dead = [row for row in indexable if not row["impr_16m"]]
            no_click = [row for row in indexable if not row["clicks_16m"]]
            fading = [row for row in indexable if row["impr_prev3m"] >= 20 and row["impr_3m"] < 0.3 * row["impr_prev3m"]]
            families.append({
                "family": name, "indexable": len(indexable), "median_words": median,
                "never_shown_16m": len(dead), "never_clicked_16m": len(no_click), "thin": len(thin),
                "thin_and_never_clicked": sum(1 for row in thin if not row["clicks_16m"]),
                "lost_most_impressions_in_3m": len(fading),
                "clicks_16m": round(sum(row["clicks_16m"] for row in indexable)),
                "pages_holding_90_percent_of_clicks": self._concentration(indexable),
                "oldest_published": min((row["published"] for row in indexable if row["published"]), default=""),
                "newest_published": max((row["published"] for row in indexable if row["published"]), default=""),
            })
        families.sort(key=lambda item: -item["indexable"])
        indexable = [row for row in self.rows.values() if row["indexable"]]
        candidates = sorted((row for row in indexable if not row["clicks_16m"]),
                            key=lambda row: (row["impr_16m"], row["inlinks_content"], row["words"]))
        return {
            "note": "Judge by family first, then name the exceptions. A verdict can target a family with a filter "
                    "over the inventory table instead of listing URLs (see the finding schema).",
            "indexable_pages": len(indexable),
            "never_shown_16m": sum(1 for row in indexable if not row["impr_16m"]),
            "never_clicked_16m": sum(1 for row in indexable if not row["clicks_16m"]),
            "families": families,
            "weakest_pages_sample": [self.brief(row, "published", "in_sitemap") for row in candidates[:60]],
            "never_clicked_but_well_linked": [self.brief(row, "inlinks") for row in sorted(
                (row for row in indexable if not row["clicks_16m"]), key=lambda row: -row["inlinks_content"])[:25]],
            "gone_pages_still_in_search": [self.brief(row, "status", "final_url") for row in sorted(
                (row for row in self.rows.values() if row["status"] != 200 and row["impr_3m"]),
                key=lambda row: -row["impr_3m"])[:40]],
        }

    @staticmethod
    def _concentration(rows: list) -> int:
        clicks = sorted((row["clicks_16m"] for row in rows), reverse=True)
        target, running = 0.9 * sum(clicks), 0
        for count, value in enumerate(clicks, 1):
            running += value
            if running >= target:
                return count if target else 0
        return 0

    def cannibalization(self) -> dict:
        by_query: dict = defaultdict(list)
        span_used = "3m"
        rows = self.conn.execute(
            "SELECT query, url, clicks, impressions, position FROM gsc_query_page WHERE span='3m'").fetchall()
        if len(rows) < 500:
            span_used = "16m"
            rows = self.conn.execute(
                "SELECT query, url, clicks, impressions, position FROM gsc_query_page WHERE span='16m'").fetchall()
        for row in rows:
            by_query[row["query"]].append((row["url"], row["clicks"], row["impressions"], row["position"]))
        pairs: dict = defaultdict(lambda: {"queries": 0, "shared_impr": 0, "examples": []})
        contested = []
        for query, hits in by_query.items():
            total = sum(hit[2] for hit in hits)
            strong = sorted((hit for hit in hits if hit[2] >= max(5, 0.1 * total)), key=lambda hit: -hit[2])
            if len(strong) < 2:
                continue
            contested.append({"query": query, "impressions": round(total), "intent": _intent(query),
                              "pages": [{"url": url, "impr": round(impr), "clicks": round(clicks), "pos": round(pos, 1)}
                                        for url, clicks, impr, pos in strong[:4]]})
            for first in range(len(strong)):
                for second in range(first + 1, len(strong)):
                    key = tuple(sorted((strong[first][0], strong[second][0])))
                    pair = pairs[key]
                    pair["queries"] += 1
                    pair["shared_impr"] += min(strong[first][2], strong[second][2])
                    if len(pair["examples"]) < 4:
                        pair["examples"].append(query)
        contested.sort(key=lambda item: -item["impressions"])
        link_counts = Counter()
        wanted = set(pairs)
        for src, dst, _zone in self.links:
            if (src, dst) in wanted or (dst, src) in wanted:
                link_counts[(src, dst)] += 1
        pair_rows = []
        for (first, second), pair in pairs.items():
            def side(url: str) -> dict:
                row = self.rows.get(url)
                return ({"url": url, "family": row["family"], "status": row["status"], "indexable": row["indexable"],
                         "title": row["title"], "words": row["words"], "clicks": row["clicks_3m"],
                         "impr": row["impr_3m"], "pos": row["pos_3m"], "inlinks_content": row["inlinks_content"]}
                        if row else {"url": url, "status": "not crawled"})
            pair_rows.append({"shared_queries": pair["queries"], "shared_impressions": round(pair["shared_impr"]),
                              "example_queries": pair["examples"], "a": side(first), "b": side(second),
                              "links_a_to_b": link_counts[(first, second)], "links_b_to_a": link_counts[(second, first)]})
        pair_rows.sort(key=lambda item: (-item["shared_impressions"], -item["shared_queries"]))

        titles: dict = defaultdict(list)
        for row in self.rows.values():
            if row["indexable"] and row["title"]:
                core = re.split(r"\s[|\-:]\s", row["title"].lower())[0].strip()
                titles[core].append(row["url"])
        entity: dict = defaultdict(set)
        for url, row in self.rows.items():
            if not row["indexable"]:
                continue
            pattern = row["family"].strip("/").split("/")
            segments = [segment for segment in urlsplit(url).path.split("/") if segment]
            for name, segment in zip(pattern, segments):
                if name == "{}" and len(segment) >= 4:
                    entity[segment].add(url)
        clusters = []
        for slug, urls in entity.items():
            families = {self.rows[url]["family"] for url in urls}
            if len(urls) >= 2 and len(families) >= 2:
                clusters.append({"entity": slug, "pages": len(urls), "impr_3m": round(sum(self.rows[url]["impr_3m"] for url in urls)),
                                 "members": [{"url": url, "family": self.rows[url]["family"], "impr_3m": self.rows[url]["impr_3m"],
                                              "clicks_3m": self.rows[url]["clicks_3m"], "top_query": self.rows[url]["top_query"]}
                                             for url in sorted(urls, key=lambda url: -self.rows[url]["impr_3m"])[:6]]})
        clusters.sort(key=lambda item: (-item["impr_3m"], -item["pages"]))
        shape = Counter()
        for cluster in clusters:
            shape[" + ".join(sorted({member["family"] for member in cluster["members"]}))] += 1
        return {
            "span": span_used,
            "note": "A contested query is one where two or more URLs each hold at least 10 percent of its impressions. "
                    "Sharing a query is a candidate, not a verdict: two pages can serve two intents.",
            "contested_queries": {"count": len(contested), "top": contested[:60]},
            "page_pairs": {"count": len(pair_rows), "top": pair_rows[:70]},
            "identical_title_cores": [{"title_core": core, "pages": len(urls), "urls": urls[:6]}
                                      for core, urls in sorted(titles.items(), key=lambda item: -len(item[1]))[:40]
                                      if len(urls) > 1],
            "entity_clusters": {
                "note": "One entity (a slug) that has pages in several families. The shape table says how common each "
                        "combination is; decide per shape, then name exceptions.",
                "count": len(clusters), "shapes": shape.most_common(20), "top": clusters[:25]},
        }

    def scaled(self) -> dict:
        randomizer = random.Random(7)
        cards = []
        for name, rows in self.by_family.items():
            indexable = [row for row in rows if row["indexable"]]
            if len(indexable) < 15:
                continue
            sample = randomizer.sample(indexable, min(len(indexable), 120))
            shingle_sets = {row["url"]: _shingles(self.pages[row["url"]]["main_text"] or "") for row in sample}
            frequency = Counter()
            for shingles in shingle_sets.values():
                frequency.update(shingles)
            template = {shingle for shingle, count in frequency.items() if count >= 0.6 * len(sample)}
            ratios, unique_sizes, unique_sets = [], [], {}
            for url, shingles in shingle_sets.items():
                if not shingles:
                    continue
                own = shingles - template
                ratios.append(1 - len(own) / len(shingles))
                unique_sizes.append(len(own))
                unique_sets[url] = own
            urls = list(unique_sets)
            near = set()
            for first in range(len(urls)):
                left = unique_sets[urls[first]]
                for second in range(first + 1, len(urls)):
                    right = unique_sets[urls[second]]
                    if not left or not right:
                        continue
                    overlap = len(left & right)
                    if overlap and overlap / (len(left) + len(right) - overlap) >= 0.6:
                        near.update((urls[first], urls[second]))
            published = Counter(row["published"] for row in indexable if row["published"])
            lastmods = Counter(row["lastmod"][:10] for row in indexable if row["lastmod"])
            authors = Counter(row["author"] or "(none)" for row in indexable)
            skeletons = Counter(re.sub(r"\d+", "#", re.sub(r"\b[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*)*", "X", row["title"]))
                                for row in indexable if row["title"])
            ordered = sorted(indexable, key=lambda row: row["words"])
            cards.append({
                "family": name, "indexable": len(indexable), "sampled": len(sample),
                "median_words": _median(row["words"] for row in indexable),
                "words_p10_p90": [ordered[len(ordered) // 10]["words"], ordered[(len(ordered) * 9) // 10]["words"]],
                "template_share_median": round(_median(ratios), 2),
                "unique_shingles_median": _median(unique_sizes),
                "near_duplicate_share": _share(len(near), len(urls)),
                "never_shown_16m_share": _share(sum(1 for row in indexable if not row["impr_16m"]), len(indexable)),
                "clicks_16m_per_page": round(sum(row["clicks_16m"] for row in indexable) / len(indexable), 2),
                "authors": authors.most_common(4),
                "published_dates_known_share": _share(sum(published.values()), len(indexable)),
                "busiest_publish_days": published.most_common(4),
                "busiest_lastmod_days": lastmods.most_common(4),
                "title_shapes": [[shape, count] for shape, count in skeletons.most_common(3)],
                "samples": {"shortest": [row["url"] for row in ordered[:3]],
                            "typical": [row["url"] for row in ordered[len(ordered) // 2 - 1:len(ordered) // 2 + 2]],
                            "longest": [row["url"] for row in ordered[-3:]],
                            "near_duplicates": sorted(near)[:6]},
            })
        cards.sort(key=lambda card: -card["indexable"])
        trust_pages = [row["url"] for row in self.rows.values() if row["status"] == 200 and re.search(
            r"/(about|editorial|methodology|how-we|authors?|team|contact|disclosure|privacy|terms)", row["url"])]
        return {
            "note": "template_share is the part of a page's text that at least 60 percent of its family also carries. "
                    "near_duplicate_share counts pages whose non-template text overlaps another page by 60 percent or more. "
                    "These are signals for a human-grade read of the samples, never a verdict by themselves.",
            "ymyl": self.project.get("business", {}).get("ymyl", False),
            "families": cards,
            "author_values_sitewide": Counter(row["author"] or "(none)" for row in self.rows.values()
                                              if row["indexable"]).most_common(10),
            "trust_pages_found": trust_pages[:30],
        }

    def serp(self) -> dict:
        path = self.workspace.data / "serp.jsonl"
        found: dict = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    found[record["query"].lower()] = record
        return found

    def intent(self) -> dict:
        indexable = [row for row in self.rows.values() if row["indexable"]]
        serp = self.serp()

        def page_card(row: dict) -> dict:
            card = self.brief(row, "title", "h1")
            card["ctr_3m"] = _share(row["clicks_3m"], row["impr_3m"])
            card["top_queries"] = [
                {"query": query, "impr": round(impr), "clicks": round(clicks), "pos": round(pos, 1),
                 "intent": _intent(query), "serp_collected": query.lower() in serp}
                for query, clicks, impr, pos in self.queries.get(row["url"], [])[:6]]
            return card

        priority = sorted((row for row in indexable if row["impr_3m"] >= 30 and row["pos_3m"] > 3),
                          key=lambda row: -row["impr_3m"])
        low_ctr = []
        for row in indexable:
            position = round(row["pos_3m"])
            if row["impr_3m"] >= 100 and 1 <= position <= 10:
                expected = EXPECTED_CTR[position]
                actual = row["clicks_3m"] / row["impr_3m"]
                if actual < 0.5 * expected:
                    low_ctr.append({**self.brief(row, "title"), "ctr": round(actual, 4), "expected_ctr": expected})
        low_ctr.sort(key=lambda item: -item["impr_3m"])
        striking = [dict(row) for row in self.conn.execute(
            "SELECT query, url, ROUND(impressions) impr, ROUND(clicks) clicks, ROUND(position, 1) pos "
            "FROM gsc_query_page WHERE span='3m' AND position BETWEEN 8 AND 20 AND impressions >= 30 "
            "ORDER BY impressions DESC LIMIT 60")]
        intents = Counter()
        for row in self.conn.execute("SELECT key, impressions FROM gsc_dim WHERE dim='query' AND span='3m'"):
            intents[_intent(row["key"])] += row["impressions"]
        return {
            "note": "Priority pages have demand but sit below position 3. Read each page (facts page <url>) against "
                    "what the query's results reward; use SERP snapshots where they exist and say so where they do not.",
            "impressions_by_intent_pattern": {label: round(value) for label, value in intents.most_common()},
            "priority_pages": [page_card(row) for row in priority[:30]],
            "low_ctr_on_page_one": low_ctr[:30],
            "expected_ctr_note": "expected_ctr is a rough position curve, not a measurement of this market.",
            "striking_distance_queries": striking,
            "serp_snapshots": {"collected": len(serp), "file": "data/serp.jsonl",
                               "queries": sorted(serp)[:60]},
        }

    def structure(self) -> dict:
        families = []
        problems = {"no_h1": [], "several_h1": [], "long_with_few_headings": []}
        for name, rows in self.by_family.items():
            indexable = [row for row in rows if row["indexable"]]
            if not indexable:
                continue
            skips = no_h2 = with_table = with_list = 0
            long_pages = 0
            first_paras, skeletons, h2_texts = [], Counter(), Counter()
            for row in indexable:
                page = self.pages[row["url"]]
                outline = json.loads(page["outline"] or "[]")
                levels = [level for level, _text in outline]
                skips += int(any(later - earlier > 1 for earlier, later in zip(levels, levels[1:])))
                if row["words"] >= 300:
                    long_pages += 1
                    no_h2 += int(2 not in levels)
                with_table += int(page["tables"] > 0)
                with_list += int(page["lists"] > 0)
                first_paras.append(page["first_para_words"])
                skeletons["".join(str(level) for level in levels[:25])] += 1
                h2_texts.update({text.lower() for level, text in outline if level == 2 and text})
                if row["h1_count"] == 0:
                    problems["no_h1"].append(row)
                elif row["h1_count"] > 1:
                    problems["several_h1"].append(row)
                if row["words"] >= 1500 and len(levels) < 4:
                    problems["long_with_few_headings"].append(row)
            families.append({
                "family": name, "indexable": len(indexable),
                "h1_not_exactly_one_share": _share(sum(1 for row in indexable if row["h1_count"] != 1), len(indexable)),
                "heading_level_skip_share": _share(skips, len(indexable)),
                "no_h2_share_of_long_pages": _share(no_h2, long_pages),
                "median_first_paragraph_words": _median(first_paras),
                "with_table_share": _share(with_table, len(indexable)),
                "with_list_share": _share(with_list, len(indexable)),
                "median_words": _median(row["words"] for row in indexable),
                "most_common_heading_level_sequence": skeletons.most_common(1)[0] if skeletons else None,
                "h2_texts_on_half_or_more_pages": [text for text, count in h2_texts.most_common(25)
                                                   if count >= 0.5 * len(indexable)],
                "read_these": [row["url"] for row in sorted(indexable, key=lambda row: -row["impr_3m"])[:3]],
            })
        families.sort(key=lambda item: -item["indexable"])
        return {
            "note": "A family whose pages share one template is judged once, on its template, by reading the "
                    "pages under read_these. Report a template fault once with its page count, never per page.",
            "families": families,
            "pages": {key: {"count": len(rows), "by_family": dict(Counter(row["family"] for row in rows)),
                            "top": [self.brief(row, "h1_count") for row in sorted(rows, key=lambda row: -row["impr_3m"])[:20]]}
                      for key, rows in problems.items()},
        }

    def snippet(self) -> dict:
        indexable = [row for row in self.rows.values() if row["indexable"]]
        titles: dict = defaultdict(list)
        descriptions: dict = defaultdict(list)
        for row in indexable:
            if row["title"]:
                titles[row["title"].lower()].append(row["url"])
            description = self.pages[row["url"]]["meta_desc"]
            if description:
                descriptions[description.lower()].append(row["url"])
        families = []
        issue_counts: dict = defaultdict(Counter)
        for name, rows in self.by_family.items():
            pages = [row for row in rows if row["indexable"]]
            if len(pages) < 5:
                continue
            types = Counter()
            for row in pages:
                types.update(filter(None, row["schema_types"].split(",")))
                for issue in json.loads(self.pages[row["url"]]["schema_issues"] or "[]"):
                    issue_counts[issue][name] += 1
            suffixes = Counter(re.split(r"\s[|\-:]\s", row["title"])[-1] for row in pages
                               if row["title"] and re.search(r"\s[|\-:]\s", row["title"]))
            og_missing = sum(1 for row in pages if not json.loads(self.pages[row["url"]]["og"] or "{}").get("image"))
            families.append({
                "family": name, "indexable": len(pages),
                "title_missing": sum(1 for row in pages if not row["title"]),
                "title_over_60": sum(1 for row in pages if row["title_len"] > 60),
                "title_under_25": sum(1 for row in pages if 0 < row["title_len"] < 25),
                "median_title_len": _median(row["title_len"] for row in pages),
                "title_equals_h1_share": _share(sum(1 for row in pages if row["title"] and row["title"] == row["h1"]), len(pages)),
                "common_title_suffix": suffixes.most_common(1)[0] if suffixes else None,
                "desc_missing": sum(1 for row in pages if not row["desc_len"]),
                "desc_over_160": sum(1 for row in pages if row["desc_len"] > 160),
                "desc_under_70": sum(1 for row in pages if 0 < row["desc_len"] < 70),
                "median_desc_len": _median(row["desc_len"] for row in pages),
                "og_image_missing": og_missing,
                "schema_types_share": {kind: _share(count, len(pages)) for kind, count in types.most_common(10)},
                "no_schema": sum(1 for row in pages if not row["schema_types"]),
                "author_in_markup_share": _share(sum(1 for row in pages if row["author"]), len(pages)),
                "examples": [{"url": row["url"], "title": row["title"],
                              "description": self.pages[row["url"]]["meta_desc"][:220]}
                             for row in sorted(pages, key=lambda row: -row["impr_3m"])[:2]],
            })
        families.sort(key=lambda item: -item["indexable"])
        return {
            "note": "Length limits are character counts, a proxy for the pixel width a result shows. Judge a family's "
                    "title pattern once; name single pages only where demand justifies hand-writing.",
            "families": families,
            "duplicate_titles": {"groups": sum(1 for urls in titles.values() if len(urls) > 1),
                                 "pages": sum(len(urls) for urls in titles.values() if len(urls) > 1),
                                 "top": [{"title": title, "pages": len(urls), "urls": urls[:5]}
                                         for title, urls in sorted(titles.items(), key=lambda item: -len(item[1]))[:25]
                                         if len(urls) > 1]},
            "duplicate_descriptions": {"groups": sum(1 for urls in descriptions.values() if len(urls) > 1),
                                       "pages": sum(len(urls) for urls in descriptions.values() if len(urls) > 1),
                                       "top": [{"description": text[:200], "pages": len(urls), "urls": urls[:5]}
                                               for text, urls in sorted(descriptions.items(), key=lambda item: -len(item[1]))[:25]
                                               if len(urls) > 1]},
            "schema_issues": [{"issue": issue, "pages": sum(counts.values()), "by_family": dict(counts.most_common(6))}
                              for issue, counts in sorted(issue_counts.items(), key=lambda item: -sum(item[1].values()))[:40]],
            "low_ctr_on_page_one": self.intent()["low_ctr_on_page_one"],
        }

    def performance(self) -> dict:
        conn = self.conn
        days = conn.execute("SELECT day, clicks, impressions, position FROM gsc_day ORDER BY day").fetchall()
        weeks = []
        for start in range(len(days) % 7, len(days), 7):
            chunk = days[start:start + 7]
            impressions = sum(day["impressions"] for day in chunk)
            weeks.append({"week_from": chunk[0]["day"], "clicks": round(sum(day["clicks"] for day in chunk)),
                          "impressions": round(impressions),
                          "position": round(sum(day["position"] * day["impressions"] for day in chunk) / impressions, 1)
                          if impressions else 0})
        months = [dict(row) for row in conn.execute(
            "SELECT month, ROUND(SUM(clicks)) clicks, ROUND(SUM(impressions)) impressions, COUNT(*) pages_with_impressions "
            "FROM gsc_month GROUP BY month ORDER BY month")]
        family_month: dict = defaultdict(dict)
        for row in conn.execute(
                "SELECT COALESCE(i.family, '(not on the site now)') family, m.month, ROUND(SUM(m.clicks)) clicks, "
                "ROUND(SUM(m.impressions)) impressions, COUNT(*) pages FROM gsc_month m "
                "LEFT JOIN inventory i ON i.url = m.url GROUP BY 1, 2"):
            family_month[row["family"]][row["month"]] = [row["clicks"], row["impressions"], row["pages"]]
        rows = list(self.rows.values())

        def movers(key_now: str, key_before: str, reverse: bool) -> list:
            ordered = sorted(rows, key=lambda row: row[key_now] - row[key_before], reverse=reverse)
            return [{"url": row["url"], "family": row["family"], "status": row["status"], "indexable": row["indexable"],
                     "now": row[key_now], "before": row[key_before]} for row in ordered[:25]
                    if (row[key_now] - row[key_before]) * (1 if reverse else -1) > 0]

        lost = [row for row in rows if row["impr_prev3m"] >= 10 and not row["impr_3m"]]
        brand_now = brand_before = total_now = total_before = 0
        query_rows: dict = defaultdict(dict)
        for row in conn.execute("SELECT key, span, clicks, impressions, position FROM gsc_dim WHERE dim='query'"):
            query_rows[row["key"]][row["span"]] = (row["clicks"], row["impressions"], row["position"])
            is_brand = any(term in row["key"].lower() for term in self.brand)
            if row["span"] == "3m":
                total_now += row["clicks"]
                brand_now += row["clicks"] if is_brand else 0
            else:
                total_before += row["clicks"]
                brand_before += row["clicks"] if is_brand else 0

        def dimension(name: str, limit: int) -> list:
            table: dict = defaultdict(dict)
            for row in conn.execute("SELECT key, span, clicks, impressions FROM gsc_dim WHERE dim=?", (name,)):
                table[row["key"]][row["span"]] = [round(row["clicks"]), round(row["impressions"])]
            return [{"key": key, "now_3m": spans.get("3m", [0, 0]), "prev_3m": spans.get("prev3m", [0, 0])}
                    for key, spans in sorted(table.items(), key=lambda item: -item[1].get("3m", [0, 0])[1])[:limit]]

        top_queries = sorted(query_rows.items(), key=lambda item: -item[1].get("3m", (0, 0, 0))[1])[:50]
        totals = self.overview()["search_console"]
        return {
            "note": "Query-level numbers are anonymised by Google below a privacy threshold, so query totals are "
                    "smaller than page totals. Position is an impression-weighted average; do not compare it across "
                    "windows whose query mix changed.",
            "spans": totals,
            "weekly": weeks,
            "monthly": months,
            "family_by_month": {name: months_map for name, months_map in sorted(
                family_month.items(), key=lambda item: -sum(value[1] for value in item[1].values()))[:25]},
            "winners_impressions_3m": movers("impr_3m", "impr_prev3m", True),
            "losers_impressions_3m": movers("impr_3m", "impr_prev3m", False),
            "winners_clicks_3m": movers("clicks_3m", "clicks_prev3m", True),
            "losers_clicks_3m": movers("clicks_3m", "clicks_prev3m", False),
            "losers_impressions_28d": movers("impr_28d", "impr_prev28d", False),
            "pages_that_vanished_from_search": {
                "note": "Had 10 or more impressions in the previous 91 days and none in the last 91.",
                "count": len(lost), "by_family": dict(Counter(row["family"] for row in lost)),
                "by_status": dict(Counter(str(row["status"]) for row in lost)),
                "by_indexable": dict(Counter("indexable" if row["indexable"] else "not indexable" for row in lost))},
            "brand_share_of_query_clicks": {"now_3m": _share(round(brand_now), round(total_now)),
                                            "prev_3m": _share(round(brand_before), round(total_before)),
                                            "brand_terms": self.brand},
            "countries": dimension("country", 12),
            "devices": dimension("device", 4),
            "search_appearance": dimension("searchAppearance", 10),
            "top_queries": [{"query": query, "intent": _intent(query),
                             "now_3m": [round(value, 1) for value in spans.get("3m", (0, 0, 0))],
                             "prev_3m": [round(value, 1) for value in spans.get("prev3m", (0, 0, 0))]}
                            for query, spans in top_queries],
        }

    def competitor(self) -> dict:
        serp = self.serp()
        domains = Counter()
        ahead = Counter()
        host = urlsplit(self.project["site"]).netloc.lower().removeprefix("www.")
        for record in serp.values():
            ours = next((result.get("rank", index + 1) for index, result in enumerate(record.get("results", []))
                         if host in result.get("url", "")), None)
            for index, result in enumerate(record.get("results", [])[:10]):
                domain = urlsplit(result.get("url", "")).netloc.lower().removeprefix("www.")
                if not domain or domain == host:
                    continue
                domains[domain] += 1
                if ours is None or result.get("rank", index + 1) < ours:
                    ahead[domain] += 1
        return {
            "note": "Competitors named on the project card are a starting list. The SERP table shows who actually "
                    "holds the results for this site's own queries. No traffic or backlink figure exists for any "
                    "competitor in this run; never state one.",
            "named_competitors": self.project.get("competitors", []),
            "serp_queries_collected": len(serp),
            "domains_in_top_10": [{"domain": domain, "queries": count, "ranked_above_us": ahead[domain]}
                                  for domain, count in domains.most_common(30)],
            "serp_file": "data/serp.jsonl",
            "our_sections": [{"family": item["family"], "pages": item["pages"], "impr_3m": item["impr_3m"]}
                             for item in self.family_table()[:25]],
        }


def run(workspace: Workspace, only: tuple = ()) -> dict:
    """Build everything, or with ``only`` refresh the named fact files.

    A partial build leaves the inventory table alone, so it is safe while
    agents are reading it: use it when SERP snapshots arrive after the build.
    """
    audit = Audit(workspace)
    audit.load()
    if not audit.pages:
        raise SystemExit("no crawled pages yet. Run the crawl command first.")
    if only:
        audit.read_inventory()
    else:
        audit.build_inventory()
    written = {}
    for name, builder in (
            ("overview", audit.overview), ("technical", audit.technical), ("architecture", audit.architecture),
            ("inventory", audit.inventory_facts), ("cannibalization", audit.cannibalization),
            ("scaled", audit.scaled), ("intent", audit.intent), ("structure", audit.structure),
            ("snippet", audit.snippet), ("performance", audit.performance), ("competitor", audit.competitor)):
        if only and name not in only:
            continue
        path = workspace.write_fact(name, builder())
        written[name] = f"{path.stat().st_size // 1024} KB"
    if not only:
        set_meta(audit.conn, "built", written)
    audit.conn.close()
    return {"pages": len(audit.rows), "families": len(audit.by_family), "facts": written}
