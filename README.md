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
- `keel_seo.keywords` (since v0.12.0) — market-side keyword research from Google
  autocomplete alone: crawls one seed's whole query universe, clusters it by shared
  wording and ranks it. Standard library only, no API key. Can run through
  keel-crawler's rotating proxy pool (`pip install 'keel-seo[proxies]'`) so one
  blocked IP cannot end the research. See below.

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
        # Sections where every indexable page must be declared (since v0.7.1).
        "guarded_prefixes": ["/blog/"],
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
indexable page on an owned intent), `deferral-is-owner`, `retired-still-present`
(a withdrawn URL whose Landing row a reseed brought back), and
`undeclared-in-guarded-section`.

`guarded_prefixes` is the preventive half. Point it at the section that keeps producing
competitors for pages that already exist — a blog, usually — and every indexable page
under it must be named somewhere in the registry. A new page there fails the check
until somebody states which need it answers, and stating that is what makes a
collision with an existing owner visible *before* the page is written rather than
after it ranks. Pages outside those prefixes are unaffected, and a noindex page inside
them needs no entry.

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

A Django host can set the first two as `KEEL_SEO["gsc_site"]` / `KEEL_SEO["gsc_credentials"]`
instead; the environment variables win over the settings, so a container overrides a
settings default without a rebuild. The property is never guessed — an unset one raises
rather than acting on the wrong site.

Google Cloud + Search Console setup (which APIs to enable, which permission level each
capability needs, and how one service account serves several properties) is in
[`docs/gsc-setup.md`](docs/gsc-setup.md).

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

### Recurring measurement (`keel_seo.gsc.pulse`)

Reads the **shape of a property's performance over a span** — 90 days by default — and
then, inside it, one deep before/after window. The trend is the frame on purpose: a
window pair answers "what changed", but only the trend says whether that change is a
step, a drift, or last week's weather, and the two often disagree (a site can be up on
the quarter and falling this month — `trend.directions_disagree` says so out loud).
Week-over-week and a month-sized pair are always computed, because those are what a
human actually reads. All of it avoids the traps that make a naive before/after wrong. Site totals come from the complete `date` dimension
(the query dimension withholds a different share in each window); position is reported
unweighted over the matched keyword set (the impression-weighted average *improves*
when a site loses keywords); every CTR cohort is gated at a minimum impression count
(CTR is unreadable on a handful of impressions).

```
python -m keel_seo.gsc.pulse                                   # 90-day trend, 28-day deep window
python -m keel_seo.gsc.pulse --trend-days 180                  # a longer trend, same window
python -m keel_seo.gsc.pulse --days 14 --end 2026-08-24        # a window straddling a dated event
python -m keel_seo.gsc.pulse --content-prefixes blog,news
```

What it writes about the trend: `trend` (direction from a 28-day trailing mean, net
change, best and worst week, the largest week-over-week break, a single-breakpoint
`level_shift`, and `recent_4_weeks` beside the span so the two directions can be
compared), `week_over_week` for every complete week, `rolling_comparisons` for 7 / the
window / the trend span, and `monthly_pair` on its own. Every span is a multiple of 7:
a 30-day comparison reads naturally and is quietly wrong, because each window would
count two weekdays three times. A span the property is
too young to fill is marked `partial_history`, and any comparison whose previous span
is mostly missing is marked `comparable: false` instead of being reported as a result —
a launch ramp otherwise reads as "+34,091%".

It writes `<end>-facts.json` and appends `history.json` under `--out-dir`, diffing the
previous run into `facts["vs_previous_run"]`. Every span resolves from the last
**finalised** day, never from today, and is recorded in `facts.meta`; the pull is cached, so two runs
on one cache are byte-identical. `--content-prefixes` names the directories whose
children are single articles (`blog`, `news`, a glossary) so the page-family rollup
groups them instead of listing every post.

It measures and writes JSON; it never interprets. The `/seo-pulse` skill in **keel-kit**
carries the reading rules, the finding→action playbook and the report it publishes.

### The full Search Console API surface (v0.11.0)

Everything Google exposes for a property is reachable from one command, one
service-account key and one set of Django management commands. Start with the
preflight — it proves each capability with a real call and names the fix for
whatever fails:

```
python -m keel_seo.gsc check --site sc-domain:example.com
python manage.py keel_seo_gsc_check        # same, with the project's settings loaded
```

```
python -m keel_seo.gsc <command>

  check      diagnose credentials, permissions and every API in one pass
  sites      list / get / add / delete properties
  inspect    URL Inspection API: index status, canonical, crawl, coverage
  index      Indexing API: URL_UPDATED / URL_DELETED notifications
  sitemaps   list / get / submit / delete sitemaps
  analytics  Search Analytics with filters, types, aggregation, pagination
  query      the simple Search Analytics query CLI
  registry   durable query registry (sync / stats / xlsx)
  pulse      recurring measurement engine (trend + deep window)
```

Every capability, the scope it uses and the property permission it needs:

| Capability | Module | Scope | Property permission |
|---|---|---|---|
| Search Analytics | `analytics`, `connector`, `pulse` | `webmasters.readonly` | Restricted |
| URL Inspection | `inspection` | `webmasters.readonly` | **Full** or Owner |
| Sitemaps (read) | `sitemaps` | `webmasters.readonly` | Restricted |
| Sitemaps (submit/delete) | `sitemaps` | `webmasters` | Full or Owner |
| Sites (list/get) | `sites` | `webmasters.readonly` | Restricted |
| Sites (add/delete) | `sites` | `webmasters` | Owner |
| Indexing notifications | `indexing` | `indexing` | **Owner** |

One service-account key covers all of it — only the scope differs per call — so
granting the account **Owner** on a property unlocks the whole table at once.

#### URL Inspection (`keel_seo.gsc.inspection`)

The API behind the URL Inspection panel: index status, `coverageState`, the canonical
Google actually chose, last crawl time and user agent, robots.txt and fetch state, the
sitemaps and referring URLs it was discovered through, plus the mobile-usability,
rich-result and AMP verdicts. Quota is **2,000 inspections/day and 600/minute per
property**, and `inspect_urls` paces itself against both.

```
python -m keel_seo.gsc inspect url https://example.com/pricing/
python -m keel_seo.gsc inspect urls urls.txt --json out.json

python manage.py keel_seo_gsc_inspect --url /pricing/
python manage.py keel_seo_gsc_inspect --all-indexable --limit 500
python manage.py keel_seo_gsc_inspect --all-indexable --stale-days 30
python manage.py keel_seo_gsc_inspect --sitemap https://example.com/sitemap.xml
python manage.py keel_seo_gsc_inspect --not-indexed      # re-check known gaps
```

The command stores one current-state row per (site, url) in `keel_seo.UrlInspection`
and reports the gap that matters: how many indexable pages Google does **not** have
indexed, how many it canonicalises elsewhere, and the coverage-state histogram behind
those counts. Because the store records `fetched_at`, `--stale-days` lets a nightly
run walk a large site's backlog in quota-sized bites — never-inspected URLs first,
then the oldest reading — instead of re-reading the same first 2,000 URLs forever.

#### Indexing API (`keel_seo.gsc.indexing`)

Notifies Google that a URL was updated or removed, so it is re-crawled sooner than
sitemap discovery alone would manage. Needs **Owner** permission and the "Web Search
Indexing API" enabled on the Cloud project. Quota is **200 publishes/day per Cloud
project** — not per property, so several sites on one key share the same budget, which
is why every submission is logged to `keel_seo.IndexingSubmission`.

```
python -m keel_seo.gsc index publish <url>            # URL_UPDATED
python -m keel_seo.gsc index remove  <url>            # URL_DELETED
python -m keel_seo.gsc index status  <url>            # last-notification metadata
python -m keel_seo.gsc index batch urls.txt --limit 200

python manage.py keel_seo_gsc_index --url /blog/new-post/
python manage.py keel_seo_gsc_index --changed-since 3      # freshness-driven
python manage.py keel_seo_gsc_index --all-indexable --dry-run
python manage.py keel_seo_gsc_index --url /old-page/ --remove
```

The command refuses to spend quota on a URL it already notified within
`--cooldown-hours` (default 24), and truncates to the daily cap rather than burning
into 429s.

**On removing a URL.** `URL_DELETED` is a notification, not a removal: Google drops the
page once a crawl confirms a 404/410 or a `noindex`, and never merely because we asked.
The Search Console **Removals** tool — the temporary ~6-month block — has no public API
and stays a browser action. `--removal-guidance` (and `indexing.removal_guidance(url)`)
prints the durable path instead of pretending otherwise, including the trap that ruins
most removals: blocking the URL in `robots.txt` prevents the re-crawl that would have
seen the 410, so the page lingers in the index indefinitely.

#### Sitemaps (`keel_seo.gsc.sitemaps`)

```
python -m keel_seo.gsc sitemaps list
python -m keel_seo.gsc sitemaps submit https://example.com/sitemap.xml

python manage.py keel_seo_gsc_sitemaps                     # list with counts
python manage.py keel_seo_gsc_sitemaps --auto              # submit <origin>/sitemap.xml
```

`--auto` is safe to run on every deploy: submission is idempotent and re-queues a
sitemap Google may not have re-downloaded since the last content release. The listing
shows submitted-vs-indexed counts per sitemap, which is the cheapest honest answer to
"did Google actually process what we shipped?".

#### Search Analytics, complete (`keel_seo.gsc.analytics`)

What `connector` and the dashboard's live path leave out: dimension filters, the
non-web search types, aggregation types, `dataState`, and pagination past the
25,000-row response cap.

```
python -m keel_seo.gsc analytics --dimensions query,page --days 28
python -m keel_seo.gsc analytics --filter "page contains /blog/" --dimensions query
python -m keel_seo.gsc analytics --type discover --dimensions page --days 90
python -m keel_seo.gsc analytics --dimensions query --all --csv all-queries.csv
```

Filters are written as `"<dimension> <operator> <value>"` (`equals`, `notEquals`,
`contains`, `notContains`, `includingRegex`, `excludingRegex`) and repeat with
`--filter`. From Python: `analytics.query(...)` for one call, `analytics.fetch_all(...)`
to walk every page.

#### Sites (`keel_seo.gsc.sites`)

```
python -m keel_seo.gsc sites list      # every property + this key's permission on it
python -m keel_seo.gsc sites add sc-domain:example.com
```

`add` registers a property; it does not **verify** it. Verification has no public API
and stays a one-time browser step per property.

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

## Keyword universe (`keel_seo.keywords`)

Search Console tells a host what it is **already shown for**. This tells it what
the market **asks**, including all the demand it is invisible to — the other half
of keyword work, and the half no amount of first-party data can reconstruct.

One source only: Google's autocomplete endpoint, keyless and free.

```bash
python -m keel_seo.keywords quotex --out ./keywords
python -m keel_seo.keywords "pip value calculator" --levels 2 --rate 5
```

Four files come out, all in priority order: `<seed>.json` (the full record, every
signal per phrase), `<seed>.csv` (flat), `<seed>.md` (clusters, intents and the
contamination check, for a person), and `<seed>.xlsx` — the working spreadsheet.
Its sheets are ordered the way they are used: **Run** first, carrying a short
summary of where the data came from and how complete it is, plus an explanation
of what every scored column means (the volume caveat lives inside the Priority
entry, where a reader is actually looking); then **Clusters**, the map, one row
per topic with **every row hyperlinked to its own keywords**; then **Keywords**,
the working sheet, frozen header and autofilter; then **Off-seed** for the
contamination check. The workbook needs
`pip install 'keel-seo[xlsx]'`; the other three are stdlib and always written.

### How one seed becomes a universe

Autocomplete answers at most 15 phrases per request, so asking a bare term returns
its fifteen most popular continuations and hides the rest. The crawler gets past
that by asking the same term many times with something different attached, then
re-seeding from what comes back.

**Surround the term.** Six families of attachment, each reaching phrases the
others cannot: a spaced suffix sweep (`quotex a` … `quotex z`, plus digits), a
tight suffix sweep with no space (`quotexa` → `quotexapk`), a spaced *prefix*
sweep (`a quotex` → `is quotex a broker`), real words on both sides (`is quotex`
→ `is quotex legal in india`; `quotex vs` → `quotex vs pocket option`), a **`*`
walked through every gap between words** (`quotex * signal bot`), and every
seed-tier query asked again with a **leading space**, which this endpoint treats
as a different query. 378 queries for the seed, 57 for each phrase discovered
after it.

The last two were added on measurement rather than intuition. Against a finished
2,869-phrase `quotex` universe, the gap-walk returned **1,346 phrases nothing
else had found** (1.5 per query, against a 0.1 bar for keeping a family) and the
leading space 54 more (0.29 per query) — together a **48% enlargement**. The
gap-walk matters most because it is the only family that reaches phrases the term
does *not* lead: `best indicator for quotex trading`, `affiliate center quotex`.
A **trailing** space was tested in the same pass and rejected: Google trims it, so
it returns byte-identical results and would have doubled the request count for
nothing.

**Drill only where evidence says there is more.** A response is capped at the
client's capacity, so a *full* response means Google truncated it and something
is hidden behind it; that query is re-asked with each letter appended. A short
response is left alone — that corner of the space is already fully reported.

**Let it stop on its own.** A run ends by converging — a level that discovers
nothing new — or by exhausting `--levels`. `--budget` is a runaway guard set well
above the structural ceiling (~73,000 queries at the default `--levels 2
--frontier 300`), not a plan. It was 12,000 until 2026-09-02, which meant every
large seed was silently cut at about a sixth of its own space; if a report says
*"stopped with N phrases left unexpanded"*, that is what happened.

Throughput, not budget, is the real limit: it is roughly `live proxies x 200/hour`,
because each address is deliberately capped at 200 requests an hour. The way to go
faster is more addresses (`--proxy-want`), never a looser per-address limit — that
limit is what keeps the addresses alive.

**Re-seed and repeat.** Every returned phrase that contains the seed becomes the
next round's seed. Each level discovers less than the last, and when a level
discovers nothing the universe is closed — the report says so explicitly rather
than leaving the reader to assume it.

**Stay on topic.** Only phrases containing the seed are kept and re-seeded. This
is both the definition of the universe and the thing that stops the crawl
wandering: `how to quotex` returns `how to quote on reddit`, and re-seeding that
walks the search into an unrelated industry. Rejected phrases are counted, not
discarded — a term whose neighbours belong to another market is contamination
worth seeing, and the report has a section for it.

### Clustering

Phrases are grouped by how much wording they genuinely share. The seed's own
tokens are removed first, since every phrase has them and they say nothing about
which phrases belong together. Shared words are then weighted by rarity, because
two phrases sharing `app` share almost nothing while two sharing `zigzag` are
about the same thing. Clusters grow by average linkage, which is what stops a
long tail chaining into one blob.

Each cluster is labelled by the rare words most of its members agree on and
tagged with a deterministic intent — `navigational`, `informational`,
`commercial`, `transactional`, or `brand` for the seed plus a bare noun. No model
is involved anywhere in this package.

### What the priority score is worth, measured

The score was checked against real numbers rather than left as an assertion. A
Semrush export of 1,747 `quotex` keywords carrying US search volume was joined to
an autocomplete harvest of the same seed; 379 phrases appear in both.

| Signal | Spearman correlation with real search volume |
|---|---|
| **`priority` (the shipped composite)** | **+0.42** |
| `reach` — how many independent expansions surfaced the phrase | +0.44 |
| `relevance` — Google's own suggestion score | +0.18 |
| `best_rank` — position in the suggestion list | +0.15 |

Two things follow. **Reach is the signal that matters**, by a wide margin, and
the weights are set accordingly — an earlier version led with rank and scored
only +0.27. And a moderate positive correlation is exactly what it looks like:
enough to sort a harvest into "worth a page" and "tail", not enough to choose
between two close candidates. For that, use volume from §4's Keyword Planner.

Coverage from the same comparison, for a harvest that had not yet closed: it
reached **71% of the total search volume** Semrush lists for the seed, and 48 of
its 100 highest-volume keywords, while surfacing 2,275 phrases the export does
not contain at all.

### Two things to know before trusting the output

**It cannot measure volume.** Autocomplete returns none, and no parameter exists
that would make it. The `priority` score ranks demand *shape* — Google's own
ordering, how many independent expansions surfaced a phrase, its relevance score,
its depth — and every output file says so in its own header. Do not present it as
a volume estimate.

**Its geography is the egress IP.** `gl=` does nothing: `gl=us`, `gl=in` and
`gl=br` return byte-identical results, while the same query from two different
egress IPs does not. Every run therefore probes its own egress and stamps the
country it actually left from into the output, rather than one the caller asked
for, and the cache is namespaced by it so two markets can never be blended into
one harvest that claims to be a single one. To harvest a specific market, route
the run through an exit in it.

### Rate limits

The endpoint blocks, and it gives no warning: an unthrottled run measured 57
queries/second and was answered `HTTP 403` for everything after about 5,000
requests. Two things about that block matter more than its existence. It lasted
**over 75 minutes**, so it is not something a backoff waits out. And it is
**IP-wide, not query-scoped** — once tripped, `weather` and `pizza recipe` were
refused exactly like the harvested seed, so the whole machine loses the endpoint.
There is no `Retry-After` to read.

Whether the trigger is the rate or the cumulative count is unknown; the single
observation is ~5,000 requests at 57 q/s. Until those are separated, **do not
assume throttling alone prevents it** — keep one run well under a few thousand
network calls and let the cache carry a large universe across several sessions.
So the client throttles to 6 q/s by default, treats
403/429/503 as a distinct condition rather than a generic error, and trips a
circuit breaker that ends the crawl cleanly — keeping everything collected so far
and saying in the report that the harvest is incomplete. Every response is cached
on disk by (egress country, client, language, vertical, query), so a re-run after
a block repeats none of the work already done — and a run from a new country
starts its own namespace rather than quietly inheriting another market's answers.

### Rotating proxies (`--proxies auto`)

A single exit address is one mistake away from losing the source for a day, and
the measured block outlasted **sixteen hours**. `--proxies auto` removes that
single point of failure by rotating the crawl across many addresses.

```bash
pip install 'keel-seo[proxies]'
python -m keel_seo.keywords quotex --proxies auto --proxy-want 60
```

**The rotation itself is not this package's.** It lives in **keel-crawler**
(`keel_crawler.proxy.pool`), which owns harvesting from sixteen published lists,
the durable self-pruning store, per-target block memory, and the per-address
budgets that keep each proxy from being blocked in turn — see that package's
README for the ageing rules and the maintenance CLI
(`python -m keel_crawler.proxy`). Any Keel project needing proxy rotation should
use it rather than growing its own; this was keel-seo's own mistake first, and
the code moved out in v0.15.0.

The crawl starts as soon as ten proxies answer (`--proxy-start-at`) and the pool
keeps filling in the background, so a run begins in seconds instead of waiting out
a full verification pass.

What stays here is the one endpoint-specific piece: what counts as a real
autocomplete answer (`keel_seo/keywords/proxying.py`). A proxy returning a
captive-portal page also answers 200, and without that check it would be admitted
to the pool and then fail every real request. The import is soft — a host that
only wants the Landing registry does not install a crawler, and `--proxies` says
what to install rather than failing obscurely.

Two consequences for the output. **The harvest is multi-country by construction**,
and the report says so instead of naming a country: autocomplete answers by IP,
so a rotating pool returns some phrases local to whichever exit surfaced them,
and the exact mix is not reproducible. Read a pooled harvest as *what the term is
asked, broadly*; use a direct run, or an egress pinned to one country, when a
single market is the question. And a 403 through the pool is not a rate limit but
one spent address — evicted immediately, no backoff — so the circuit breaker
trips only when the pool empties.

## Status

v0.7.1. Consumed by SignalBots (its first host) since v0.1.4: the Landing table is
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
binaryoption.trading to resolve glossary-versus-pillar cannibalization (v0.7.0), plus
`guarded_prefixes`, which requires every indexable page in a named section to declare
the need it answers so a competitor cannot be written unnoticed (v0.7.1). v0.12.0 adds
`keel_seo.keywords`, the market-side counterpart to the GSC client: autocomplete-only
keyword-universe crawling, lexical clustering and priority ranking, with no key, no
dependency and no model.
