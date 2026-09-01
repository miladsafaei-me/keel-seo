"""Keyword research from Google autocomplete alone.

One seed in, its whole query universe out: crawled by surrounding the term with
every character and word that opens a different window onto Google's suggestion
list, re-seeded from what comes back until the cycle closes, then clustered by
shared wording and ranked.

    python -m keel_seo.keywords quotex --out ./keywords

The pieces, in the order the CLI uses them:

* :mod:`keel_seo.keywords.suggest` — the endpoint client, its cache, and the
  egress probe that gives a harvest its only real geography label.
* :mod:`keel_seo.keywords.grammar` — the attachments that surround a term.
* :mod:`keel_seo.keywords.crawl` — the breadth-first walk and the priority score.
* :mod:`keel_seo.keywords.cluster` — IDF-weighted lexical clustering and
  deterministic intent tagging.
* :mod:`keel_seo.keywords.report` — JSON, CSV and Markdown output.

Standard library only, and no API key: it can run anywhere Python runs.

The hard limit, stated once here and repeated in every output: **autocomplete
never returns search volume.** Everything this package produces describes the
*shape* of demand, not its size.
"""
from .cluster import Cluster, build as build_clusters
from .crawl import Phrase, Universe, crawl
from .suggest import SuggestCache, SuggestClient, egress_identity

__all__ = [
    "Cluster",
    "Phrase",
    "SuggestCache",
    "SuggestClient",
    "Universe",
    "build_clusters",
    "crawl",
    "egress_identity",
]
