# TODO

This file is the single source of truth for pending, follow-up, and deferred work on this project. See CLAUDE.md for the tracking rule.

Guidelines:
- Add a task here as soon as it's identified — with priority, prerequisites/dependencies, and enough context to pick it up cold.
- Group by priority: P0 (urgent / blocking / production risk), P1 (next up), P2 (backlog / nice-to-have).
- Note real dependencies explicitly ("Blocked by: ...", "Requires: ...").
- Delete a task from this file the moment it's done. This file only ever holds what's left.

## P1 — Next up

- [ ] **Indexing API is the only capability still blocked on all 5 properties** (measured
  2026-08-29 with `python -m keel_seo.gsc check`). Everything else — Search Analytics,
  URL Inspection, Sitemaps, Sites — passes today on revenika.com, martiland.com,
  binaryoption.trading, prop-firm.review and sarmayeh.media, all of which already grant
  `gsc-dashboard@milad-seo-tools.iam.gserviceaccount.com` (project `milad-seo-tools`,
  number 90801831831) at `siteFullUser`. Two console steps unblock indexing, both
  outside this repo: (a) enable `indexing.googleapis.com` on the Cloud project — once,
  covers all five; (b) raise the service account from Full to **Owner** on each
  property in Search Console. Re-run `keel_seo_gsc_check` per property afterwards; it
  should reach 8/8. Requires: nothing in code — the client is done and tested.
- [ ] Adopt v0.11.0 in the 5 consumers: bump the pin, run `migrate` (new
  `keel_seo.UrlInspection` + `keel_seo.IndexingSubmission` tables, migration 0003), and
  add `manage.py keel_seo_gsc_sitemaps --auto` to the deploy one-shot so each deploy
  re-submits the sitemap. `GSC_SITE` / `GSC_CREDENTIALS` are already wired in every
  consumer's `compose.prod.yaml`, so no new env work is needed. Consider a weekly
  `keel_seo_gsc_inspect --all-indexable --stale-days 30` timer per site once adopted.

## P2 — Backlog
- [ ] Wire the new content-freshness engine (`keel_seo.freshness`, v0.6.0) into consumer
  projects one at a time. Each needs: `KEEL_SEO["freshness_enabled"] = True`, a
  `python manage.py keel_seo_freshness` line added to the deploy one-shot, `{% load
  keel_seo_freshness %}{% last_updated %}` dropped into whatever byline/footer template
  should show it, and `freshness_schema(request.path)` merged into that page's existing
  JSON-LD builder. Verify `freshness_content_selector` (default `"main"`) actually
  matches each host's page template before enabling — a host whose main content isn't
  wrapped in `<main>` needs `#id`/`.class` set explicitly or the render pass raises.
  binaryoptiontrading is first (see its own TODO for the concrete rollout task); no
  other consumer scheduled yet.
- [ ] Wire the directory-grouped sitemap engine (`keel_seo.sitemap_directory.directory_sitemap_urls`) into the three consumers left out of the 2026-08-12 rollout: binaryoptiontrading, broker-best, prop-firm-review. None of the three currently have any sitemap infrastructure (no `django.contrib.sitemaps`, no `keel_cms_urls`, no `robots.txt`), so this is a real standup per consumer, not just a pin bump. Blocked by: the URL-creation confirm-first rule — a new `/sitemap.xml` + per-directory `/<segment>.xml` family is new URLs, so get explicit user confirmation before adding them on each consumer.
- [ ] Wire the new `/search-console` dashboard (`keel_seo.gsc.urls`, v0.4.0) into the 5 other consumer projects planned to reuse it. Each needs: `include("keel_seo.gsc.urls")` at its desired mount path, `KEEL_SEO["gsc_data_dir"]` pointed at wherever its own GSC exporter writes (`gsc_dashboard.json`/`gsc_insights.json`/`query_enrichment.json`/`windows/`), `gsc_base_template` set to its own admin chrome (must expose `title`/`extra_head`/`content`/`extra_js` blocks), and — only if that consumer has a content-ideation queue to deposit insights into — `gsc_queue_hook` + `gsc_queue_list_url_name` + `gsc_plan_edit_url_name`. A consumer without keel-web installed will also need its own `{% load %}` icon library (the shipped template loads `keel_seo_icons`, which re-exports keel-web's `icon` tag).
- [ ] The GSC dashboard template's `{% load keel_seo_icons %}` hard-depends on keel-web being installed for its `{% icon %}` tag set (silent fallback is an empty tag library, not a rendering fallback). Every current Keel consumer has keel-web installed for auth, so this hasn't bitten yet — flag it if a future consumer adopts the dashboard without keel-web.
- [ ] **P1 — binaryoptiontrading and prop-firm-review need Alpine.js wired into `admin_os/base.html`.** Confirmed 2026-08-20 while fixing Revenika's stuck "Loading Search Console data…" overlay: `keel_seo.gsc.urls` is mounted on both, `gsc_base_template = "admin_os/base.html"` on both, and neither template (nor anything it extends) loads Alpine.js anywhere — identical gap to the one that caused Revenika's hang (the entire `/admin-os/search-console` dashboard is Alpine-driven: `x-data`/`x-show`/`x-cloak`/`Alpine.store`). The v0.5.2 `[x-cloak]` CSS fix degrades this from "stuck visible forever" to "silently non-interactive" on these two, which is safer but still broken — the dashboard won't actually work (tabs, charts, dismiss buttons, the range picker) until Alpine loads. Fix: vendor `alpine.min.js` into each project's `admin_os/static/admin_os/vendor/alpine/` (copy from `~/www/keel-seo`'s revenika fix or from signalbots' own `client/vendor/alpine/alpine.min.js`, which already carries this working pattern) and add `<script defer src="...">` to `admin_os/base.html`, mirroring signalbots' `admin_os/base_admin.html` `{% block alpine_scripts %}` right before `{% block extra_js %}`. Host-local fix in each project, not a keel-seo change.
