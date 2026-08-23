# keel-seo

Reusable SEO plumbing for Keel projects — a **Landing registry** that is the single
source of truth for the sitemap and the noindex-by-default gate, plus the critical-CSS
generation engine. Extracted from SignalBots and neutralized: every project-specific
piece is a config hook, not hardcoded.

Read [`PLATFORM.md`](https://github.com/miladsafaei-me/keel-kit) (in keel-kit) for the
platform model, and this repo's [`CLAUDE.md`](CLAUDE.md) for the contract.

## What it provides

- `keel_seo.models.Landing` — the page registry (`title`, `url`, `is_indexable`).
- `keel_seo.sitemaps.LandingSitemap` — sitemap of `is_indexable=True` rows, ordered by
  structural section, with an optional host `lastmod` hook for dynamic-freshness pages.
- `keel_seo.sitemap_directory.directory_sitemap_urls` — re-groups any flat set of source
  Sitemaps into **one sitemap per first URL path segment** (`/academy/... -> /academy.xml`)
  under a `/sitemap.xml` index, plus a `/static-sitemap.xml` for root-level pages that live
  in no routing directory (`/`, `/about`). Rebuilt from live DB state on every request.
- `keel_seo.context_processors.landing` — injects the row for `request.path` (cached).
- `keel_seo/templates/keel_seo/robots_meta.html` — the noindex-by-default `<meta>` gate.
- `keel_seo.signals` — per-path cache invalidation on `Landing` change.
- `keel_seo.admin_helpers.categorize_landing` — structural label for admin tables.
- `keel_seo/tools/gen_critical_css.js` — the penthouse-based critical-CSS engine.
- `keel_seo.freshness` (since v0.6.0) — the content-freshness engine: a real,
  non-fabricated `dateModified`/`lastmod` per URL, driven by hashing each page's
  rendered content rather than trusting `updated_at`. See below.
- `keel_seo.intent` (since v0.7.0) — the anti-cannibalization gate: a host-declared
  registry of query intents, one canonical owning URL each, enforced against the live
  Landing table by `manage.py keel_seo_intent_check --strict`. See below.

## Consume it (host wiring)

1. `pip install keel-seo` (or a git/editable install during development).
2. Add `keel_seo` to `INSTALLED_APPS`.
3. Add `keel_seo.context_processors.landing` to your `TEMPLATES` context processors.
4. In the `<head>` of your base template: `{% include "keel_seo/robots_meta.html" %}`.
5. Compose your source sitemaps: `SITEMAPS = {"landings": LandingSitemap, ...content sitemaps}`,
   then publish them directory-grouped in `urls.py`:
   `urlpatterns = [*directory_sitemap_urls(SITEMAPS), ...]` (from `keel_seo.sitemap_directory`).
   This serves `/sitemap.xml` (index), `/<segment>.xml` per routing directory, and
   `/static-sitemap.xml`. `django.contrib.sitemaps` must be in `INSTALLED_APPS` (its
   templates render the XML) and `Site.domain` must be the real domain.
6. Configure via `KEEL_SEO` (all optional — see `keel_seo/config.py`):
   - `landing_db_table` — set to an existing table (e.g. `"core_landing"`) to adopt an
     existing registry with only a metadata-level `AlterModelTable` migration.
   - `cache_ttl` — per-path lookup cache TTL (default 300s).
   - `lastmod_hook` — dotted path to `() -> {url: date}` for pages that genuinely change
     often (e.g. a signals mirror's latest date per market URL). Default: none.
   - `sitemap_min_section_urls` — how many indexable URLs a routing directory needs
     before the index links its own `/<segment>.xml`. Thinner directories keep their
     URLs (folded into `/static-sitemap.xml`) but get no section file. Default 2.

   Sources may overlap freely — a URL that several of them list is emitted once,
   carrying the newest `lastmod` any source reported for it.

## Config-contract / override seams (the rawification points)

- **`lastmod_hook`** — the host owns any dynamic-freshness logic (it stays out of the
  package; keel-seo is domain-neutral).
- **critical-CSS manifest** — `gen_critical_css.js` expects a per-project `PAGES`/`CHROME`
  manifest describing which URLs to process and the site chrome selectors. Supply it in
  the host; the sibling `extract_critical_css.js` (broker/site-specific) stays in the host.

## Content freshness (`keel_seo.freshness`)

A real `dateModified` / sitemap `lastmod` per URL, computed by **hashing each
page's rendered content** rather than trusting a model's `updated_at`. The
problem that solves: a deploy that re-imports the whole content corpus
(`import_blog_posts --overwrite`, a glossary importer) bumps every row's own
`updated_at` whether or not a single visible word changed — publishing that as
`dateModified` tells search engines the entire site was rewritten on every
deploy, which is worse than publishing nothing. Hashing the rendered HTML
instead means a content edit, a template edit, a data edit and a code edit are
all detected identically — anything that doesn't show up in what the reader
sees doesn't move the date, and anything that does, does.

Two-line host adoption:

```python
# settings.py
KEEL_SEO = {
    ...,
    "freshness_enabled": True,   # opt in -- the render pass has a real cost
}
```

```
python manage.py keel_seo_freshness   # add to your deploy script, every deploy
```

Then, in any template that should show its freshness:

```django
{% load keel_seo_freshness %}
{% last_updated %}
```

And in whatever builds a page's JSON-LD:

```python
from keel_seo.freshness import freshness_schema

schema = {**my_schema, **freshness_schema(request.path)}  # adds "dateModified" when known
```

What each piece does:

- `keel_seo.freshness.normalize_content(html, *, selector, strip_patterns)` —
  extracts the `selector` region (a bare tag name like `"main"`, or `"#id"` /
  `".class"`), strips volatile substrings (cache-busting `?v=` query strings,
  CSRF tokens, CSP nonces, any element carrying `data-keel-freshness`), and
  collapses whitespace. Pure function, stdlib regex only — no parser
  dependency, no DB access. Raises `ValueError` loudly if the selector region
  isn't found, rather than silently hashing the whole document.
- `keel_seo.freshness.record(url, html, *, now=None, dry_run=False)` —
  normalizes + hashes `html`, compares against the `Landing` row's stored
  `content_hash`, and updates `content_modified_at` **only on a real change**.
  Idempotent: calling it twice on unchanged output never moves the date a
  second time. Never touches `updated_at`. Raises `Landing.DoesNotExist` for
  an unregistered URL.
- `keel_seo.freshness.freshness_for(url)` — the resolved date, or `None`.
- `keel_seo.freshness.freshness_schema(url)` — `{"dateModified": "<iso utc>"}`,
  or `{}` when unknown, ready to merge into a host's own JSON-LD.
- `python manage.py keel_seo_freshness` — walks every `is_indexable=True`
  `Landing` row, renders each URL **in-process with the Django test client**
  (no network, no running server needed) and calls `record()`. Flags:
  `--dry-run` (report only, write nothing), `--url <path>` (one URL only),
  `--quiet` (summary line only). A no-op — always exits 0 — when
  `freshness_enabled` is False, so it's safe to add to a deploy script
  unconditionally, before the feature is even turned on. Exits non-zero only
  when a URL failed to render; content merely changing is never a failure.
- `{% load keel_seo_freshness %}{% last_updated %}` — the public last-updated
  line: a semantic `<time datetime="...">` (machine-readable UTC ISO-8601)
  with a human-readable body, always carrying `data-keel-freshness` so it's
  excluded from its own hash (otherwise the date could never converge).
  Renders nothing when no date is recorded yet. Override the markup with your
  own `keel_seo/freshness/last_updated.html` (Django's normal per-app template
  precedence) — restyle freely, keep the `data-keel-freshness` attribute and
  the `<time>` element.
- `LandingSitemap.lastmod` — falls back to `content_modified_at` when the
  host's `lastmod_hook` supplies no date for a URL, so a host gets accurate
  `<lastmod>` for free once freshness is enabled. The hook still wins where it
  answers (it's the documented override for hosts with a better date source).

Config (`KEEL_SEO`, all optional — see `keel_seo/config.py`):

- `freshness_enabled` — opt-in switch (default `False`): the management
  command renders every indexable URL, so this is only turned on once a host
  wants that render pass to happen.
- `freshness_content_selector` — the region to hash. Default `"main"`.
- `freshness_strip_patterns` — extra `(regex, replacement)` pairs applied on
  top of `keel_seo.freshness.DEFAULT_STRIP_PATTERNS` before hashing, for
  volatile substrings specific to a host's own templates. Default `None`
  (built-ins only).

## Intent registry (`keel_seo.intent`)

Two pages competing for one search need is an architecture defect that only becomes
visible once both are long enough to be plausible answers — and by then the fix is a
redirect, not an edit. The registry makes the ownership call explicit and checkable
before that point.

A host declares, per query need, the one URL allowed to answer it:

```python
# core/seo_intents.py
def intent_registry():
    return {
        "intents": [
            {
                "key": "contract.high-low@what-is",   # <entity>@<frame>
                "entity": "contract.high-low",
                "frame": "what-is",
                "owner": "/instruments/high-low",     # the one indexable answer
                "label": "What a high/low binary option is and how it settles",
                "defers": ["/tag/call-option"],       # live spokes; must be noindex
                "retired": ["/tag/high-low-contract"], # withdrawn; must be gone + 301
            },
        ],
        "entity_families": {"contract.turbo": ["contract.60-second"]},
    }
```

```python
KEEL_SEO = {"intent_registry_hook": "core.seo_intents.intent_registry"}
```

Then:

```bash
python manage.py keel_seo_intent_check --strict     # deploy + CI gate
python manage.py keel_seo_intent_check --coverage   # indexable URLs not yet declared
```

The invariants, each with its own code so each has its own fix: `key-shape`,
`duplicate-key`, `aliased-intent` (two keys resolving to one entity+frame through the
synonym net — the failure mode where the gate never sees a collision), `owner-missing`,
`owner-noindex`, `deferral-missing`, `deferral-indexable` (the headline case: a second
indexable page on an owned intent), `deferral-is-owner`, and `retired-still-present`
(a withdrawn URL whose Landing row a reseed brought back).

`registry.canonical_owner_for(url)` is the runtime half: what a deferring page renders
as its "the full treatment lives here" link. The vocabulary — what this site's entities
and frames are — belongs to the host; the invariants and the gate live here.

## GSC query intelligence (`keel_seo.gsc`, optional)

A headless Google Search Console connector plus a persistent query registry — the
generic half of a "what do we already rank for?" loop. Project-neutral: property,
credentials, and storage dir are all runtime blanks.

Install the extra (it pulls the Google client libs + openpyxl):

```
pip install 'keel-seo[gsc]'
```

Configure via env (Bucket-3 blanks):

- `GSC_SITE` — the property, e.g. `sc-domain:example.com` (required; no default).
- `GSC_CREDENTIALS` — service-account JSON key (default `~/.config/keel-seo/gsc-service-account.json`).
- `GSC_DATA_DIR` — where the registry is stored (default `~/.local/share/keel-seo/gsc`).

Use:

```
python -m keel_seo.gsc.connector sites          # connection test
python -m keel_seo.gsc.registry sync            # pull the full query universe, merge
python -m keel_seo.gsc.registry stats           # summary
python -m keel_seo.gsc.registry xlsx            # formatted export
```

The registry writes `registry.json` (the store), `registry.csv`, and dated
`snapshots/`. Turning those queries into *content ideas* — clustering against a
scope vocabulary, market tagging, taxonomy — is host business logic and stays in
the consuming project; this package owns only the neutral connector + registry.

### Google Indexing API (`keel_seo.gsc.indexing`)

Submits URL-update / URL-delete notifications so freshly-published pages get
crawled sooner than sitemap discovery alone. Shares `$GSC_CREDENTIALS` with the
connector, but needs a DIFFERENT permission level: the service account must be an
**Owner** of the property (Restricted/Full is not enough) and the "Web Search
Indexing API" must be enabled on the Cloud project.

```
python -m keel_seo.gsc.indexing publish <url>   # notify URL_UPDATED
python -m keel_seo.gsc.indexing remove  <url>   # notify URL_DELETED
python -m keel_seo.gsc.indexing status  <url>   # read last-notification metadata
```

Or from Python: `notify_url(url)`, `notify_urls(urls)` (per-URL errors captured,
never raised), `url_status(url)`.

### GSC dashboard UI (`keel_seo.gsc.dashboard` / `.views` / `.urls`)

A `/search-console` reporting + insights dashboard — keel-seo's first UI surface.
Renders a per-window snapshot a host's own offline exporter produced (deterministic,
no LLM), plus host-curated `gsc_insights.json` insight cards (dismissible, keyed by
a fingerprint of each insight's data so a look-alike for new data still shows), and
optionally computes any date range live via `keel_seo.gsc.connector` when
`GSC_LIVE=1`. What is intentionally NOT here: generating the dashboard JSON or the
insights themselves — that stays host tooling (SignalBots keeps `tools/gsc/*` for
this); the package only reads what the host produced.

Mount it at whatever path you want:

```python
# host urls.py
from django.urls import include, path

urlpatterns = [
    path("admin-os/search-console", include("keel_seo.gsc.urls")),
    ...
]
```

This exposes `<mount>`, `<mount>/dismiss`, `<mount>/restore`, `<mount>/queue`,
`<mount>/dedicated/exclude`, `<mount>/dedicated/queue`,
`<mount>/dedicated/cluster-exclude`, `<mount>/dedicated/cluster-queue` under the
`keel_seo_gsc` app namespace (`{% url 'keel_seo_gsc:search_console' %}`, etc.).
Every view is superuser-gated.

Configure via `KEEL_SEO` (all optional; see `keel_seo/config.py` for the full
docstring):

- `gsc_data_dir` — directory holding `gsc_dashboard.json` / `gsc_insights.json` /
  `query_enrichment.json` / `windows/*.json` (a host's exporter output). Default
  `<MEDIA_ROOT>/gsc/data`.
- `gsc_base_template` — the template the dashboard extends for its chrome (must
  define `title` / `extra_head` / `content` / `extra_js` blocks). Default is
  keel-seo's own bare fallback so the package renders standalone.
- `gsc_queue_hook` — dotted path to `(spec, *, source_type, source_ref) -> (obj,
  outcome)`, the host's content-ideation intake for the "Add to Plan" button on an
  insight card. No default — genuinely host business logic.
- `gsc_queue_list_url_name` / `gsc_plan_edit_url_name` — URL names (reverse()-able)
  for the host's clustering-queue list and content-plan edit views, used for
  "already queued" links.
- `gsc_forbidden_redirect` — URL name an authenticated-but-not-superuser visitor is
  sent to. Default raises Django's standard 403.

Keyword picks and whole-cluster queueing (`dedicated_queue` / `cluster_queue`) go
straight into `keel_content`'s clustering-queue accumulator (a peer Keel package,
soft-imported) — no host hook needed for those two.

## Status

v0.7.0. Consumed by SignalBots (its first host) since v0.1.4: the Landing table is
adopted via the state-only `0001`, the sitemap is composed, the noindex-by-default
gate is live, and the GSC query registry is in use. Since then: greenfield-capable
initial migrations (v0.2.0), the GSC Indexing API client (v0.2.1), the
directory-grouped sitemap engine consumed by signalbots/revenika/martiland
(v0.3.0), the `/search-console` dashboard UI — keel-seo's first views/urls/
templates surface, migrated wholesale from SignalBots' `admin_os` app (v0.4.0) —
GSC dashboard fixes (v0.4.1, v0.4.2), sitemap directory-index fixes (v0.5.0,
v0.5.1, v0.5.2), and the content-freshness engine — `keel_seo.freshness`, the
`keel_seo_freshness` command, the `{% last_updated %}` tag, and the
`LandingSitemap.lastmod` content-hash fallback (v0.6.0), and the intent registry —
`keel_seo.intent` plus the `keel_seo_intent_check` gate, first consumed by
binaryoption.trading to resolve glossary-versus-pillar cannibalization (v0.7.0).
