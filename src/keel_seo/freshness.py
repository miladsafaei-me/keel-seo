"""Content-freshness engine: a real, non-fabricated ``dateModified`` per URL.

The problem an ``auto_now`` timestamp cannot solve: a deploy that re-imports the
whole content corpus (e.g. ``import_blog_posts --overwrite``, a glossary
importer) bumps every row's own ``updated_at`` whether or not a single visible
word changed. Publishing that as ``dateModified`` tells search engines the
entire site was rewritten on every deploy -- worse than publishing nothing.

The fix: hash the page's **rendered content**, not its database row. Render the
URL, extract the main-content region, strip everything that varies without the
content varying (cache-busting query strings, CSRF tokens, CSP nonces, the
freshness line itself), and compare the hash against what is stored on
``Landing.content_hash``. Different hash -> the reader-visible page genuinely
changed -> stamp ``content_modified_at`` with now. Same hash -> leave the
stored date alone, however many times the row was re-saved underneath it. This
one mechanism covers content edits, template edits, data edits and code edits
identically, because all of them show up in the rendered output and nothing
else does.

Public entry points:

- ``normalize_content(html, selector=..., strip_patterns=...)`` -- the pure
  hashing input: extract + strip + collapse whitespace. No Django/DB access.
- ``record(url, html, now=None, dry_run=False)`` -- normalize, hash, compare
  against the ``Landing`` row for ``url``, update ``content_modified_at`` only
  on a real change. Idempotent: calling it twice on unchanged output never
  moves the date. Raises ``Landing.DoesNotExist`` if the URL isn't registered.
- ``freshness_for(url)`` -- the resolved public date for a URL, or ``None``.
- ``freshness_schema(url)`` -- ``{"dateModified": "<iso utc>"}`` for a host to
  merge into its own JSON-LD, or ``{}`` when unknown.
- ``humanize(dt)`` / ``isoformat_utc(dt)`` -- the two renderings the
  ``{% last_updated %}`` template tag uses; exported so a host that builds its
  own markup instead of overriding the template can reuse them.

Driven by the ``keel_seo_freshness`` management command (walks every indexable
``Landing`` row, renders each in-process, calls ``record``) and by
``keel_seo.templatetags.keel_seo_freshness.last_updated`` / the
``LandingSitemap.lastmod`` fallback in ``sitemaps.py``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from django.utils import timezone

from .config import seo_setting
from .models import Landing

# Regex fragments used to walk balanced HTML elements without a parser
# dependency. `_TAG_BOUNDARY` stops a tag-name match at "main-nav" not
# matching selector "main": the char right after the name must be
# whitespace, "/" or ">", never a bare word character.
_TAG_BOUNDARY = r"(?=[\s/>])"


def _open_tag_re(tag: str) -> re.Pattern:
    return re.compile(r"<" + re.escape(tag) + _TAG_BOUNDARY + r"[^>]*?(/?)>", re.IGNORECASE)


def _close_tag_re(tag: str) -> re.Pattern:
    return re.compile(r"</" + re.escape(tag) + r"\s*>", re.IGNORECASE)


def _find_matching_close(html: str, pos: int, tag: str) -> int:
    """Return the index just past the ``</tag>`` that closes the element whose
    open tag ended at ``pos``, counting nested same-tag opens/closes so a
    selector like "main" isn't fooled by a nested ``<main>`` (invalid HTML,
    but defensive). Raises ValueError on an unbalanced document rather than
    silently returning a wrong span."""
    open_re = _open_tag_re(tag)
    close_re = _close_tag_re(tag)
    depth = 1
    idx = pos
    while depth > 0:
        close_match = close_re.search(html, idx)
        if close_match is None:
            raise ValueError(f"unbalanced <{tag}> element in rendered HTML")
        open_match = open_re.search(html, idx, close_match.start())
        if open_match is not None and not open_match.group(1):
            depth += 1
            idx = open_match.end()
        else:
            depth -= 1
            idx = close_match.end()
    return idx


def _extract_by_tag(html: str, tag: str) -> str:
    match = _open_tag_re(tag).search(html)
    if not match:
        raise ValueError(
            f"freshness content selector <{tag}> not found in rendered HTML"
        )
    if match.group(1):  # self-closing <tag ... />
        return html[match.start():match.end()]
    end = _find_matching_close(html, match.end(), tag)
    return html[match.start():end]


def _extract_by_attr(html: str, attr_pattern: str, missing_msg: str) -> str:
    match = re.search(
        r"<([a-zA-Z][\w-]*)\b[^>]*?" + attr_pattern + r"[^>]*?(/?)>",
        html,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(missing_msg)
    tag = match.group(1)
    if match.group(2):
        return html[match.start():match.end()]
    end = _find_matching_close(html, match.end(), tag)
    return html[match.start():end]


def _extract_region(html: str, selector: str) -> str:
    """Extract the element matched by ``selector`` -- a bare tag name
    ("main"), "#some-id", or ".some-class". Raises ValueError if no element
    matches, rather than silently hashing the whole document."""
    selector = (selector or "main").strip()
    if selector.startswith("#"):
        value = selector[1:]
        return _extract_by_attr(
            html,
            r'id=["\']' + re.escape(value) + r'["\']',
            f"freshness content selector '#{value}' not found in rendered HTML",
        )
    if selector.startswith("."):
        value = selector[1:]
        return _extract_by_attr(
            html,
            r'class=["\'][^"\']*\b' + re.escape(value) + r'\b[^"\']*["\']',
            f"freshness content selector '.{value}' not found in rendered HTML",
        )
    return _extract_by_tag(html, selector)


def _strip_elements_with_attr(html: str, attr_name: str) -> str:
    """Remove every element (open tag through its matching close tag) whose
    open tag carries ``attr_name``, with or without a value. Used to exclude
    the freshness line itself (``data-keel-freshness``) from its own hash --
    without this the date could never converge, since recording a new date
    would change the very content the next comparison hashes."""
    open_re = re.compile(
        r"<([a-zA-Z][\w-]*)\b[^>]*?\b"
        + re.escape(attr_name)
        + r"\b(=[\"'][^\"']*[\"'])?[^>]*?(/?)>",
        re.IGNORECASE,
    )
    out = []
    pos = 0
    while True:
        match = open_re.search(html, pos)
        if not match:
            out.append(html[pos:])
            break
        out.append(html[pos:match.start()])
        tag = match.group(1)
        if match.group(3):  # self-closing
            pos = match.end()
            continue
        pos = _find_matching_close(html, match.end(), tag)
    return "".join(out)


# Volatile substrings stripped before hashing regardless of host config --
# things that change on every request/response without the reader-visible
# content changing. A host extends this list via KEEL_SEO["freshness_strip_patterns"]
# (appended, not replacing these).
DEFAULT_STRIP_PATTERNS: list[tuple[str, str]] = [
    # Asset cache-busting query strings, e.g. "style.css?v=a1b2c3".
    (r"[?&]v=[0-9a-fA-F]+", ""),
    # A CSRF hidden input field -- a fresh token every render.
    (r"<input\b[^>]*\bname=[\"']csrfmiddlewaretoken[\"'][^>]*/?>", ""),
    # A CSRF token embedded anywhere else (a query string, inline JSON).
    (r"\bcsrfmiddlewaretoken=[^\"'&\s]+", ""),
    # A CSP nonce attribute -- regenerated every request.
    (r"\bnonce=[\"'][^\"']*[\"']", ""),
]


def normalize_content(
    html: str,
    *,
    selector: str = "main",
    strip_patterns: Optional[list] = None,
) -> str:
    """Extract the ``selector`` region of ``html`` and strip everything that
    varies without the content varying, collapsing all whitespace. Pure
    function -- no DB/Django access -- so it's independently testable.

    ``strip_patterns`` is a list of ``(regex, replacement)`` pairs applied in
    order after the built-in ``data-keel-freshness`` element removal. Pass
    ``None`` (the default) to use exactly ``DEFAULT_STRIP_PATTERNS``.
    """
    region = _extract_region(html, selector)
    region = _strip_elements_with_attr(region, "data-keel-freshness")
    patterns = DEFAULT_STRIP_PATTERNS if strip_patterns is None else strip_patterns
    for pattern, replacement in patterns:
        region = re.sub(pattern, replacement, region, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", region).strip()


def _content_hash(html: str, *, selector: str, strip_patterns: list) -> str:
    normalized = normalize_content(html, selector=selector, strip_patterns=strip_patterns)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _resolved_strip_patterns() -> list:
    extra = seo_setting("freshness_strip_patterns") or []
    return [*DEFAULT_STRIP_PATTERNS, *extra]


class FreshnessOutcome(str, Enum):
    """What ``record()`` did with a URL's stored freshness state."""

    CREATED = "created"    # first time this URL's content has ever been hashed
    CHANGED = "changed"    # hash differs from what was stored -> date moved
    UNCHANGED = "unchanged"  # hash matches -> stored date left alone


@dataclass(frozen=True)
class FreshnessResult:
    outcome: FreshnessOutcome
    landing: Landing
    previous_content_modified_at: Optional[_dt.datetime]


def record(url: str, html: str, *, now=None, dry_run: bool = False) -> FreshnessResult:
    """Normalize + hash ``html``, compare against the stored hash on the
    ``Landing`` row for ``url``, and update ``content_modified_at`` only when
    the hash actually changed. Idempotent by construction: running this twice
    in a row on the same rendered output never moves the date a second time.

    Never touches ``updated_at`` (via ``update_fields``) -- that field stays
    the row's own audit timestamp, never a publishable freshness signal.

    Raises ``Landing.DoesNotExist`` if ``url`` has no registry row -- callers
    (the management command) are expected to only pass registered URLs.
    """
    now = now or timezone.now()
    selector = seo_setting("freshness_content_selector")
    strip_patterns = _resolved_strip_patterns()
    digest = _content_hash(html, selector=selector, strip_patterns=strip_patterns)

    landing = Landing.objects.get(url=url)
    previous_hash = landing.content_hash
    previous_modified = landing.content_modified_at

    if not previous_hash:
        outcome = FreshnessOutcome.CREATED
    elif previous_hash != digest:
        outcome = FreshnessOutcome.CHANGED
    else:
        outcome = FreshnessOutcome.UNCHANGED

    if not dry_run and outcome is not FreshnessOutcome.UNCHANGED:
        landing.content_hash = digest
        landing.content_modified_at = now
        landing.save(update_fields=["content_hash", "content_modified_at"])

    return FreshnessResult(
        outcome=outcome,
        landing=landing,
        previous_content_modified_at=previous_modified,
    )


def freshness_for(url: str) -> Optional[_dt.datetime]:
    """The resolved public freshness date for ``url``, or ``None`` when the
    URL isn't registered or hasn't been recorded yet."""
    try:
        return Landing.objects.get(url=url).content_modified_at
    except Landing.DoesNotExist:
        return None


def isoformat_utc(dt: _dt.datetime) -> str:
    """Machine-readable UTC ISO-8601, e.g. ``2026-08-22T14:03:00Z`` -- the
    format the ``<time datetime="...">`` attribute and ``dateModified`` use."""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, _dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def humanize(dt: _dt.datetime) -> str:
    """Human-readable UTC rendering, e.g. "August 22, 2026" -- the visible
    body of the ``{% last_updated %}`` line."""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, _dt.timezone.utc)
    dt = dt.astimezone(_dt.timezone.utc)
    return f"{dt:%B} {dt.day}, {dt.year}"


def freshness_schema(url: str) -> dict:
    """``{"dateModified": "<iso utc>"}`` for a host to merge into its own
    JSON-LD, or ``{}`` when the URL has no recorded freshness date yet. Not a
    whole schema layer -- hosts already build their own JSON-LD; this hands
    back the one field, correctly formatted."""
    dt = freshness_for(url)
    if dt is None:
        return {}
    return {"dateModified": isoformat_utc(dt)}
