# TODO

This file is the single source of truth for pending, follow-up, and deferred work on this project. See CLAUDE.md for the tracking rule.

Guidelines:
- Add a task here as soon as it's identified — with priority, prerequisites/dependencies, and enough context to pick it up cold.
- Group by priority: P0 (urgent / blocking / production risk), P1 (next up), P2 (backlog / nice-to-have).
- Note real dependencies explicitly ("Blocked by: ...", "Requires: ...").
- Delete a task from this file the moment it's done. This file only ever holds what's left.

## P1 — Next up
- [ ] README `## Status` section is stale: it still says "v0.1.4 — extracted, neutralized, and consumed by SignalBots (its first host)". pyproject.toml is at v0.3.0 and three features have shipped since: greenfield-capable initial migrations (v0.2.0), the GSC Indexing API client (v0.2.1), and the directory-grouped sitemap engine (v0.3.0, `sitemap_directory.py`). Rewrite Status to reflect current version + current consumers (signalbots, revenika, martiland are on the directory-grouped sitemap engine per the 2026-08-12 rollout).
- [ ] README's "GSC query intelligence" section documents only the connector + query registry (`keel_seo.gsc.connector`, `keel_seo.gsc.registry`). It does not mention `keel_seo.gsc.indexing` (the Google Web Search Indexing API client — `notify_url`/`notify_urls`/`url_status`, plus its CLI) shipped in v0.2.1. Add a short usage subsection so the indexing client isn't undiscoverable.

## P2 — Backlog
- [ ] Wire the directory-grouped sitemap engine (`keel_seo.sitemap_directory.directory_sitemap_urls`) into the three consumers left out of the 2026-08-12 rollout: binaryoptiontrading, broker-best, prop-firm-review. None of the three currently have any sitemap infrastructure (no `django.contrib.sitemaps`, no `keel_cms_urls`, no `robots.txt`), so this is a real standup per consumer, not just a pin bump. Blocked by: the URL-creation confirm-first rule — a new `/sitemap.xml` + per-directory `/<segment>.xml` family is new URLs, so get explicit user confirmation before adding them on each consumer.
