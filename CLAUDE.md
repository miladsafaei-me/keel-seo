# keel-seo — package guide

Part of the **Keel** platform (see keel-kit `PLATFORM.md`). This is a Bucket-2
reusable Django app: the Landing registry + sitemap + noindex gate + critical-CSS
engine. English only; no banner comments; CSS variables only in any styling.

## Task tracking

Remaining and follow-up work for this project is tracked in [TODO.md](TODO.md), not in chat memory. Every pending task — priority, prerequisites/dependencies, enough context to resume cold — goes there before starting new work; remove a task from TODO.md the moment it's done.

## Boundaries — what is here vs what stays in the host

- **Here (generic):** the `Landing` model, `LandingSitemap`, the directory-grouped
  sitemap engine (`sitemap_directory.py`: `/sitemap.xml` index + one `/<segment>.xml`
  per routing directory + `/static-sitemap.xml`), the `landing` context processor, the
  robots-meta partial, cache-invalidation signals, `categorize_landing`, the
  `gen_critical_css.js` engine, and `keel_seo.gsc` — the connector, query registry,
  Indexing API client, and (since v0.4.0) the `/search-console` dashboard UI
  (`gsc/{build,live,dashboard,views,urls}.py` + `templates/keel_seo/gsc/` +
  `static/keel_seo/gsc/`): pure data transforms, the live-API path, context
  building/insight dismissals, and the routes. Mount with
  `include("keel_seo.gsc.urls")`.
- **Stays in the host (Bucket-0):** any dynamic-freshness computation (wired via
  `KEEL_SEO["lastmod_hook"]`), the content sitemaps (blog/news/archives — those live in
  the content package), `extract_critical_css.js`, the per-project critical-CSS
  `PAGES`/`CHROME` manifest, generating the GSC dashboard/insights JSON (a host's own
  offline exporter tooling — e.g. SignalBots' `tools/gsc/*`), and any query→cluster→
  ideation logic (reached from the dashboard only through the explicit
  `KEEL_SEO["gsc_queue_hook"]` seam).

## Editing rule (drift prevention)

When a consuming project has this installed, its copy of these files is **not** editable
in that project — change them **here**, bump the version, and let the project pull the
new version. Project-specific behavior belongs in `KEEL_SEO` config hooks, never in a
fork of this code.

## Override hooks (config-contract)

| Hook | Where | Default | Host provides |
|---|---|---|---|
| `KEEL_SEO["lastmod_hook"]` | `config.landing_lastmod_map` | none → no `<lastmod>` | `() -> {url: date}` |
| `KEEL_SEO["landing_db_table"]` | `models.Landing.Meta` | `keel_seo_landing` | existing table name to adopt |
| `KEEL_SEO["cache_ttl"]` | `context_processors.landing` | 300 | seconds |
| critical-CSS `PAGES`/`CHROME` | `tools/gen_critical_css.js` | — | per-project manifest |
| `KEEL_SEO["gsc_data_dir"]` | `config.gsc_data_dir` | `<MEDIA_ROOT>/gsc/data` | dir holding the exporter's JSON |
| `KEEL_SEO["gsc_base_template"]` | `gsc/views.py` context | `keel_seo/gsc/_default_base.html` | template name (title/extra_head/content/extra_js blocks) |
| `KEEL_SEO["gsc_queue_hook"]` | `gsc/views.queue` | none → button errors | `(spec, *, source_type, source_ref) -> (obj, outcome)` |
| `KEEL_SEO["gsc_queue_list_url_name"]` | `config.gsc_queue_list_url` | none → no link | reverse()-able URL name, no args |
| `KEEL_SEO["gsc_plan_edit_url_name"]` | `gsc/views.queue` | none → no link | reverse()-able URL name, `args=[pk]` |
| `KEEL_SEO["gsc_forbidden_redirect"]` | `gsc/views._forbidden` | none → Django 403 | reverse()-able URL name |

## Adoption note (host migration)

Setting `landing_db_table` to an existing table lets a host adopt keel-seo with only a
Django state migration (`AlterModelTable` / `RenameModel`) — no row copy. Sequence any
model-move behind the host's canary deploy; verify `sitemap.xml` builds and a known
noindex page still emits `noindex,nofollow` before cutover.
