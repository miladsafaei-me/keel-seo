"""Views for the keel_seo.gsc dashboard (``keel_seo.gsc.urls``).

Data reports render deterministically from a per-window snapshot under
``KEEL_SEO["gsc_data_dir"]`` (chosen by ``?window=`` / ``?start=&end=``, media-volume
copy first, see :mod:`keel_seo.gsc.dashboard`); the insight cards render the
committed, host-curated ``gsc_insights.json``, no model runs here.

Every view is superuser-gated (a superuser check inline, not a mixin, so this module
carries no host-specific permission dependency). Three actions reach into host
business logic through config hooks documented in :mod:`keel_seo.config`:

* ``queue``, depositing a curated insight into the host's content-ideation queue
  (``KEEL_SEO["gsc_queue_hook"]``, no default, genuinely host-specific).
* ``dedicated_queue`` / ``cluster_queue``, depositing keyword picks into
  keel-content's clustering-queue accumulator (``keel_content`` is itself a Keel
  package, so this is a soft import, not a host hook, a host without keel-content
  installed simply gets "not available" instead of an ImportError).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
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
    """``scheme://domain`` for this request, used to shorten displayed page URLs
    down to their path (GSC rows carry the full absolute URL)."""
    try:
        from django.contrib.sites.shortcuts import get_current_site

        site = get_current_site(request)
        return f"{request.scheme}://{site.domain}"
    except Exception:
        return f"{request.scheme}://{request.get_host()}"


def _property_base(site: str) -> str:
    """``scheme://domain`` for a declared property, so its page URLs shorten the same way
    the host's own do."""
    if site.startswith("sc-domain:"):
        return "https://" + site.split(":", 1)[1]
    return site.rstrip("/")


class SearchConsoleView(View):
    """The Search Console report for the host's own property or for one declared in
    ``KEEL_SEO["gsc_properties"]``: the same report for every property."""

    def get(self, request, property_key=None):
        if not _is_superuser(request.user):
            return _forbidden(request)
        prop = None
        if property_key is not None:
            prop = config.gsc_property(property_key)
            if prop is None:
                raise Http404("No such Search Console property")
        if config.seo_setting("gsc_dashboard") == "classic":
            return self._classic(request, prop)
        if request.GET.get("download") == "pdf":
            return monthly_pdf(request, prop)
        ctx = report_context(request, prop)
        ctx["gsc_base_template"] = config.seo_setting("gsc_base_template")
        return render(request, "keel_seo/gsc/report.html", ctx)

    def _classic(self, request, prop):
        ctx = dashboard.build_context(
            window=request.GET.get("window"),
            start=request.GET.get("start"),
            end=request.GET.get("end"),
            prop=prop,
        )
        ctx["page_pretitle"] = "SEO"
        ctx["gsc_base_template"] = config.seo_setting("gsc_base_template")
        ctx["sc_standalone_css"] = config.seo_setting("gsc_standalone_css")
        ctx["sc_site_base"] = _property_base(prop["site"]) if prop else _site_base(request)
        return render(request, "keel_seo/gsc/search_console.html", ctx)


def monthly_pdf(request, prop: "dict | None"):
    """The client's monthly report as a PDF download (``?download=pdf&month=YYYY-MM``)."""
    from django.http import HttpResponse

    from . import report_api, report_pdf

    target = report_property(prop)
    try:
        payload = report_api.monthly(target, request.GET.get("month"))
        pdf = report_pdf.render(payload, target, config.seo_setting("gsc_report_brand"))
    except report_pdf.PdfUnavailable as exc:
        return HttpResponse(str(exc), status=503, content_type="text/plain; charset=utf-8")
    except Exception as exc:  # noqa: BLE001: a failed pull becomes a readable answer
        logger.exception("gsc monthly pdf failed for %s", target.get("site"))
        return HttpResponse(f"The monthly report could not be built: {str(exc)[:300]}", status=502,
                            content_type="text/plain; charset=utf-8")
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{report_pdf.filename(target, payload["month"])}"'
    return response


# The old dashboard's ?window= values, so a saved link still opens the nearest range.
_OLD_WINDOWS = {"7d": "7d", "30d": "28d", "60d": "3m", "90d": "3m", "full": "16m"}


def _brand_list(spec) -> list:
    """A brand spec (a list of (label, regex) or a preset name) as a list."""
    from .build import BRAND_PRESETS

    if isinstance(spec, str):
        return list(BRAND_PRESETS.get(spec, []))
    return list(spec or [])


def report_property(prop: "dict | None") -> dict:
    """The property the report reads: a declared one as it is, or the host's own,
    built from ``gsc_site``, ``gsc_credentials`` and ``gsc_brands``."""
    from . import auth

    if prop:
        return {**prop, "brands": _brand_list(prop.get("brands")), "credentials": str(
            prop.get("credentials") or auth.credentials_path())}
    site = auth.resolve_site()
    return {"key": "", "site": site, "label": _property_base(site).split("://", 1)[-1],
            "credentials": str(auth.credentials_path()),
            "brands": _brand_list(config.seo_setting("gsc_brands"))}


def report_choices(params) -> dict:
    """The reader's choices from the query string, each one validated."""
    from . import report as R

    range_key = params.get("range") or _OLD_WINDOWS.get(params.get("window") or "", R.DEFAULT_RANGE)
    if params.get("start") and params.get("end"):
        range_key = "custom"
    filters = {}
    for dim, (_label, ops) in R.FILTER_DIMENSIONS.items():
        value = (params.get(dim) or "").strip()[:200]
        if value:
            op = params.get(f"{dim}_op") or ops[0]
            filters[dim] = {"op": op if op in ops else ops[0],
                            "value": value.upper() if dim == "device" else (value.lower() if dim == "country" else value)}
    return {
        "range_key": range_key,
        "start": params.get("start"),
        "end": params.get("end"),
        "compare": params.get("compare") or R.DEFAULT_COMPARE,
        "search_type": params.get("type") or "web",
        "filters": filters,
    }


REPORT_METRICS = [
    ("clicks", "Total clicks", "count"),
    ("impressions", "Total impressions", "count"),
    ("ctr", "Average CTR", "ctr"),
    ("position", "Average position", "position"),
]


def _change_text(ch: "dict | None") -> "dict | None":
    """A tile's change as words and a direction: ``{text, tone, title}``."""
    if not ch:
        return None
    if ch["flat"]:
        return {"text": "no change", "tone": "flat", "title": "Same as the earlier period"}
    if ch["unit"] == "new":
        return {"text": "new", "tone": "up", "title": "No traffic in the earlier period"}
    arrow = "\u25b2" if ch["good"] else "\u25bc"
    v = abs(ch["value"])
    unit = {"%": "%", "pp": " pp", "places": ""}[ch["unit"]]
    title = {"%": "Change against the earlier period",
             "pp": "Change in percentage points",
             "places": "Places gained (up) or lost (down) in average position"}[ch["unit"]]
    return {"text": f"{arrow} {v:g}{unit}", "tone": "up" if ch["good"] else "down", "title": title}


def _without(params, dim: str) -> str:
    """The current query string without one filter."""
    q = params.copy()
    for key in (dim, f"{dim}_op"):
        q.pop(key, None)
    return "?" + q.urlencode()


def report_context(request, prop: "dict | None") -> dict:
    """Everything report.html renders, or an error the page can show."""
    import logging as _logging

    from . import report as R
    from . import report_api

    target = report_property(prop)
    choices = report_choices(request.GET)
    data, error = None, None
    try:
        data = report_api.build_report(target, **choices)
    except Exception as exc:  # noqa: BLE001: a failed pull becomes the page's message
        _logging.getLogger(__name__).exception("gsc report failed for %s", target.get("site"))
        error = str(exc)[:400]
    tiles, alerts, notes, discover_tiles, devices, months = [], [], [], [], [], []
    if data:
        disc = data.get("discover") or {}
        if disc.get("available"):
            for key, label, kind in REPORT_METRICS[:3]:
                discover_tiles.append({
                    "key": key, "label": label, "value": R.compact(disc["totals"].get(key), kind),
                    "prev": R.compact(disc["prev_totals"].get(key), kind) if disc.get("prev_totals") else None,
                    "change": _change_text((disc.get("changes") or {}).get(key))})
        devices = [{**d, "change": _change_text((d.get("changes") or {}).get("clicks"))}
                   for d in data.get("devices") or []]
        last = R.parse_date(data["last_complete"])
        months = R.complete_months(last, dt.date.today()) if last else []
        prev = data.get("prev_totals")
        for key, label, kind in REPORT_METRICS:
            tiles.append({"key": key, "label": label,
                          "value": R.compact(data["totals"].get(key), kind),
                          "prev": R.compact(prev.get(key), kind) if prev else None,
                          "change": _change_text(data["changes"].get(key))})
        alerts, notes = R.alerts(data), R.notes(data)
    active_filters = [{"dim": dim, "label": R.FILTER_DIMENSIONS[dim][0], "op": f["op"],
                       "op_label": R.OPERATOR_LABELS[f["op"]], "value": f["value"],
                       "clear_url": _without(request.GET, dim)}
                      for dim, f in choices["filters"].items()]
    return {
        "page_pretitle": "Search Console",
        "page_title": target.get("label") or target["site"],
        "tiles": tiles,
        "discover_tiles": discover_tiles,
        "device_rows": devices,
        "months": months,
        "alerts": alerts,
        "notes": notes,
        "active_filters": active_filters,
        "reset_url": "?",
        "range_label": (R.RANGE_LABELS.get(choices["range_key"]) or
                        (data["range"]["span"] if data else "Custom")),
        "compare_label": R.COMPARE_LABELS.get(choices["compare"], R.COMPARE_LABELS[R.DEFAULT_COMPARE]),
        "type_label": R.SEARCH_TYPE_LABELS.get(choices["search_type"], "Web"),
        "rp": target,
        "report": data,
        "report_error": error,
        "choices": choices,
        "ranges": R.RANGES,
        "compares": R.COMPARES,
        "search_types": R.SEARCH_TYPES,
        "filter_dims": [(k, label, ops) for k, (label, ops) in R.FILTER_DIMENSIONS.items()],
        "operator_labels": R.OPERATOR_LABELS,
        "devices": R.DEVICES,
        "site_base": _property_base(target["site"]),
    }


@require_POST
def dismiss(request):
    """Hide one insight, keyed by the fingerprint of its specific data. ``reason`` is
    ``done`` (acted on) or ``irrelevant`` (won't do), it feeds the feedback loop."""
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
    and a stated intent, a human curated them, so there is nothing left to analyse
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
    """Permanently remove one query from the dedicated-content candidates list, it
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
    keywords, and picking it out alone permanently loses that grouping, which
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
    keywords nobody has analysed yet, how many articles it is worth, what their
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
                f"Sent from the Search Console dashboard, dedicated-content cluster "
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
