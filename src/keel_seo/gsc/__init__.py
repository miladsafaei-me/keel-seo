"""Google Search Console query intelligence — a headless, service-account
connector plus a persistent query registry.

Two generic, project-neutral pieces:

* :mod:`keel_seo.gsc.connector` — authenticate with a Google Cloud *service
  account* and pull Search Analytics rows (queries / pages / clicks / impressions
  / CTR / position) for a property. Run: ``python -m keel_seo.gsc.connector``.
* :mod:`keel_seo.gsc.registry` — maintain a durable JSON/CSV registry of every
  query a property has ever appeared for, refreshed by an incremental ``sync``.
  Run: ``python -m keel_seo.gsc.registry sync``.

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
vocabulary and taxonomy), so it stays in the consuming project — this package only
owns the neutral connector + registry.
"""
