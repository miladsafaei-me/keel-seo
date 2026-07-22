# keel-seo — package guide

Part of the **Keel** platform (see keel-kit `PLATFORM.md`). This is a Bucket-2
reusable Django app: the Landing registry + sitemap + noindex gate + critical-CSS
engine. English only; no banner comments; CSS variables only in any styling.

## Boundaries — what is here vs what stays in the host

- **Here (generic):** the `Landing` model, `LandingSitemap`, the `landing` context
  processor, the robots-meta partial, cache-invalidation signals, `categorize_landing`,
  the `gen_critical_css.js` engine.
- **Stays in the host (Bucket-0):** any dynamic-freshness computation (wired via
  `KEEL_SEO["lastmod_hook"]`), the content sitemaps (blog/news/archives — those live in
  the content package), `extract_critical_css.js`, and the per-project critical-CSS
  `PAGES`/`CHROME` manifest.

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

## Adoption note (host migration)

Setting `landing_db_table` to an existing table lets a host adopt keel-seo with only a
Django state migration (`AlterModelTable` / `RenameModel`) — no row copy. Sequence any
model-move behind the host's canary deploy; verify `sitemap.xml` builds and a known
noindex page still emits `noindex,nofollow` before cutover.
