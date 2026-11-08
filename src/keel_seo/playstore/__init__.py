"""Google Play keyword research and gap finding, keyless.

The third surface next to :mod:`keel_seo.keywords` (what people type into
Google and YouTube) and :mod:`keel_seo.youtube` (what they watched). This one
answers a builder's question: for a topic, which phrases do people type into
Google Play, and for which of them are the apps that answer weak?

Google Play publishes no search volume, so nothing here pretends to have one.
Demand is read from two public signals instead: Play's own autocomplete offering
the phrase at all, and how many installs the apps it ranks for that phrase have
collected. Weakness is read from the same apps: low ratings, no update for a
year, titles that do not even name the need, and no strong incumbent.

Run it as ``python -m keel_seo.playstore --seed "prop firm" --out <dir>``.
It needs the ``playstore`` extra (``pip install 'keel-seo[playstore]'``), which
brings the google-play-scraper parser for search, details and reviews; the
autocomplete call is this package's own, because that library does not have one.
"""
from .client import PlayClient
from .expand import expand
from .score import KeywordScore, score_keyword

__all__ = ["PlayClient", "expand", "KeywordScore", "score_keyword"]
