# TODO

This file is the single source of truth for pending, follow-up, and deferred work on this project. See CLAUDE.md for the tracking rule.

Guidelines:
- Add a task here as soon as it's identified — with priority, prerequisites/dependencies, and enough context to pick it up cold.
- Group by priority: P0 (urgent / blocking / production risk), P1 (next up), P2 (backlog / nice-to-have).
- Note real dependencies explicitly ("Blocked by: ...", "Requires: ...").
- Delete a task from this file the moment it's done. This file only ever holds what's left.

## P2 — Backlog
- [ ] Wire the directory-grouped sitemap engine (`keel_seo.sitemap_directory.directory_sitemap_urls`) into the three consumers left out of the 2026-08-12 rollout: binaryoptiontrading, broker-best, prop-firm-review. None of the three currently have any sitemap infrastructure (no `django.contrib.sitemaps`, no `keel_cms_urls`, no `robots.txt`), so this is a real standup per consumer, not just a pin bump. Blocked by: the URL-creation confirm-first rule — a new `/sitemap.xml` + per-directory `/<segment>.xml` family is new URLs, so get explicit user confirmation before adding them on each consumer.
- [ ] Wire the new `/search-console` dashboard (`keel_seo.gsc.urls`, v0.4.0) into the 5 other consumer projects planned to reuse it. Each needs: `include("keel_seo.gsc.urls")` at its desired mount path, `KEEL_SEO["gsc_data_dir"]` pointed at wherever its own GSC exporter writes (`gsc_dashboard.json`/`gsc_insights.json`/`query_enrichment.json`/`windows/`), `gsc_base_template` set to its own admin chrome (must expose `title`/`extra_head`/`content`/`extra_js` blocks), and — only if that consumer has a content-ideation queue to deposit insights into — `gsc_queue_hook` + `gsc_queue_list_url_name` + `gsc_plan_edit_url_name`. A consumer without keel-web installed will also need its own `{% load %}` icon library (the shipped template loads `keel_seo_icons`, which re-exports keel-web's `icon` tag).
- [ ] The GSC dashboard template's `{% load keel_seo_icons %}` hard-depends on keel-web being installed for its `{% icon %}` tag set (silent fallback is an empty tag library, not a rendering fallback). Every current Keel consumer has keel-web installed for auth, so this hasn't bitten yet — flag it if a future consumer adopts the dashboard without keel-web.
