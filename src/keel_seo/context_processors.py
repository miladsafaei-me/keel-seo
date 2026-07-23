"""Template context processor that injects the Landing row for the current path.

A base template renders the robots meta from ``landing.is_indexable``; when no
row matches the path, ``landing`` is ``None`` and the template falls back to a
noindex default (see ``templates/keel_seo/robots_meta.html``). Cached per-path,
with a sentinel for misses so paths that never match (404s, asset URLs sharing
the middleware pipeline) don't hit the DB every request. Invalidated on
``Landing.save()``/``delete()`` via ``keel_seo.signals``.
"""
from django.core.cache import cache
from django.db import DatabaseError

from .config import seo_setting
from .models import Landing

_MISS_SENTINEL = "__none__"


def landing(request):
    key = f"landing:{request.path}"
    try:
        cached = cache.get(key)
    except Exception:
        # A value cached under a previous model layout (e.g. the pre-extraction
        # core.Landing) can't be unpickled once the model moves into keel_seo —
        # cache.get raises LookupError deep in pickle. Treat it as a miss and
        # drop the poison key rather than 500-ing every page through this
        # processor. The DB re-query below rewrites it cleanly.
        try:
            cache.delete(key)
        except Exception:
            pass
        cached = None
    if cached == _MISS_SENTINEL:
        return {"landing": None}
    if cached is not None:
        return {"landing": cached}
    try:
        row = Landing.objects.filter(url=request.path).first()
    except DatabaseError:
        return {"landing": None}
    cache.set(key, row if row is not None else _MISS_SENTINEL, seo_setting("cache_ttl"))
    return {"landing": row}
