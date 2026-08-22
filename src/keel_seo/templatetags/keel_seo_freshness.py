"""Template tag rendering the public last-updated line for the current page.

Load with ``{% load keel_seo_freshness %}`` and place ``{% last_updated %}``
wherever a page should show its freshness (e.g. next to the byline). Looks up
``request.path`` in the Landing registry via ``keel_seo.freshness.freshness_for``
-- populated by ``keel_seo.freshness.record``, run in bulk via the
``keel_seo_freshness`` management command -- and renders nothing at all when no
date has been recorded yet, rather than a placeholder or a fabricated date.

The rendered markup always carries ``data-keel-freshness`` on its outermost
element, and uses a semantic ``<time datetime="...">`` with a machine-readable
UTC ISO-8601 attribute plus a human-readable body. The ``data-keel-freshness``
attribute is load-bearing: ``keel_seo.freshness.normalize_content`` strips any
element carrying it before hashing, so this line is excluded from its own
hash input -- without that, recording a new date would change the very
content the next comparison hashes, and the date could never converge.

Override the markup by placing your own ``keel_seo/freshness/last_updated.html``
ahead of this package's copy in your app's template search path (Django's
normal per-app template-loader precedence) -- restyle freely with your own
tokens, but keep the outer ``data-keel-freshness`` attribute and the
``<time datetime="...">`` element, or the hashing/rendering contract above no
longer holds.
"""
from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from ..freshness import freshness_for, humanize, isoformat_utc

register = template.Library()


@register.simple_tag(takes_context=True)
def last_updated(context):
    request = context.get("request")
    if request is None:
        return ""
    dt = freshness_for(request.path)
    if dt is None:
        return ""
    rendered = render_to_string(
        "keel_seo/freshness/last_updated.html",
        {"iso_datetime": isoformat_utc(dt), "human_date": humanize(dt)},
    )
    return mark_safe(rendered)
