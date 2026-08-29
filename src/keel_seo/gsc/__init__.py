"""Google Search Console query intelligence — a headless, service-account
connector plus a persistent query registry, plus a dashboard UI that reads them.

Generic, project-neutral pieces:

* :mod:`keel_seo.gsc.connector` — authenticate with a Google Cloud *service
  account* and pull Search Analytics rows (queries / pages / clicks / impressions
  / CTR / position) for a property. Run: ``python -m keel_seo.gsc.connector``.
* :mod:`keel_seo.gsc.registry` — maintain a durable JSON/CSV registry of every
  query a property has ever appeared for, refreshed by an incremental ``sync``.
  Run: ``python -m keel_seo.gsc.registry sync``.
* :mod:`keel_seo.gsc.auth` — the shared credential/transport layer every client
  below sits on: key resolution, per-scope service construction and caching, retry
  with backoff, and error messages that name the console to go fix.
* :mod:`keel_seo.gsc.analytics` — the complete Search Analytics surface the simple
  connector leaves out: dimension filters, search types (image/video/news/Discover),
  aggregation types, ``dataState``, and pagination past the 25,000-row response cap.
* :mod:`keel_seo.gsc.inspection` — the URL Inspection API: index status, coverage
  state, Google's chosen canonical, crawl time and fetch state, robots.txt state,
  discovery sitemaps and referring URLs, plus the mobile/rich-result/AMP verdicts.
  Quota-paced (2,000/day, 600/min per property).
* :mod:`keel_seo.gsc.indexing` — the Google Indexing API client
  (``notify_url``/``notify_urls``/``remove_url``/``url_status``), quota-paced against
  the per-Cloud-project 200/day limit, plus ``removal_guidance`` for the removal path
  the API deliberately cannot take on its own.
* :mod:`keel_seo.gsc.sitemaps` — the Sitemaps API: list, read, submit and delete.
* :mod:`keel_seo.gsc.sites` — the Sites API: list, get, add and delete properties,
  and the permission level this key holds on each.
* :mod:`keel_seo.gsc.check` — a preflight that proves each capability with the
  cheapest real call and names the fix for whatever fails. Start here when a 403
  appears: it distinguishes a disabled Cloud API from a missing property grant from
  a permission level that is too low.
* :mod:`keel_seo.gsc.pulse` — the recurring measurement engine: a trend span (90
  days by default) carrying week-over-week, rolling 7/28/30/span comparisons and a
  level-shift detector, and inside it one deep window with keyword cohorts, the CTR
  noise floor, the site's own position→CTR curve, page families, auto-discovered
  cannibalisation pairs and a diff against the previous run. Deterministic and model-free; the
  ``/seo-pulse`` skill interprets what it writes. Run:
  ``python -m keel_seo.gsc.pulse --days 28``.
* :mod:`keel_seo.gsc.build` / :mod:`keel_seo.gsc.live` / :mod:`keel_seo.gsc.dashboard`
  / :mod:`keel_seo.gsc.views` / :mod:`keel_seo.gsc.urls` — the ``/search-console``
  dashboard UI: pure transforms, the live-API path, context building + insight
  dismissals, the views, and the URL routes. Mount with
  ``include("keel_seo.gsc.urls")``; see ``KEEL_SEO`` in :mod:`keel_seo.config` for
  the ``gsc_*`` settings this needs.

Everything project-specific is a Bucket-3 blank supplied by the host at runtime:

* ``$GSC_SITE`` — the property, e.g. ``sc-domain:example.com`` (no default; a host
  must set it or pass ``--site``).
* ``$GSC_CREDENTIALS`` — path to the service-account JSON key (default
  ``~/.config/keel-seo/gsc-service-account.json``).
* ``$GSC_DATA_DIR`` — where the registry is stored (default
  ``~/.local/share/keel-seo/gsc``).

The Google client libraries are an optional extra (``pip install
'keel-seo[gsc]'``) so a host that does not use GSC carries none of that weight.

What is intentionally NOT here: any turning of the registry into *content ideas*.
Query→cluster→ideation is host business logic (it needs the host's scope
vocabulary and taxonomy), so it stays in the consuming project. The dashboard
*reads* whatever the host's own tooling produced (or pulls live GSC data), and
reaches back into host ideation only through the explicit ``gsc_queue_hook``
config seam — it never owns clustering or scope logic itself.
"""
