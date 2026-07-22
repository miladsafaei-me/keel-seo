"""Helpers a host admin can use to present the Landing registry.

``categorize_landing`` gives each row a structural label (Home / Company /
Listing / Sub-page) using the same section logic as the sitemap ordering, so an
admin table can group rows without the host reimplementing URL bucketing. A host
that wants project-specific buckets can ignore this and supply its own.
"""
from .sitemaps import section_key

_SECTION_LABELS = {0: "Home", 1: "Company", 2: "Listing", 3: "Sub-page"}


def categorize_landing(url: str, all_urls: set) -> str:
    return _SECTION_LABELS[section_key(url, all_urls)[0]]
