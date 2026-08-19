"""Search Console dashboard — data + insight loading for keel_seo.gsc's dashboard view.

Two lifecycles, deliberately separate (this is the whole design):

* **Data** — ``gsc_dashboard.json``, produced deterministically by a host's own
  offline exporter (e.g. SignalBots' ``tools/gsc/export_dashboard.py``) from the GSC
  registry. Rendered as-is, no LLM.
* **Insights** — ``gsc_insights.json``, curated by the LLM on request and committed
  by the host. The dashboard renders it verbatim; no model runs at view time.

Both files are committed under ``KEEL_SEO["gsc_data_dir"]`` on the host (see
:func:`keel_seo.config.gsc_data_dir`) so they ship in the image. Insight
**dismissals** are the one piece of runtime state: a superuser can delete an
insight, keyed by a fingerprint of *that insight's specific data* (id + its queries
+ its metrics). A look-alike insight generated later for DIFFERENT data gets a
different fingerprint, so it still shows — dismissing one instance never suppresses
a genuinely new one. Dismissals live in a JSON file on ``settings.MEDIA_ROOT``
(survives redeploys without a DB migration).

Ideation/insight-*curation* logic (turning this data into content ideas) stays in
the host — see the package boundary in ``keel_seo/CLAUDE.md``. This module only
reads what a host's own tooling produced or pulls live GSC data through
:mod:`keel_seo.gsc.connector`.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .. import config
from . import build as gsc_build

# Google-Search-Console-style palette (base tokens; the fill-*/bg-* forms are
# safelisted in client tailwind input.css). Assigned in order, never cycled; Other
# is always gray. Clicks = GSC blue, impressions = GSC purple.
CATEGORICAL = ["gsc-p1", "gsc-p2", "gsc-p3", "gsc-p4", "gsc-p5", "gsc-p6"]  # pastel — pies
OTHER_COLOR = "gray-300"
TIME_COLORS = ["gsc-p1", "gsc-p3", "gsc-p4"]             # 0-30d, 30-60d, 60-90d (pastel)

def _windows_dir() -> Path:
    """Per-window snapshots shipped in the image (``KEEL_SEO["gsc_data_dir"]/windows``)."""
    return config.gsc_data_dir() / "windows"


def _insights_json() -> Path:
    return config.gsc_data_dir() / "gsc_insights.json"

# Selectable time windows, smallest first. Each is a self-contained snapshot produced
# by tools/gsc/build_windows.py; "full" is the accumulated registry and the default.
WINDOWS = [
    ("7d", 7, "7 days"),
    ("30d", 30, "30 days"),
    ("60d", 60, "60 days"),
    ("90d", 90, "90 days"),
    ("full", 480, "All"),
]
WINDOW_DAYS = {key: days for key, days, _ in WINDOWS}
DEFAULT_WINDOW = "90d"

INSIGHT_CATEGORIES = [
    ("new_intent", "New user intents", "New needs arriving with new queries"),
    ("content_optimization", "Content optimization", "Rank an existing page higher for demand it already sees"),
    ("dedicated_content", "Dedicated content", "New pages for demand landing on generic / mismatched pages"),
]
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _media_windows_dir() -> "Path | None":
    """Fresh snapshots land here (1-2x/day refresh, no redeploy); image copies are the seed."""
    try:
        from django.conf import settings

        return Path(settings.MEDIA_ROOT) / "gsc" / "dashboard"
    except Exception:
        return None


def _load_coverage() -> "dict | None":
    """Index-coverage snapshot written by the `gsc_coverage` command (media volume)."""
    try:
        from django.conf import settings

        return json.loads((Path(settings.MEDIA_ROOT) / "gsc" / "coverage.json").read_text())
    except Exception:
        return None


def _load_window(key: str) -> dict:
    """A window snapshot: media volume first (fresh), then the committed image copy,
    then the accumulated ``full`` window as a last resort so a page always renders."""
    media = _media_windows_dir()
    if media:
        data = _read(media / f"{key}.json")
        if data:
            return data
    windows_dir = _windows_dir()
    data = _read(windows_dir / f"{key}.json")
    if data:
        return data
    return _read(windows_dir / "full.json")


def _live_target(window: "str | None", start: "str | None", end: "str | None") -> tuple:
    """The concrete (window_key, start, end) to compute live, or (None, None, None) when
    the request should use the committed snapshot instead. Presets become a trailing
    window ending at the GSC data lag; an explicit start/end is a custom range; ``full``
    (and the bare default) always stays the curated registry snapshot."""
    if start and end:
        return "custom", start, end
    import datetime as _dt

    # Every preset AND the "All" window (+ the bare default) is a trailing live window.
    # "All" is a 480-day (~16-month, GSC domain-property max) pull — so it is always the
    # widest window, never smaller than a preset (the old registry snapshot undercounted).
    key = window if window in WINDOW_DAYS else DEFAULT_WINDOW
    # Pull right up to today: gsc_live uses dataState="all" (fresh data), so recent days
    # are populated, and the empty tail (if any) is trimmed downstream. Ending at the old
    # today-2 lag is what left the freshest day rendering as a zero.
    e = _dt.date.today()
    s = e - _dt.timedelta(days=WINDOW_DAYS[key] - 1)
    return key, s.isoformat(), e.isoformat()


def _resolve_window(window: "str | None", start: "str | None", end: "str | None") -> tuple:
    """Pick which snapshot to serve. An explicit start/end (the free calendar) maps to
    the smallest preset whose span covers it; a bare ?window= picks that preset directly.

    Returns (key, snapped_from) where snapped_from is the user's requested range label
    when we had to round a custom span up to a preset, else None.
    """
    if start and end:
        try:
            import datetime as _dt

            span = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days + 1
        except ValueError:
            span = None
        if span and span > 0:
            for key, days, _ in WINDOWS:
                if days >= span:
                    snapped = None if days == span else f"{start} → {end}"
                    return key, snapped
            return "full", f"{start} → {end}"
    if window in WINDOW_DAYS:
        return window, None
    return DEFAULT_WINDOW, None


def _twelfths(value: float, peak: float) -> int:
    """A 1..11 bucket for a `w-{n}/12` bar (11 is the safelisted max)."""
    if peak <= 0:
        return 1
    return max(1, min(11, round(value / peak * 11)))


def _position_tone(position: float) -> str:
    """Reserved status palette — always paired with the number, never colour alone."""
    if position and position <= 3:
        return "good"
    if position and position <= 10:
        return "warn"
    return "bad"


def _pie(slices: list) -> list:
    """SVG pie geometry for brand slices in a 0..100 viewBox (cx=cy=50, r=48)."""
    cx = cy = 50.0
    r = 48.0
    total = sum(s["value"] for s in slices) or 1
    out = []
    angle = -90.0  # start at 12 o'clock
    ci = 0
    for s in slices:
        frac = s["value"] / total
        sweep = frac * 360.0
        start, end = angle, angle + sweep
        if s["brand"] == "Other":
            color = OTHER_COLOR
        else:
            color = CATEGORICAL[ci % len(CATEGORICAL)]
            ci += 1
        full = frac >= 0.999
        d = ""
        if not full:
            x1 = cx + r * math.cos(math.radians(start))
            y1 = cy + r * math.sin(math.radians(start))
            x2 = cx + r * math.cos(math.radians(end))
            y2 = cy + r * math.sin(math.radians(end))
            large = 1 if sweep > 180 else 0
            d = f"M{cx:.2f},{cy:.2f} L{x1:.2f},{y1:.2f} A{r},{r} 0 {large} 1 {x2:.2f},{y2:.2f} Z"
        out.append({"brand": s["brand"], "pct": s["pct"], "value": s["value"],
                    "d": d, "full": full, "color": color})
        angle = end
    return out


def _brand_bars(brand: dict) -> list:
    """Merge the per-metric brand slice lists into one row per brand carrying the
    keywords / impressions / clicks percentage plus a 1..11 bar width per metric.

    Rows are ordered by clicks share (then impressions) with "Other" always last, so
    the table reads as a ranked comparison — the pies' job, but scannable and compact.
    """
    metrics = ("queries", "impressions", "clicks")
    agg: dict = {}
    for metric in metrics:
        for s in brand.get(metric, []) or []:
            row = agg.setdefault(s["brand"], {"brand": s["brand"],
                                              "queries": 0.0, "impressions": 0.0, "clicks": 0.0})
            row[metric] = s.get("pct", 0.0)
    peak = {m: max((r[m] for r in agg.values()), default=0.0) for m in metrics}
    rows = list(agg.values())
    for r in rows:
        for m in metrics:
            r[f"{m}_width_12"] = _twelfths(r[m], peak[m])
    rows.sort(key=lambda r: (r["brand"] == "Other", -r["clicks"], -r["impressions"]))
    return rows


def _vgroups(groups: list, colors: list, series_labels: "list | None" = None) -> dict:
    """Vertical grouped-bar geometry (SVG). `groups`: [{label, values:[...]}].

    The chart fills its box via preserveAspectRatio="none" (only rects, no in-SVG
    text — group labels render as HTML, values live in each rect's <title>), so it
    can stretch to match a neighbouring column's height without distortion. One
    color per series, assigned in order.
    """
    n = len(groups)
    ns = len(groups[0]["values"]) if groups else 0
    if not n or not ns:
        return {"w": 100.0, "h": 100.0, "rects": [], "glabels": []}
    slot, gap, base, top = 70.0, 16.0, 96.0, 6.0
    zone = base - top
    mx = max((max(g["values"]) for g in groups), default=0) or 1
    bw = (slot / ns) * 0.66
    inner_gap = (slot - bw * ns) / (ns + 1)
    W = gap + n * (slot + gap)
    rects, glabels = [], []
    x = gap
    for g in groups:
        glabels.append({"label": g["label"]})
        bx = x + inner_gap
        for si, v in enumerate(g["values"]):
            h = v / mx * zone
            slabel = series_labels[si] if series_labels and si < len(series_labels) else ""
            rects.append({
                "x": round(bx, 1), "y": round(base - h, 1), "w": round(bw, 1), "h": round(h, 1),
                "color": colors[si % len(colors)], "value": v,
                "glabel": g["label"], "slabel": slabel,
            })
            bx += bw + inner_gap
        x += slot + gap
    return {"w": round(W, 1), "h": round(base + 4, 1), "rects": rects, "glabels": glabels}


# ---- Insight fingerprint + dismissals (runtime state on the media volume) ----

def insight_fingerprint(insight: dict) -> str:
    """Stable id for THIS insight's specific data (id + its queries + its metrics).

    Changes when the underlying data changes, so a dismissal is scoped to the
    exact instance — a fresh look-alike for new data is a different fingerprint.
    """
    payload = {
        "id": insight.get("id", ""),
        "queries": sorted(insight.get("queries", []) or []),
        "metrics": insight.get("metrics", {}) or {},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _dismiss_path() -> "Path | None":
    try:
        from django.conf import settings

        return Path(settings.MEDIA_ROOT) / "gsc" / "dismissed_insights.json"
    except Exception:
        return None


DISMISS_REASONS = ("done", "irrelevant")


def load_dismissed() -> dict:
    """Fingerprint -> {reason, at}. Reads the current dict form and the legacy list
    form (a bare list of fingerprints = dismissed with an unknown reason)."""
    path = _dismiss_path()
    if not path or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    if isinstance(raw, list):
        return {fp: {"reason": "done", "at": ""} for fp in raw}
    return raw if isinstance(raw, dict) else {}


def add_dismissed(fingerprint: str, reason: str = "done") -> None:
    """Hide one insight with a reason — ``done`` (acted on) or ``irrelevant`` (won't
    do). The reason feeds the insight-authoring feedback loop, not just the UI."""
    path = _dismiss_path()
    if not path:
        return
    if reason not in DISMISS_REASONS:
        reason = "done"
    from django.utils import timezone

    current = load_dismissed()
    current[fingerprint] = {"reason": reason, "at": timezone.now().date().isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, sort_keys=True))


def clear_dismissed() -> int:
    path = _dismiss_path()
    if not path or not path.exists():
        return 0
    count = len(load_dismissed())
    try:
        path.unlink()
    except OSError:
        return 0
    return count


# ---- Dedicated-content candidate exclusions (runtime state on the media volume) ----
#
# Two independent exclusion lists, one per view of the same table: a query the reader
# dismissed in the Detail tab, and a whole cluster dismissed in the By-cluster tab.
# They are kept apart rather than folded into one list because they answer different
# questions — "this keyword is not worth a page" versus "this whole topic is not ours"
# — and collapsing a cluster dismissal into its member queries would silently bury
# keywords that a later data refresh assigns to a different cluster.

_EXCLUDE_FILES = {
    "query": "dedicated_excluded.json",
    "cluster": "dedicated_cluster_excluded.json",
}


def _exclude_path(kind: str) -> "Path | None":
    try:
        from django.conf import settings

        return Path(settings.MEDIA_ROOT) / "gsc" / _EXCLUDE_FILES[kind]
    except Exception:
        return None


def _load_excluded(kind: str) -> set:
    path = _exclude_path(kind)
    if not path or not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return set()
    return set(raw) if isinstance(raw, list) else set()


def _add_excluded(kind: str, value: str) -> None:
    v = (value or "").strip().lower()
    if not v:
        return
    path = _exclude_path(kind)
    if not path:
        return
    current = _load_excluded(kind)
    current.add(v)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(current), ensure_ascii=False))


def load_dedicated_excluded() -> set:
    """Query strings (lowercased) permanently removed from the dedicated-content
    candidates list. Applied on every render — live pull or committed snapshot alike —
    so a deleted row never resurfaces in a later data refresh."""
    return _load_excluded("query")


def add_dedicated_excluded(query: str) -> None:
    _add_excluded("query", query)


def load_dedicated_cluster_excluded() -> set:
    """Cluster names (lowercased) permanently removed from the By-cluster tab."""
    return _load_excluded("cluster")


def add_dedicated_cluster_excluded(cluster: str) -> None:
    _add_excluded("cluster", cluster)


# Single queries picked out of the Detail tab accumulate into one pool per market
# rather than one pool each, so they are clustered together — which keywords share an
# intent is the whole question clustering answers, and a pool of one cannot answer it.
PICKS_POOL_PREFIX = "search-console-picks"


def picks_pool_identity(market: str = "") -> tuple:
    """``(base_slug, label)`` of the accumulator pool for one market."""
    m = (market or "").strip().lower()
    if m:
        return f"{PICKS_POOL_PREFIX}-{m}", f"Search Console picks — {m}"
    return PICKS_POOL_PREFIX, "Search Console picks — cross-market"


def picked_queries() -> dict:
    """Every query already sitting in a Search-Console picks pool → that pool's status.

    Lets the Detail tab render a keyword as already taken on page load rather than only
    after a click, the same way the By-cluster tab does for whole clusters.
    """
    Job = _cluster_job_model()
    if Job is None:
        return {}
    out = {}
    try:
        rows = Job.objects.filter(slug__startswith=PICKS_POOL_PREFIX).values_list(
            "keywords", "status"
        )
    except Exception:
        return {}
    for keywords, status in rows:
        for kw in keywords or []:
            if isinstance(kw, dict):
                term = str(kw.get("keyword", "")).strip().lower()
                if term:
                    out[term] = status
    return out


def _cluster_job_model():
    """The clustering-queue model, or ``None`` when this host has none configured.

    Resolved through keel-content's host seam rather than imported, so the dashboard
    keeps working on a host that runs no clustering queue at all — the By-cluster tab
    simply offers no queue action instead of failing to render.
    """
    try:
        from keel_content.host import cluster_job_model

        return cluster_job_model()
    except Exception:
        return None


def dedicated_cluster_members(cluster: str, *, window=None, start=None, end=None) -> dict:
    """The member queries of one dedicated-content cluster, straight from the payload
    the page was rendered from.

    The browser never receives these — a broad cluster carries hundreds of keywords and
    the page has no use for them — so the queue action posts a cluster NAME and this
    re-derives the list server-side from the same window the reader was looking at.
    """
    target = (cluster or "").strip().lower()
    if not target:
        return {}
    data = _window_payload(window=window, start=start, end=end)
    for row in data.get("dedicated_by_cluster", []):
        if str(row.get("cluster", "")).strip().lower() == target:
            return row
    return {}


def _kfmt(v: float) -> str:
    v = round(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if v >= 1000:
        return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(v)


# Chart geometry (viewBox units). Rendered server-side with a STATIC viewBox +
# preserveAspectRatio="none" so the line always spans the full width — an Alpine
# :viewBox binding lowercases the attribute and SVG then ignores it, leaving the plot
# stranded in the left of the box. Path visibility + hover cursor/dots are client-side.
_TL_W, _TL_H, _TL_PT, _TL_PB = 1000, 300, 14, 6

# GSC "Performance" metrics. Order fixes which axis each takes when toggled on (first
# active → left axis, second active → right axis); ``color`` is the sc-* line/box token.
_TL_METRICS = [
    ("clicks", "Total clicks", "clicks", "count"),
    ("impressions", "Total impressions", "impr", "count"),
    ("ctr", "Average CTR", "ctr", "pct"),
    ("position", "Average position", "pos", "position"),
]
_TL_SKEY = {"clicks": "clicks", "impressions": "impr", "ctr": "ctr", "position": "pos"}


def _path_with_gaps(pts: list) -> str:
    """An SVG path over (x, y) points, lifting the pen on any None y (a day with no data,
    e.g. average-position on a zero-impression day) so the line breaks instead of diving."""
    out, pen_up = [], True
    for px, py in pts:
        if py is None:
            pen_up = True
            continue
        out.append(("M" if pen_up else "L") + f"{px},{py}")
        pen_up = False
    return " ".join(out)


def _tl_xticks(series: list, n: int) -> list:
    import datetime as _dt

    xticks, want, seen = [], min(6, n), set()
    for k in range(want):
        i = round(k / (want - 1) * (n - 1)) if want > 1 else 0
        if i in seen:
            continue
        seen.add(i)
        try:
            label = _dt.date.fromisoformat(series[i]["date"]).strftime("%b %-d")
        except ValueError:
            label = series[i]["date"]
        xticks.append({"pct": round(50 if n <= 1 else i / (n - 1) * 100, 2), "label": label})
    return xticks


def _timeseries_charts(ts: dict, totals: dict) -> "dict | None":
    """GSC-style Performance chart: one normalized line per metric (clicks, impressions,
    CTR, average position — each on its own scale so it fills the plot, position inverted
    so a better rank sits higher), plus each metric's axis ticks and headline total. The
    client (scPerformance) toggles which lines/axes show and drives the hover tooltip."""
    days = ts.get("days", []) if ts else []
    if not days:
        return None
    series = [{"date": d["date"], "clicks": d["c_clicks"], "impr": d["c_impr"],
               "ctr": d.get("c_ctr", 0.0), "pos": d.get("c_pos", 0.0)} for d in days]
    n = len(series)
    W, H, PT, PB = _TL_W, _TL_H, _TL_PT, _TL_PB
    plot_h = H - PT - PB

    def x(i: int) -> float:
        return W / 2 if n <= 1 else round(i / (n - 1) * W, 1)

    metrics, hover_y = [], {}
    for mkey, label, color, kind in _TL_METRICS:
        skey = _TL_SKEY[mkey]
        vals = [s[skey] for s in series]
        if kind == "position":
            # Inverted axis: best (smallest) position at the TOP. Only days with
            # impressions carry a real position; 0 placeholders don't scale the axis.
            real = [v for v in vals if v > 0]
            pmin = min(real) if real else 0.0
            pmax = max(real) if real else 1.0
            span = (pmax - pmin) or 1.0

            def yfn(v, pmin=pmin, span=span):
                return None if v <= 0 else round(PT + (v - pmin) / span * plot_h, 1)

            ticks = [{"label": f"{pmin + (pmax - pmin) * k / 3:.1f}",
                      "y": round(PT + k / 3 * plot_h, 1)} for k in range(4)]
            total_fmt = f"{totals.get('position', 0) or 0:.1f}"
        else:
            mx = ((max(vals) if vals else 0) or 1) * 1.05

            def yfn(v, mx=mx):
                return round(H - PB - (v / mx) * plot_h, 1)

            if kind == "pct":
                ticks = [{"label": f"{mx * (3 - k) / 3:.1f}%",
                          "y": round(PT + k / 3 * plot_h, 1)} for k in range(4)]
                total_fmt = f"{totals.get('ctr', 0) or 0:.1f}%"
            else:
                ticks = [{"label": _kfmt(mx * (3 - k) / 3),
                          "y": round(PT + k / 3 * plot_h, 1)} for k in range(4)]
                total_fmt = _kfmt(totals.get(mkey, 0) or 0)
        ys = [yfn(v) for v in vals]
        hover_y[skey] = ys
        metrics.append({"key": mkey, "label": label, "color": color, "kind": kind,
                        "path": _path_with_gaps([(x(i), ys[i]) for i in range(n)]),
                        "ticks": ticks, "total": total_fmt})

    hover = [{"x": x(i), "date": s["date"], "clicks": s["clicks"], "impr": s["impr"],
              "ctr": round(s["ctr"], 2), "pos": round(s["pos"], 1),
              "y_clicks": hover_y["clicks"][i], "y_impr": hover_y["impr"][i],
              "y_ctr": hover_y["ctr"][i], "y_pos": hover_y["pos"][i]}
             for i, s in enumerate(series)]

    grid = [{"y": round(PT + k / 3 * plot_h, 1)} for k in range(4)]
    return {"w": W, "h": H, "metrics": metrics, "grid": grid,
            "xticks": _tl_xticks(series, n), "hover": hover, "points": n}


def _perf_directory_charts(data: dict, all_chart: dict) -> tuple:
    """Bundle the 'all' Performance chart plus one chart per URL directory (same axis and
    geometry), so the card's directory dropdown can swap the plotted series client-side
    with no page reload. Only the metrics/hover of each chart are shipped (the grid,
    x-axis ticks and viewBox are identical across directories and come from ``all``)."""
    charts = {"all": {"metrics": all_chart["metrics"], "hover": all_chart["hover"]}}
    dirs = [{"slug": "all", "label": "All directories"}]
    for slug, info in (data.get("dir_series") or {}).items():
        ch = _timeseries_charts({"days": info.get("days", [])}, info.get("totals", {}))
        if not ch:
            continue
        charts[slug] = {"metrics": ch["metrics"], "hover": ch["hover"]}
        dirs.append({"slug": slug, "label": info.get("label", slug)})
    return charts, dirs


def _resolve_payload(window=None, start=None, end=None):
    """Load the dashboard payload for a range. Returns ``(data, win_key, snapped_from,
    live_failed)`` so both the page render and the row actions read the SAME data for
    the same range — an action that re-derived it differently could act on a row the
    reader was never shown."""
    data = None
    win_key = None
    snapped_from = None
    live_failed = False

    # Live path: any exact range (presets refreshed to "now", or a free-calendar span)
    # is computed straight from the API and cached. Falls through to the committed
    # snapshot when live is disabled or a pull fails.
    live_key, live_start, live_end = _live_target(window, start, end)
    if live_key is not None:
        try:
            from . import live as gsc_live

            if gsc_live.live_enabled():
                data = gsc_live.build_range(live_start, live_end, window=live_key)
                win_key = live_key
        except Exception:
            # Live was on but the pull failed — surface it so the reader knows they are
            # looking at an older committed snapshot, not silently-stale live data.
            import logging

            logging.getLogger(__name__).exception("gsc live pull failed; serving snapshot")
            data = None
            live_failed = True

    if data is None:
        win_key, snapped_from = _resolve_window(window, start, end)
        data = _load_window(win_key)
    return data, win_key, snapped_from, live_failed


def _window_payload(window=None, start=None, end=None) -> dict:
    return _resolve_payload(window=window, start=start, end=end)[0]


def build_context(window: "str | None" = None, start: "str | None" = None,
                  end: "str | None" = None) -> dict:
    data, win_key, snapped_from, live_failed = _resolve_payload(
        window=window, start=start, end=end
    )

    insights_doc = _read(_insights_json())

    meta = data.get("meta", {})
    sc_range = {
        "window": meta.get("window", win_key),
        "start": meta.get("start", ""),
        "end": meta.get("end", ""),
        "days": meta.get("days", WINDOW_DAYS.get(win_key)),
        "label": meta.get("range_label", ""),
        "snapped_from": snapped_from,
        "live": bool(meta.get("live")),
    }
    sc_windows = [
        {"key": k, "days": d, "label": lbl, "active": k == win_key}
        for k, d, lbl in WINDOWS
    ]

    totals = data.get("totals", {})

    # Performance chart: the "all" series + a per-directory chart bundle for the dropdown.
    sc_ts = _timeseries_charts(data.get("time_series"), totals)
    perf_charts, perf_dirs = _perf_directory_charts(data, sc_ts) if sc_ts else ({}, [])

    # CTR-by-position curve: horizontal bars scaled to the best-CTR bucket
    ctr_curve = data.get("ctr_curve", [])
    peak_ctr = max((c.get("ctr_pct", 0) for c in ctr_curve), default=0)
    for c in ctr_curve:
        c["width_12"] = _twelfths(c.get("ctr_pct", 0), peak_ctr)

    # ranking-position bands: horizontal bar = query-count share of all queries
    position_bands = data.get("position_bands", [])
    peak_qpct = max((b.get("query_pct", 0) for b in position_bands), default=0)
    for b in position_bands:
        b["width_12"] = _twelfths(b.get("query_pct", 0), peak_qpct)

    # vertical grouped bars: each band's query-share across the 3 trailing 30-day windows
    bot = data.get("position_bands_over_time", {})
    time_windows = bot.get("windows", [])
    time_bars = _vgroups(
        [{"label": b["label"], "values": b.get("shares", [])} for b in bot.get("bands", [])],
        TIME_COLORS, time_windows,
    )
    time_legend = [{"label": w, "color": TIME_COLORS[i % len(TIME_COLORS)]}
                   for i, w in enumerate(time_windows)]

    # Brand share as one comparison bar table (replaces three pies): each brand is a
    # row with a keywords / impressions / clicks percentage, each bar sized against the
    # top brand for that metric so the three columns compare like-for-like.
    brand = data.get("brand_share", {})
    brand_bars = _brand_bars(brand)

    # URL-directory share (first path segment): kept as the count/% table; each row
    # carries a clicks-share bar so the distribution reads at a glance without a pie.
    directory_rows = data.get("directory_share", [])
    peak_dir_clicks = max((d.get("clicks_pct", 0) for d in directory_rows), default=0)
    for d in directory_rows:
        d["clicks_width_12"] = _twelfths(d.get("clicks_pct", 0), peak_dir_clicks)

    # per-market bars sized against the busiest market's clicks
    by_market = data.get("by_market", [])
    peak_clicks = max((m.get("clicks", 0) for m in by_market), default=0)
    for m in by_market:
        m["width_12"] = _twelfths(m.get("clicks", 0), peak_clicks)

    # high-impr/low-click rows get an impression bar + a position status tone
    hilc = data.get("high_impressions_low_clicks", [])
    peak_impr = max((r.get("impressions", 0) for r in hilc[:25]), default=0)
    for r in hilc:
        r["impr_width_12"] = _twelfths(r.get("impressions", 0), peak_impr)
        r["pos_tone"] = _position_tone(r.get("position", 0))

    # insights: fingerprint each, drop dismissed, group by category
    dismissed = load_dismissed()
    raw = insights_doc.get("insights", [])
    kept, hidden = [], 0
    for ins in raw:
        ins["fingerprint"] = insight_fingerprint(ins)
        if ins["fingerprint"] in dismissed:
            hidden += 1
            continue
        kept.append(ins)

    # dedicated-content candidates: drop permanently-excluded queries (the row-level
    # delete button in the UI writes here — every future refresh, live or snapshot,
    # re-applies this filter) and flag rows already picked so "Add to Plan" renders as
    # "Added to Plan" on load, not only after a click this session.
    #
    # "Picked" means the keyword sits in a clustering pool, NOT that a content plan
    # exists for it. A single query is not a content plan: it usually belongs in a
    # cluster with other keywords, and a plan needs a title and a brief that only the
    # intent analysis can produce. So the check follows where the button actually
    # sends it.
    excluded_dedicated = load_dedicated_excluded()
    already_picked = picked_queries()
    picks_url = config.gsc_queue_list_url()
    dedicated_total = data.get("dedicated_content_candidates_count", 0)
    dedicated = []
    for r in data.get("dedicated_content_candidates", []):
        term = (r.get("query") or "").strip().lower()
        if term in excluded_dedicated:
            dedicated_total = max(0, dedicated_total - 1)
            continue
        picked = term in already_picked
        r["already_queued"] = picked
        r["plan_url"] = picks_url if picked else ""
        dedicated.append(r)

    # By-cluster view of the same table. A cluster goes to the CLUSTERING queue, not
    # straight to ContentPlan: a cluster is a pool of keywords nobody has analysed yet,
    # so how many articles it becomes is exactly what clustering decides. Member
    # queries are dropped before the payload reaches the browser — the page never
    # needs them and a broad cluster carries hundreds.
    excluded_clusters = load_dedicated_cluster_excluded()
    dedicated_clusters = []
    for row in data.get("dedicated_by_cluster", []):
        name = str(row.get("cluster", "")).strip()
        if name.lower() in excluded_clusters:
            continue
        dedicated_clusters.append({
            k: v for k, v in row.items() if k != "queries"
        } | {
            "keywords_available": len(row.get("queries") or []),
            "already_queued": False,
            "job_url": "",
        })
    if dedicated_clusters:
        from django.utils.text import slugify

        Job = _cluster_job_model()
        if Job is not None:
            by_slug = {}
            for row in dedicated_clusters:
                by_slug.setdefault(slugify(row.get("cluster") or ""), []).append(row)
            by_slug.pop("", None)
            if by_slug:
                for job_slug, job_status in Job.objects.filter(
                    slug__in=by_slug.keys()
                ).values_list("slug", "status"):
                    for row in by_slug[job_slug]:
                        row["already_queued"] = True
                        row["job_status"] = job_status
                        row["job_url"] = picks_url

    insights_by_category = []
    for key, label, blurb in INSIGHT_CATEGORIES:
        items = sorted(
            (i for i in kept if i.get("category") == key),
            key=lambda i: PRIORITY_ORDER.get(i.get("priority", "low"), 3),
        )
        insights_by_category.append({"key": key, "label": label, "blurb": blurb, "items": items})

    return {
        "sc_meta": data.get("meta", {}),
        "sc_range": sc_range,
        "sc_windows": sc_windows,
        "sc_kpi_deltas": data.get("kpi_deltas"),
        "sc_movers": data.get("movers"),
        "sc_timeseries": sc_ts,
        "sc_perf_charts": perf_charts,
        "sc_perf_dirs": perf_dirs,
        "sc_cannibalization": data.get("cannibalization", []),
        "sc_cannibalization_count": data.get("cannibalization_count", 0),
        "sc_ctr_curve": ctr_curve,
        "sc_ctr_underperformers": data.get("ctr_underperformers", []),
        "sc_coverage": _load_coverage(),
        "sc_insights_meta": insights_doc.get("meta", {}),
        "sc_totals": totals,
        "sc_position_bands": position_bands,
        "sc_time_bars": time_bars,
        "sc_time_legend": time_legend,
        "sc_brand_bars": brand_bars,
        "sc_directory_rows": directory_rows,
        "sc_by_market": by_market,
        "sc_high_impr_low_click": hilc,
        "sc_high_impr_low_click_count": data.get("high_impressions_low_clicks_count", 0),
        "sc_hilc_by_cluster": data.get("hilc_by_cluster", []),
        "sc_hilc_by_page": data.get("hilc_by_page", []),
        "sc_new_by_cluster": data.get("new_by_cluster", []),
        "sc_dedicated_by_cluster": dedicated_clusters,
        "sc_new_queries": data.get("new_queries", []),
        "sc_top_pages": data.get("top_pages", []),
        "sc_dedicated_candidates": dedicated,
        "sc_dedicated_candidates_count": dedicated_total,
        "sc_insights_by_category": insights_by_category,
        "sc_insights_dismissed_count": hidden,
        "sc_thresholds": gsc_build.thresholds(),
        "sc_live_failed": live_failed,
        "sc_has_data": bool(data),
    }
