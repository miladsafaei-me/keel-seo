"""Write a gap run out as JSON (the record), CSV (flat) and XLSX (to work in).

Every format carries the run's own caveat: Play has no search volume, and the
opportunity score ranks phrases inside this run only.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re

CAVEAT = ("Google Play publishes no search volume. Demand here is the installs of "
          "the apps Play ranks for a phrase; opportunity ranks phrases within this "
          "run only and is not a forecast of downloads or revenue.")

KEYWORD_COLUMNS = [
    ("phrase", 34), ("opportunity", 12), ("demand", 9), ("weakness", 10),
    ("installs_total", 15), ("installs_median", 15), ("rating_avg", 10),
    ("relevant_share", 12), ("weak_rating_share", 12), ("stale_share", 10),
    ("strong_incumbents", 11), ("unserved", 10), ("iap_share", 9), ("paid_share", 9),
    ("offered_by", 10), ("level", 7), ("reasons", 60), ("top_apps", 70),
]
APP_COLUMNS = [
    ("title", 34), ("appId", 34), ("realInstalls", 14), ("installs", 13),
    ("score", 8), ("ratings", 10), ("updated", 12), ("released", 14),
    ("offersIAP", 9), ("inAppProductPrice", 26), ("containsAds", 10),
    ("genre", 14), ("developer", 28), ("ranks_for", 60), ("url", 50),
]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "seed"


def _date(stamp) -> str:
    try:
        return dt.datetime.fromtimestamp(float(stamp), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def keyword_rows(scores, phrases) -> list[dict]:
    meta = {p.text: p for p in phrases}
    rows = []
    for s in scores:
        p = meta.get(s.phrase)
        rows.append({
            "phrase": s.phrase, "opportunity": s.opportunity,
            "demand": round(s.demand, 2), "weakness": round(s.weakness, 2),
            "installs_total": s.installs_total, "installs_median": s.installs_median,
            "rating_avg": s.rating_avg,
            "weak_rating_share": round(s.weak_rating_share, 2),
            "stale_share": round(s.stale_share, 2),
            "relevant_share": round(s.relevant_share, 2),
            "strong_incumbents": s.strong_incumbents, "unserved": s.unserved,
            "iap_share": round(s.iap_share, 2), "paid_share": round(s.paid_share, 2),
            "offered_by": p.offered_by if p else 0, "level": p.level if p else 0,
            "reasons": s.reasons,
            "top_apps": " | ".join(f"{a.get('title')} ({a.get('installs') or '?'}, "
                                   f"{round(a['score'], 1) if a.get('score') else '-'})"
                                   for a in (s.relevant or s.apps)[:5]),
        })
    return sorted(rows, key=lambda r: -r["opportunity"])


def app_rows(apps: dict, ranks: dict) -> list[dict]:
    rows = []
    for app_id, a in apps.items():
        if not a:
            continue
        row = {k: a.get(k) for k, _ in APP_COLUMNS}
        row["updated"] = _date(a.get("updated"))
        row["score"] = round(a["score"], 2) if a.get("score") else None
        row["ranks_for"] = " | ".join(f"{q} #{pos}" for q, pos in ranks.get(app_id, [])[:8])
        row["url"] = a.get("url") or f"https://play.google.com/store/apps/details?id={app_id}"
        rows.append(row)
    return sorted(rows, key=lambda r: -(r.get("realInstalls") or 0))


def write_all(outdir: str, seed: str, meta: dict, keywords: list[dict],
              apps: list[dict], complaints: list[dict], terms: list[dict]) -> dict:
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, f"{slugify(seed)}_playstore_{meta['gl']}_{meta['date']}")
    paths = {"json": base + ".json", "csv": base + ".csv", "xlsx": base + ".xlsx"}
    with open(paths["json"], "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "caveat": CAVEAT, "keywords": keywords, "apps": apps,
                   "complaints": complaints, "complaint_terms": terms},
                  handle, ensure_ascii=False, indent=1, default=str)
    with open(paths["csv"], "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[c for c, _ in KEYWORD_COLUMNS])
        writer.writeheader()
        writer.writerows(keywords)
    if not _write_xlsx(paths["xlsx"], meta, keywords, apps, complaints, terms):
        paths.pop("xlsx")
    return paths


def _write_xlsx(path, meta, keywords, apps, complaints, terms) -> bool:
    try:
        from openpyxl import Workbook  # noqa: PLC0415 - optional extra
        from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
        from openpyxl.utils import get_column_letter  # noqa: PLC0415
    except ImportError:
        return False
    book = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="3F3A8C")

    def sheet(title, columns, rows, first=False):
        ws = book.active if first else book.create_sheet()
        ws.title = title
        for i, (name, width) in enumerate(columns, 1):
            cell = ws.cell(row=1, column=i, value=name)
            cell.font, cell.fill = head_font, head_fill
            ws.column_dimensions[get_column_letter(i)].width = width
        for r, row in enumerate(rows, 2):
            for i, (name, _) in enumerate(columns, 1):
                value = row.get(name)
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                ws.cell(row=r, column=i, value=value)
        ws.freeze_panes = "B2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
        return ws

    sheet("Keywords", KEYWORD_COLUMNS, keywords, first=True)
    sheet("Apps", APP_COLUMNS, apps)
    sheet("Complaint terms", [("app", 34), ("appId", 34), ("term", 26), ("reviews", 9)], terms)
    ws = sheet("Complaints", [("app", 30), ("appId", 30), ("score", 7), ("at", 11),
                              ("thumbs", 8), ("text", 110)], complaints)
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    run = book.create_sheet("Run")
    run.column_dimensions["A"].width, run.column_dimensions["B"].width = 24, 110
    for r, (key, value) in enumerate([("caveat", CAVEAT)] + list(meta.items()), 1):
        run.cell(row=r, column=1, value=key).font = Font(bold=True)
        run.cell(row=r, column=2, value=json.dumps(value, ensure_ascii=False)
                 if isinstance(value, (list, dict)) else value)
    book.save(path)
    return True
