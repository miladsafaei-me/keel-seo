"""The workspace and its SQLite database.

One audit run owns one folder. Raw data (page text, query strings) stays in
``data/`` and is never committed: query strings are keyword data.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    source TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    status INTEGER DEFAULT 0,
    final_url TEXT DEFAULT '',
    redirect_chain TEXT DEFAULT '[]',
    content_type TEXT DEFAULT '',
    bytes INTEGER DEFAULT 0,
    fetched_at TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    x_robots TEXT DEFAULT '',
    canonical TEXT DEFAULT '',
    robots_meta TEXT DEFAULT '',
    title TEXT DEFAULT '',
    meta_desc TEXT DEFAULT '',
    h1 TEXT DEFAULT '[]',
    outline TEXT DEFAULT '[]',
    word_count INTEGER DEFAULT 0,
    text_hash TEXT DEFAULT '',
    lang TEXT DEFAULT '',
    hreflang TEXT DEFAULT '[]',
    og TEXT DEFAULT '{}',
    schema_types TEXT DEFAULT '[]',
    schema_issues TEXT DEFAULT '[]',
    author TEXT DEFAULT '',
    published TEXT DEFAULT '',
    modified TEXT DEFAULT '',
    img_count INTEGER DEFAULT 0,
    img_noalt INTEGER DEFAULT 0,
    tables INTEGER DEFAULT 0,
    lists INTEGER DEFAULT 0,
    first_para_words INTEGER DEFAULT 0,
    main_text TEXT DEFAULT '',
    family TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS links (
    src TEXT, dst TEXT, anchor TEXT, rel TEXT, zone TEXT, internal INTEGER
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE TABLE IF NOT EXISTS sitemap_urls (url TEXT PRIMARY KEY, sitemap TEXT, lastmod TEXT);
CREATE TABLE IF NOT EXISTS gsc_page (
    url TEXT, span TEXT, clicks REAL, impressions REAL, position REAL,
    PRIMARY KEY (url, span)
);
CREATE TABLE IF NOT EXISTS gsc_query_page (
    query TEXT, url TEXT, span TEXT, clicks REAL, impressions REAL, position REAL
);
CREATE INDEX IF NOT EXISTS gqp_url ON gsc_query_page(url);
CREATE INDEX IF NOT EXISTS gqp_query ON gsc_query_page(query);
CREATE TABLE IF NOT EXISTS gsc_month (
    month TEXT, url TEXT, clicks REAL, impressions REAL, PRIMARY KEY (month, url)
);
CREATE TABLE IF NOT EXISTS gsc_day (
    day TEXT PRIMARY KEY, clicks REAL, impressions REAL, position REAL
);
CREATE TABLE IF NOT EXISTS gsc_dim (
    dim TEXT, key TEXT, span TEXT, clicks REAL, impressions REAL, position REAL,
    PRIMARY KEY (dim, key, span)
);
CREATE TABLE IF NOT EXISTS psi (
    url TEXT PRIMARY KEY, family TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

STARTER_PROJECT = {
    "name": "",
    "site": "https://example.com",
    "gsc_property": "sc-domain:example.com",
    "gsc_credentials": "",
    "language": "en",
    "business": {"goal": "", "money_pages": [], "ymyl": False},
    "brand_terms": [],
    "competitors": [],
    "crawl": {"max_pages": 20000, "workers": 24, "follow_links": True,
              "include_query_urls": False},
    "families": [],
    "notes": "",
}


class Workspace:
    """Paths of one audit run, plus its project card and database handle."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.data = self.root / "data"
        self.facts = self.root / "facts"
        self.findings = self.root / "findings"
        self.report = self.root / "report"
        self.project_path = self.root / "project.json"

    def ensure(self) -> None:
        for folder in (self.data, self.facts, self.findings, self.report):
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def project(self) -> dict:
        if not self.project_path.exists():
            raise SystemExit(f"no project.json in {self.root}. Run: python -m keel_seo.audit init {self.root}")
        return json.loads(self.project_path.read_text(encoding="utf-8"))

    def db(self) -> sqlite3.Connection:
        self.ensure()
        conn = sqlite3.connect(self.data / "audit.sqlite", timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return conn

    def write_fact(self, name: str, payload: dict) -> Path:
        self.ensure()
        path = self.facts / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default
