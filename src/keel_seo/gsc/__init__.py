"""Google Search Console query intelligence — a headless, service-account
connector plus a persistent query registry, plus a dashboard UI that reads them.

Generic, project-neutral pieces:

* :mod:`keel_seo.gsc.connector` — authenticate with a Google Cloud *service
  account* and pull Search Analytics rows (queries / pages / clicks / impressions
  / CTR / position) for a property. Run: ``python -m keel_seo.gsc.connector``.
* :mod:`keel_seo.gsc.registry` — maintain a durable JSON/CSV registry of every
  query a property has ever appeared for, refreshed by an incremental ``sync``.
  Run: ``python -m keel_seo.gsc.registry sync``.
* :mod:`keel_seo.gsc.indexing` — the Google Indexing API client
  (``notify_url``/``notify_urls``/``url_status``).
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
