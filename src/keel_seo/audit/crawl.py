"""Discover a site's URLs and crawl them into the workspace database.

Discovery reads robots.txt and every sitemap, then follows internal links
found on crawled pages, so the audit sees both what the site declares and what
it actually links to. The crawl is resumable: a URL already answered is not
asked again unless ``refresh`` is set.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit

from . import egress
from .parse import normalize_url, parse_page, same_site
from .store import Workspace, set_meta

ASSET = re.compile(
    r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|css|js|mjs|json|xml|txt|pdf|zip|gz|mp4|webm|mp3|"
    r"woff2?|ttf|eot|csv|xlsx?|docx?|pptx?|rss|atom)$", re.IGNORECASE)
LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)\s*(?:\]\]>)?\s*</loc>", re.IGNORECASE)
LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)
URL_BLOCK = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
CHALLENGE = ("just a moment", "attention required", "verify you are human", "cf-chl-")


def _blocked(status: int, headers: dict, body: str) -> bool:
    if status not in (403, 429, 503):
        return False
    head = body[:4000].lower()
    return "cf-mitigated" in headers or any(mark in head for mark in CHALLENGE)


def _log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def _sitemaps(pool, origin: str, robots: str) -> tuple[dict, list]:
    """Every URL the sitemaps declare, with its lastmod and source file."""
    queue = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots) or [f"{origin}/sitemap.xml"]
    seen: set = set()
    found: dict = {}
    problems: list = []
    while queue:
        sitemap = queue.pop(0)
        if sitemap in seen or len(seen) > 500:
            continue
        seen.add(sitemap)
        if sitemap.lower().endswith(".gz"):
            problems.append(f"{sitemap}: gzip sitemap files are not read")
            continue
        response = egress.fetch(pool, sitemap, blocked=_blocked)
        if response.status != 200:
            problems.append(f"{sitemap}: status {response.status or response.error}")
            continue
        for kind, block in URL_BLOCK.findall(response.body):
            loc = LOC.search(block)
            if not loc:
                continue
            target = loc.group(1).strip().replace("&amp;", "&")
            if kind.lower() == "sitemap":
                queue.append(target)
                continue
            lastmod = LASTMOD.search(block)
            found.setdefault(normalize_url(target), (sitemap, lastmod.group(1) if lastmod else ""))
    found.pop("", None)
    return found, problems


def _crawlable(url: str, host: str, include_query: bool) -> bool:
    parts = urlsplit(url)
    if not same_site(url, host) or ASSET.search(parts.path):
        return False
    return include_query or not parts.query


def _one(pool, url: str, host: str) -> tuple[str, dict, list]:
    response = egress.fetch(pool, url, blocked=_blocked)
    if response.status >= 400 or not response.status:
        retry = egress.fetch(pool, url, blocked=_blocked)
        if retry.status and (not response.status or retry.status < response.status):
            retry.attempts += response.attempts
            response = retry
    row = {
        "status": response.status,
        "final_url": normalize_url(response.final_url) or url,
        "redirect_chain": json.dumps(response.chain),
        "content_type": response.headers.get("content-type", "").split(";")[0].strip().lower(),
        "bytes": len(response.body.encode("utf-8")),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "attempts": response.attempts,
        "error": response.error,
        "x_robots": response.headers.get("x-robots-tag", "").lower(),
    }
    links: list = []
    if response.status == 200 and "html" in row["content_type"]:
        fields, links = parse_page(row["final_url"], response.body, host)
        row.update(fields)
    return url, row, links


def _save(conn, url: str, row: dict, links: list) -> None:
    columns = ", ".join(f"{key}=?" for key in row)
    conn.execute(f"UPDATE pages SET {columns} WHERE url=?", (*row.values(), url))
    conn.execute("DELETE FROM links WHERE src=?", (url,))
    conn.executemany(
        "INSERT INTO links(src, dst, anchor, rel, zone, internal) VALUES (?,?,?,?,?,?)",
        [(url, link["dst"], link["anchor"], link["rel"], link["zone"], link["internal"]) for link in links],
    )


def run(workspace: Workspace, *, refresh: bool = False, limit: int = 0) -> dict:
    project = workspace.project
    settings = {"max_pages": 20000, "workers": 24, "follow_links": True,
                "include_query_urls": False, **project.get("crawl", {})}
    origin = project["site"].rstrip("/")
    host = urlsplit(origin).netloc.lower()
    conn = workspace.db()

    _log(f"building the proxy pool against {host}")
    pool = egress.build_pool(
        f"{origin}/robots.txt",
        accept=lambda status, body: status == 200 and bool(body.strip()) and "<html" not in body[:300].lower(),
    )
    try:
        robots = egress.fetch(pool, f"{origin}/robots.txt", blocked=_blocked)
        set_meta(conn, "robots_txt", robots.body if robots.status == 200 else "")
        set_meta(conn, "robots_status", robots.status)
        sitemap_urls, problems = _sitemaps(pool, origin, robots.body if robots.status == 200 else "")
        set_meta(conn, "sitemap_problems", problems)
        conn.execute("DELETE FROM sitemap_urls")
        conn.executemany("INSERT OR REPLACE INTO sitemap_urls(url, sitemap, lastmod) VALUES (?,?,?)",
                         [(url, src, lastmod) for url, (src, lastmod) in sitemap_urls.items()])
        _log(f"sitemaps declare {len(sitemap_urls)} URLs ({len(problems)} problems)")

        home = normalize_url(origin + "/")
        for url in [home, *sitemap_urls]:
            source = "sitemap" if url in sitemap_urls else "home"
            conn.execute("INSERT OR IGNORE INTO pages(url, source) VALUES (?, ?)", (url, source))
        gsc_only = 0
        for row in conn.execute(
                "SELECT url FROM gsc_page WHERE span='16m' ORDER BY impressions DESC LIMIT ?",
                (settings.get("max_gsc_urls", 5000),)).fetchall():
            if same_site(row["url"], host) and not ASSET.search(urlsplit(row["url"]).path):
                gsc_only += conn.execute(
                    "INSERT OR IGNORE INTO pages(url, source) VALUES (?, 'gsc')", (row["url"],)).rowcount
        _log(f"Search Console adds {gsc_only} URLs that no sitemap declares")
        if refresh:
            conn.execute("UPDATE pages SET fetched_at=''")
        conn.commit()

        known = {row["url"] for row in conn.execute("SELECT url FROM pages")}
        todo = [row["url"] for row in conn.execute(
            "SELECT url FROM pages WHERE fetched_at='' OR status=0 ORDER BY rowid")]
        if limit:
            todo = todo[:limit]
        cap = limit or settings["max_pages"]
        done = failed = 0
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=settings["workers"]) as executor:
            pending: set = set()
            while todo or pending:
                while todo and len(pending) < settings["workers"] * 2:
                    pending.add(executor.submit(_one, pool, todo.pop(0), host))
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    url, row, links = future.result()
                    _save(conn, url, row, links)
                    done += 1
                    failed += 0 if row["status"] else 1
                    if settings["follow_links"] and not limit:
                        for link in links:
                            target = link["dst"]
                            if target in known or len(known) >= cap:
                                continue
                            if _crawlable(target, host, settings["include_query_urls"]):
                                known.add(target)
                                conn.execute("INSERT OR IGNORE INTO pages(url, source) VALUES (?, 'link')", (target,))
                                todo.append(target)
                    if done % 100 == 0:
                        conn.commit()
                        rate = done / max(time.monotonic() - started, 1)
                        _log(f"{done} fetched, {failed} unanswered, {len(todo) + len(pending)} queued, "
                             f"{rate:.1f}/s, pool {len(pool)}")
        conn.commit()
        summary = {"fetched": done, "unanswered": failed, "known_urls": len(known),
                   "sitemap_urls": len(sitemap_urls), "pool": pool.stats(),
                   "minutes": round((time.monotonic() - started) / 60, 1)}
        set_meta(conn, "crawl_summary", summary)
        set_meta(conn, "crawled_at", dt.date.today().isoformat())
        return summary
    finally:
        pool.stop()
        conn.close()
