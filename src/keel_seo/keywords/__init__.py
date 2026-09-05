"""Keyword research from Google autocomplete alone.

One seed in, its whole query universe out: crawled by surrounding the term with
every character and word that opens a different window onto Google's suggestion
list, re-seeded from what comes back until the cycle closes, then clustered by
shared wording and ranked.

    python -m keel_seo.keywords quotex --out ./keywords

**A harvest never leaves from the machine running it.** Google's refusal is
IP-wide and outlasts the run, so a crawl asked from a laptop or a production host
costs that entire address its access to the endpoint — measured 2026-09-04, after
3,909 requests at the throttled default, on a server carrying six live sites.
Rotation is the only supported egress: ``--proxies auto`` is the default, asking
directly is refused, and both the single-seed CLI and the batch walker enforce it
before they create anything. The rule and its evidence live in
:mod:`keel_seo.keywords.proxying`.

The pieces, in the order the CLI uses them:

* :mod:`keel_seo.keywords.suggest` — the endpoint client, its cache, and the
  egress probe that gives a harvest its only real geography label.
* :mod:`keel_seo.keywords.proxying` — the egress rule, and the seam to
  keel-crawler's rotating pool that satisfies it.
* :mod:`keel_seo.keywords.markets` — which countries a harvest asks, and the
  language each one is asked in.
* :mod:`keel_seo.keywords.language` — which language a keyword is in, from its
  script, its vocabulary or its accents. No model.
* :mod:`keel_seo.keywords.grammar` — the attachments that surround a term.
* :mod:`keel_seo.keywords.crawl` — the breadth-first walk and the priority score.
* :mod:`keel_seo.keywords.cluster` — IDF-weighted lexical clustering and
  deterministic intent tagging.
* :mod:`keel_seo.keywords.report` — JSON, CSV and Markdown output.

No API key, and the analysis itself is standard library only. A real harvest
additionally needs the rotating pool, which is one install away:
``pip install 'keel-seo[proxies]'``.

Three things a caller does not have to remember. **A seed's spellings are one
seed**: ``fundingpips`` and ``funding pips`` are crawled together, land in one
file and cluster as one keyword. **A harvest asks the project's target markets**
— sixteen countries by default, each in the language it searches in — but crawls
in full only the ones that answer differently from the primary market, deciding
on a sixty-query sample rather than on a full crawl it cannot take back. And **every keyword carries its language**, so a
sixteen-country file separates rather than blurs.

The hard limit, stated once here and repeated in every output: **autocomplete
never returns search volume.** Everything this package produces describes the
*shape* of demand, not its size.
"""
from .cluster import Cluster, build as build_clusters
from .crawl import INCOMPLETE, Phrase, Universe, crawl
from .suggest import SuggestCache, SuggestClient, egress_identity

__all__ = [
    "Cluster",
    "INCOMPLETE",
    "Phrase",
    "SuggestCache",
    "SuggestClient",
    "Universe",
    "build_clusters",
    "crawl",
    "egress_identity",
]
