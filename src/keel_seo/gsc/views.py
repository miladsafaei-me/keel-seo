"""Views for the keel_seo.gsc dashboard (``keel_seo.gsc.urls``).

Data reports render deterministically from a per-window snapshot under
``KEEL_SEO["gsc_data_dir"]`` (chosen by ``?window=`` / ``?start=&end=``, media-volume
copy first, see :mod:`keel_seo.gsc.dashboard`); the insight cards render the
committed, host-curated ``gsc_insights.json`` — no model runs here.

Every view is superuser-gated (a superuser check inline, not a mixin, so this module
carries no host-specific permission dependency). Three actions reach into host
business logic through config hooks documented in :mod:`keel_seo.config`:

* ``queue`` — depositing a curated insight into the host's content-ideation queue
  (``KEEL_SEO["gsc_queue_hook"]``, no default — genuinely host-specific).
* ``dedicated_queue`` / ``cluster_queue`` — depositing keyword picks into
  keel-content's clustering-queue accumulator (``keel_content`` is itself a Keel
  package, so this is a soft import, not a host hook — a host without keel-content
  installed simply gets "not available" instead of an ImportError).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.module_loading import import_string
from django.views import View
from django.views.decorators.http import require_POST

from .. import config
from . import dashboard

logger = logging.getLogger(__name__)


def _is_superuser(user) -> bool:
    return bool(user.is_authenticated and user.is_superuser)


def _forbidden(request):
    """Not a superuser: send an authenticated user to the host's configured fallback
    (``KEEL_SEO["gsc_forbidden_redirect"]``), or fall through to Django's standard
    403. An anonymous user gets the standard login redirect."""
    if request.user.is_authenticated:
        name = config.seo_setting("gsc_forbidden_redirect")
        if name:
            try:
                return redirect(reverse(name))
            except Exception:
                pass
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    from django.contrib.auth.views import redirect_to_login

    return redirect_to_login(request.get_full_path(), login_url=settings.LOGIN_URL)


def _site_base(request) -> str:
    """``scheme://domain`` for this request — used to shorten displayed page URLs
    down to their path (GSC rows carry the full absolute URL)."""
    try:
        from django.contrib.sites.shortcuts import get_current_site

        site = get_current_site(request)
        return f"{request.scheme}://{site.domain}"
    except Exception:
        return f"{request.scheme}://{request.get_host()}"


class SearchConsoleView(View):
    """Search Console reporting + insights dashboard."""

    def get(self, request):
        if not _is_superuser(request.user):
            return _forbidden(request)
        ctx = dashboard.build_context(
            window=request.GET.get("window"),
            start=request.GET.get("start"),
            end=request.GET.get("end"),
        )
        ctx["page_pretitle"] = "SEO"
        ctx["gsc_base_template"] = config.seo_setting("gsc_base_template")
        ctx["sc_site_base"] = _site_base(request)
        return render(request, "keel_seo/gsc/search_console.html", ctx)


@require_POST
def dismiss(request):
    """Hide one insight, keyed by the fingerprint of its specific data. ``reason`` is
    ``done`` (acted on) or ``irrelevant`` (won't do) — it feeds the feedback loop."""
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    fingerprint = (request.POST.get("fingerprint") or "").strip()
    if not fingerprint:
        return JsonResponse({"error": "No fingerprint provided"}, status=400)
    reason = (request.POST.get("reason") or "done").strip()
    dashboard.add_dismissed(fingerprint, reason=reason)
    return JsonResponse({"ok": True, "fingerprint": fingerprint, "reason": reason})


@require_POST
def restore(request):
    """Un-hide every dismissed insight."""
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    restored = dashboard.clear_dismissed()
    return JsonResponse({"ok": True, "restored": restored})


@require_POST
def queue(request):
    """Send one curated INSIGHT into the host's content-ideation queue as a fresh idea.

    Insight cards are the one dashboard surface that already carries a written title
    and a stated intent — a human curated them — so there is nothing left to analyse
    and they can become a content-plan row directly, through the host's
    ``gsc_queue_hook``. Raw queries cannot: they go through ``dedicated_queue`` and
    the host's clustering stage instead.
    """
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    title = (request.POST.get("title") or "").strip()
    if not title:
        return JsonResponse({"error": "No title provided"}, status=400)

    hook_path = config.seo_setting("gsc_queue_hook")
    if not hook_path:
        return JsonResponse({"error": "Content-plan queueing is not configured"}, status=500)
    try:
        hook = import_string(hook_path)
    except Exception:
        logger.exception("gsc queue: could not import gsc_queue_hook %r", hook_path)
        return JsonResponse({"error": "Content-plan queueing is not configured"}, status=500)

    market = (request.POST.get("market") or "").strip()
    source_ref = (request.POST.get("source_ref") or "search-console")[:500]
    spec = {
        "title": title,
        "intent": (request.POST.get("intent") or "").strip(),
        "content_type": "blog",
        "markets": [market] if market else [],
    }
    kw = (request.POST.get("keyword") or "").strip()
    if kw:
        spec["keywords"] = [{"keyword": kw, "volume": 0}]
    try:
        plan, outcome = hook(spec, source_type="manual", source_ref=source_ref)
    except Exception:
        logger.exception("search-console queue: hook failed for %r", title)
        return JsonResponse({"error": "Could not queue this idea"}, status=500)
    if plan is None:
        return JsonResponse({"error": "Nothing to queue"}, status=400)

    edit_url = ""
    edit_name = config.seo_setting("gsc_plan_edit_url_name")
    if edit_name:
        try:
            edit_url = reverse(edit_name, args=[plan.pk])
        except Exception:
            edit_url = ""
    return JsonResponse({"ok": True, "outcome": outcome, "slug": plan.slug,
                         "status": plan.status, "url": edit_url})


@require_POST
def dedicated_exclude(request):
    """Permanently remove one query from the dedicated-content candidates list — it
    won't be suggested again on any future data refresh, live pull or snapshot."""
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    query = (request.POST.get("query") or "").strip()
    if not query:
        return JsonResponse({"error": "No query provided"}, status=400)
    dashboard.add_dedicated_excluded(query)
    return JsonResponse({"ok": True, "query": query})


@require_POST
def cluster_exclude(request):
    """Permanently remove one cluster from the dedicated-content By-cluster tab."""
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    cluster = (request.POST.get("cluster") or "").strip()
    if not cluster:
        return JsonResponse({"error": "No cluster provided"}, status=400)
    dashboard.add_dedicated_cluster_excluded(cluster)
    return JsonResponse({"ok": True, "cluster": cluster})


@require_POST
def dedicated_queue(request):
    """Send ONE dedicated-content query into keel-content's keyword-clustering queue.

    Not into the content-plan queue directly, even though a single query looks like
    it could be one row. A lone keyword usually belongs in a cluster with other
    keywords, and picking it out alone permanently loses that grouping — which
    keywords share an intent is exactly what clustering is for. And a content plan
    needs a title and a brief, which need the intent analysis this button does not
    have; deriving a title from the raw query string would be inventing one.

    So picks accumulate into a shared pool per market and are clustered together, and
    only what survives that becomes a content plan.
    """
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    query = (request.POST.get("query") or "").strip()
    if not query:
        return JsonResponse({"error": "No query provided"}, status=400)

    try:
        from keel_content.management.commands.clusterjob_ingest import append_to_open_pool
    except ImportError:
        return JsonResponse({"error": "Clustering queue is not available"}, status=500)

    market = (request.POST.get("market") or "").strip()
    keyword = {"keyword": query}
    for key in ("impressions", "clicks", "position"):
        raw = request.POST.get(key)
        if raw:
            try:
                keyword[key] = float(raw) if key == "position" else int(float(raw))
            except (TypeError, ValueError):
                pass

    base_slug, label = dashboard.picks_pool_identity(market)
    try:
        job, outcome = append_to_open_pool(
            base_slug=base_slug,
            label=label,
            keywords=[keyword],
            market=market,
            source_type="search_console",
            source_ref="search-console:dedicated",
            notes=(
                "Queries picked one at a time from the Search Console dashboard's "
                "dedicated-content candidates."
            ),
        )
    except Exception:
        logger.exception("search-console: pick queue failed for %r", query)
        return JsonResponse({"error": "Could not queue this keyword"}, status=500)

    if job is None:
        return JsonResponse({"error": "Nothing to queue"}, status=400)
    return JsonResponse({
        "ok": True,
        "outcome": outcome,
        "slug": job.slug,
        "status": job.status,
        "keywords": job.keyword_count,
        "url": config.gsc_queue_list_url(),
    })


@require_POST
def cluster_queue(request):
    """Send one whole dedicated-content CLUSTER into keel-content's clustering queue.

    Same destination as the per-query button, one level up: a cluster is a pool of
    keywords nobody has analysed yet — how many articles it is worth, what their
    intents are, and whether any of it is even in scope are all things clustering
    decides. Depositing it straight into the content-plan queue would mean inventing
    those answers here.
    """
    if not _is_superuser(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    cluster = (request.POST.get("cluster") or "").strip()
    if not cluster:
        return JsonResponse({"error": "No cluster provided"}, status=400)

    row = dashboard.dedicated_cluster_members(
        cluster,
        window=request.POST.get("window") or None,
        start=request.POST.get("start") or None,
        end=request.POST.get("end") or None,
    )
    keywords = row.get("queries") or []
    if not keywords:
        return JsonResponse(
            {"error": "No keywords found for this cluster in the current range"},
            status=400,
        )

    try:
        from keel_content.management.commands.clusterjob_ingest import upsert_cluster_job
    except ImportError:
        return JsonResponse({"error": "Clustering queue is not available"}, status=500)

    try:
        job, outcome = upsert_cluster_job(
            label=cluster,
            keywords=keywords,
            market=(request.POST.get("market") or "").strip(),
            source_type="search_console",
            source_ref="search-console:dedicated-cluster",
            notes=(
                f"Sent from the Search Console dashboard — dedicated-content cluster "
                f"'{cluster}', {len(keywords)} querie(s) ranking below page 1."
            ),
        )
    except Exception:
        logger.exception("search-console: cluster queue failed for %r", cluster)
        return JsonResponse({"error": "Could not queue this cluster"}, status=500)

    if job is None:
        return JsonResponse({"error": "Nothing to queue"}, status=400)
    return JsonResponse({
        "ok": True,
        "outcome": outcome,
        "slug": job.slug,
        "status": job.status,
        "keywords": job.keyword_count,
        "url": config.gsc_queue_list_url(),
    })
