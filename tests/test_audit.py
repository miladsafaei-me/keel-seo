"""keel_seo.audit: parsing, families, robots rules and findings, with no network."""
from __future__ import annotations

import json
import tempfile
import unittest

from keel_seo.audit import analyze, egress, findings
from keel_seo.audit.parse import normalize_url, parse_page
from keel_seo.audit.store import Workspace

PAGE = """<html lang="en"><head><title>Acme review | Site</title>
<meta name="description" content="What Acme charges.">
<link rel="canonical" href="/en/brokers/acme">
<script type="application/ld+json">{"@type": "Article", "headline": "Acme", "author": {"name": "Jo Doe"}}</script>
</head><body><nav><a href="/en/brokers">Brokers</a></nav>
<main><h1>Acme review</h1><h3>Costs</h3><p>Acme charges a spread of two pips on the major pairs today.</p>
<a href="/en/brokers/other#fees">Other broker</a><a href="https://example.org/x">Out</a></main>
<footer><a href="/en/about">About</a></footer></body></html>"""


class ParseTests(unittest.TestCase):
    def test_fields_and_links(self):
        fields, links = parse_page("https://site.test/en/brokers/acme", PAGE, "site.test")
        self.assertEqual(fields["title"], "Acme review | Site")
        self.assertEqual(fields["canonical"], "https://site.test/en/brokers/acme")
        self.assertEqual(json.loads(fields["h1"]), ["Acme review"])
        self.assertEqual(fields["author"], "Jo Doe")
        self.assertIn("Article", json.loads(fields["schema_types"]))
        self.assertTrue(any("datePublished" in issue for issue in json.loads(fields["schema_issues"])))
        zones = {link["dst"]: (link["zone"], link["internal"]) for link in links}
        self.assertEqual(zones["https://site.test/en/brokers"], ("nav", 1))
        self.assertEqual(zones["https://site.test/en/brokers/other"], ("main", 1))
        self.assertEqual(zones["https://example.org/x"], ("main", 0))
        self.assertNotIn("About", fields["main_text"])

    def test_normalize_url(self):
        self.assertEqual(normalize_url("/a?b=1#c", "https://Site.test/x"), "https://site.test/a?b=1")
        self.assertEqual(normalize_url("mailto:a@b.c", "https://site.test/"), "")


class EgressTests(unittest.TestCase):
    def test_parse_skips_interim_responses(self):
        raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/2 301 \r\nLocation: /b\r\nX-Robots-Tag: noindex\r\n\r\n"
        status, headers, body = egress._parse(raw)
        self.assertEqual((status, headers["location"], headers["x-robots-tag"], body), (301, "/b", "noindex", ""))

    def test_truncated_html_is_not_complete(self):
        headers = {"content-type": "text/html"}
        self.assertFalse(egress._complete(200, headers, "<html><body>cut"))
        self.assertTrue(egress._complete(200, headers, "<html><body>ok</body></html>"))


class FamilyTests(unittest.TestCase):
    def test_slugs_collapse_and_repeating_suffix_stays(self):
        urls = [f"https://s.test/en/brokers/b{n}" for n in range(8)]
        urls += [f"https://s.test/en/brokers/b{n}/review" for n in range(8)]
        urls += ["https://s.test/en/about", "https://s.test/en/contact"]
        families = analyze.assign_families(urls, [])
        self.assertEqual(families["https://s.test/en/brokers/b3"], "/en/brokers/{}")
        self.assertEqual(families["https://s.test/en/brokers/b3/review"], "/en/brokers/{}/review")
        self.assertEqual(families["https://s.test/en/about"], "/en/(single pages)")

    def test_override_wins(self):
        families = analyze.assign_families(
            ["https://s.test/p/a", "https://s.test/p/b", "https://s.test/p/c"],
            [{"name": "posts", "pattern": r"/p/"}])
        self.assertEqual(set(families.values()), {"posts"})


class RobotsTests(unittest.TestCase):
    def test_longest_rule_wins(self):
        rules = analyze._robots_blocks("User-agent: *\nDisallow: /api/\nAllow: /api/public\nDisallow: /*?_rsc=\n")
        self.assertTrue(analyze._robots_disallowed(rules, "https://s.test/api/x"))
        self.assertFalse(analyze._robots_disallowed(rules, "https://s.test/api/public/x"))
        self.assertTrue(analyze._robots_disallowed(rules, "https://s.test/en/a?_rsc=1"))
        self.assertFalse(analyze._robots_disallowed(rules, "https://s.test/en/a"))


class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self.folder.name)
        conn = self.workspace.db()
        conn.execute(f"CREATE TABLE inventory ({', '.join(analyze.INVENTORY_COLUMNS)})")
        for url, clicks in (("https://s.test/a", 0), ("https://s.test/b", 9), ("https://s.test/c", 0)):
            conn.execute(
                "INSERT INTO inventory(url, family, status, indexable, clicks_3m, impr_3m, clicks_16m, inlinks_content) "
                "VALUES (?, '/x/{}', 200, 1, ?, ?, ?, 0)", (url, clicks, clicks * 10, clicks))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.folder.cleanup()

    def _write(self, module, rows):
        base = {"severity": "high", "confidence": "high", "effort": "S", "evidence": "e", "recommendation": "r"}
        payload = {"module": module, "summary": "s", "findings": [{**base, **row} for row in rows]}
        (self.workspace.findings / f"{module}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_removal_needs_url_change_and_a_real_target(self):
        self._write("inventory", [
            {"id": "inventory-01", "title": "t", "action": "delete", "targets": {"urls": ["https://s.test/a"]}},
            {"id": "inventory-02", "title": "t", "action": "merge", "url_change": True,
             "merge_into": "https://s.test/zzz", "targets": {"family": "/x/{}", "where": "clicks_3m = 0"}}])
        result = findings.validate(self.workspace, "inventory")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["findings"][1]["pages"], 2)

    def test_reconcile_flags_conflicts_and_risky_removals(self):
        self._write("inventory", [{"id": "inventory-01", "title": "t", "action": "delete", "url_change": True,
                                   "targets": {"urls": ["https://s.test/a", "https://s.test/b"]}}])
        self._write("intent", [{"id": "intent-01", "title": "t", "action": "improve",
                                "targets": {"urls": ["https://s.test/a"]}}])
        summary = findings.reconcile(self.workspace)
        self.assertEqual(summary["conflicts"]["count"], 1)
        self.assertEqual(summary["risky_removals"]["first"][0]["url"], "https://s.test/b")
        (self.workspace.findings / "verify.json").write_text(json.dumps({"checks": [
            {"finding_id": "inventory-01", "verdict": "refuted", "reason": "r"}]}), encoding="utf-8")
        summary = findings.reconcile(self.workspace)
        self.assertEqual(summary["url_actions"]["delete"], 0)
        self.assertEqual(summary["url_actions"]["improve"], 1)


if __name__ == "__main__":
    unittest.main()
