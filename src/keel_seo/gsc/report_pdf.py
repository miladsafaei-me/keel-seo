"""The monthly client report as a PDF, in the Search Console report's look.

``render(payload, prop)`` takes :func:`keel_seo.gsc.report_api.monthly` and returns
PDF bytes. The page is ``templates/keel_seo/gsc/report_pdf.html`` with the stylesheet
and fonts in ``gsc/pdf/``; the charts are SVG drawn here, with their key values
printed on them, because a printed page cannot be hovered. WeasyPrint (the ``[pdf]``
extra) turns the HTML into the PDF and needs Pango on the host.
"""
from __future__ import annotations

import datetime as dt
from html import escape
from pathlib import Path

from . import report as R

PDF_DIR = Path(__file__).with_name("pdf")
COLORS = {"clicks": "#4285f4", "impressions": "#5e35b1", "grid": "#e8eaed", "text": "#5f6368",
          "ink": "#202124"}


class PdfUnavailable(RuntimeError):
    """WeasyPrint or its system libraries are missing on this host."""


def _text(x, y, s, anchor="start", size=10, color="text", weight=400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" '
            f'font-weight="{weight}" fill="{COLORS[color]}" font-family="Roboto">{escape(str(s))}</text>')


def daily_chart(series: list, width: int = 680, height: int = 210) -> str:
    """Clicks and impressions per day, each on its own axis, the month before dashed;
    the busiest day's clicks and impressions are printed on the lines."""
    n = len(series)
    if not n:
        return ""
    pl, pr, pt, pb = 52, 56, 24, 26
    pw, ph = width - pl - pr, height - pt - pb
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">']

    def x(i):
        return pl + (pw / 2 if n == 1 else i / (n - 1) * pw)

    for k in range(4):
        y = pt + ph * k / 3
        out.append(f'<line x1="{pl}" x2="{pl + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{COLORS["grid"]}" stroke-width="1"/>')
    for side, metric in enumerate(("clicks", "impressions")):
        vals = [d[metric] for d in series] + [(d.get("prev") or {}).get(metric, 0) for d in series]
        top = _nice(max(vals) if vals else 0)
        ax = pl - 6 if side == 0 else pl + pw + 6
        anchor = "end" if side == 0 else "start"
        out.append(_text(0 if side == 0 else width, 12, metric.capitalize(), "start" if side == 0 else "end", 10, "text", 500))
        for k in range(4):
            out.append(_text(ax, pt + ph * k / 3 + 3.5, R.compact(top * (3 - k) / 3), anchor, 9))

        def y(v, top=top):
            return pt + ph - v / top * ph

        prev = [(d.get("prev") or {}).get(metric) for d in series]
        if any(v is not None for v in prev):
            pts = " ".join(f"{x(i):.1f},{y(v or 0):.1f}" for i, v in enumerate(prev))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[metric]}" stroke-width="1.5" '
                       f'stroke-dasharray="4 3" opacity="0.6"/>')
        pts = " ".join(f"{x(i):.1f},{y(d[metric]):.1f}" for i, d in enumerate(series))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[metric]}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        peak = max(range(n), key=lambda i: series[i][metric])
        if series[peak][metric]:
            px, py = x(peak), y(series[peak][metric])
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{COLORS[metric]}"/>')
            # Clicks label above its peak, impressions below, so two peaks on one day never collide.
            ly = py - 7 if metric == "clicks" else py + 14
            out.append(_text(min(max(px, pl + 20), pl + pw - 20), ly, R.compact(series[peak][metric]), "middle", 9, "ink", 500))
    for i in sorted({0, n // 2, n - 1}):
        d = dt.date.fromisoformat(series[i]["date"])
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        out.append(_text(x(i), height - 6, f"{d:%b} {d.day}", anchor, 9))
    out.append("</svg>")
    return "".join(out)


def month_bars(months: list, metric: str, width: int = 330, height: int = 150) -> str:
    """One bar per month with its value printed above it; the last bar is the report's month."""
    n = len(months)
    if not n:
        return ""
    pl, pr, pt, pb = 4, 4, 16, 20
    pw, ph = width - pl - pr, height - pt - pb
    top = max(m[metric] for m in months) or 1
    slot = pw / n
    bw = slot * 0.62
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
           f'<line x1="{pl}" x2="{pl + pw}" y1="{pt + ph}" y2="{pt + ph}" stroke="{COLORS["grid"]}" stroke-width="1"/>']
    for i, m in enumerate(months):
        h = m[metric] / top * ph
        cx = pl + slot * i + slot / 2
        opacity = "1" if i == n - 1 else "0.45"
        out.append(f'<rect x="{cx - bw / 2:.1f}" y="{pt + ph - h:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                   f'fill="{COLORS[metric]}" opacity="{opacity}" rx="1.5"/>')
        out.append(_text(cx, pt + ph - h - 3, R.compact(m[metric]), "middle", 7, "ink", 500 if i == n - 1 else 400))
        out.append(_text(cx, height - 6, m["label"].split()[0][:3], "middle", 7))
    out.append("</svg>")
    return "".join(out)


def _nice(v: float) -> float:
    """A round axis top with three equal steps."""
    if not v or v <= 0:
        return 3
    import math

    raw = v / 3
    p = 10 ** math.floor(math.log10(raw))
    for step in (1, 2, 2.5, 5, 10):
        s = step * p
        if p >= 1 and s < 1:
            continue
        if s * 3 >= v:
            return s * 3
    return 30 * p


def _change(ch: "dict | None") -> "dict | None":
    if not ch or ch.get("value") is None:
        return {"text": "new", "tone": "flat"} if ch and ch.get("unit") == "new" and not ch.get("flat") else None
    if ch["flat"]:
        return {"text": "no change", "tone": "flat"}
    unit = {"%": "%", "pp": " pp", "places": ""}[ch["unit"]]
    arrow = "▲" if ch["good"] else "▼"
    return {"text": f"{arrow} {abs(ch['value']):g}{unit}", "tone": "up" if ch["good"] else "down"}


def _short_month(iso) -> str:
    """``2026-09-01`` -> ``Sep 2026``."""
    try:
        d = dt.date.fromisoformat(str(iso))
    except ValueError:
        return ""
    return f"{d:%b %Y}"


def context(p: dict, prop: dict, brand: "dict | None" = None) -> dict:
    """Everything report_pdf.html renders."""
    from django.utils.safestring import mark_safe

    tiles = []
    for key, label, kind in (("clicks", "Clicks", "count"), ("impressions", "Impressions", "count"),
                             ("ctr", "Average CTR", "ctr"), ("position", "Average position", "position")):
        tiles.append({
            "key": key, "label": label, "value": R.compact(p["totals"].get(key), kind),
            "prev": R.compact((p.get("prev_totals") or {}).get(key), kind) if p.get("prev_totals") else None,
            "change": _change((p.get("changes") or {}).get(key)),
            "year": R.compact((p.get("year_totals") or {}).get(key), kind) if p.get("year_totals") else None,
            "year_change": _change((p.get("year_changes") or {}).get(key)),
        })

    def rows(table, limit):
        out = []
        for r in (table or {}).get("rows", [])[:limit]:
            prev = r.get("prev") or {}
            out.append({**r, "name": r.get("label") or r["key"],
                        "delta": (r["clicks"] - prev.get("clicks", 0)) if "prev" in r else None})
        return out

    disc = p.get("discover") or {}
    logo = None
    if brand and brand.get("logo") and Path(brand["logo"]).exists():
        logo = Path(brand["logo"]).resolve().as_uri()
    return {
        "p": p, "prop": prop, "brand": brand or {}, "logo": logo,
        "compare_short": _short_month((p.get("compare") or {}).get("start")),
        "year_short": _short_month(p.get("year_start")),
        "tiles": tiles,
        "daily_chart": mark_safe(daily_chart(p["series"])),
        "clicks_bars": mark_safe(month_bars(p.get("months_trend") or [], "clicks")),
        "impressions_bars": mark_safe(month_bars(p.get("months_trend") or [], "impressions")),
        "queries": rows(p["tables"]["query"], 12),
        "pages": rows(p["tables"]["page"], 12),
        "countries": rows(p["tables"]["country"], 6),
        "devices": p.get("devices") or [],
        "device_gap": (p.get("device_gap") or [])[:6],
        "discover": disc,
        "discover_tiles": [{"label": l, "value": R.compact(disc["totals"][k], kind),
                            "change": _change((disc.get("changes") or {}).get(k))}
                           for k, l, kind in (("clicks", "Clicks", "count"), ("impressions", "Impressions", "count"),
                                              ("ctr", "CTR", "ctr"))] if disc.get("available") else [],
        "discover_pages": rows((disc.get("tables") or {}).get("page"), 6) if disc.get("available") else [],
        "striking": (p["opportunities"]["striking"] or [])[:6],
        "low_ctr": (p["opportunities"]["low_ctr"] or [])[:6],
        "brand_rows": (p.get("brand") or {}).get("rows") or [],
        "notes": R.notes(p),
        "generated": dt.date.today(),
    }


def render(p: dict, prop: dict, brand: "dict | None" = None) -> bytes:
    """The PDF bytes of one monthly report."""
    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # noqa: BLE001: missing package or missing Pango
        raise PdfUnavailable(f"PDF export needs keel-seo[pdf] and Pango on the host ({exc})") from exc
    from django.template.loader import render_to_string

    html = render_to_string("keel_seo/gsc/report_pdf.html", context(p, prop, brand))
    return HTML(string=html, base_url=str(PDF_DIR) + "/").write_pdf(
        stylesheets=[CSS(filename=str(PDF_DIR / "report.css"))])


def filename(prop: dict, month: str) -> str:
    """``wikilix-search-console-2026-10.pdf``: ASCII only."""
    import re

    base = re.sub(r"[^a-z0-9]+", "-", (prop.get("label") or prop["site"]).lower()).strip("-") or "property"
    return f"{base}-search-console-{month}.pdf"
