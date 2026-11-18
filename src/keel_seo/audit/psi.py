"""PageSpeed numbers for one sample page per family.

Calls Google's public PageSpeed Insights API, which fetches the page from
Google's side, so nothing here crawls. One page per family, the one with the
most impressions, is enough to judge a template; the field data block also
carries the whole origin's Core Web Vitals where Chrome has enough visits.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .store import Workspace

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
FIELD = {"LARGEST_CONTENTFUL_PAINT_MS": "lcp_ms", "INTERACTION_TO_NEXT_PAINT": "inp_ms",
         "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls_x100", "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb_ms",
         "FIRST_CONTENTFUL_PAINT_MS": "fcp_ms"}
LAB = {"largest-contentful-paint": "lcp_ms", "total-blocking-time": "tbt_ms", "cumulative-layout-shift": "cls",
       "speed-index": "speed_index_ms", "server-response-time": "ttfb_ms", "total-byte-weight": "bytes"}


def _field(block: dict) -> dict:
    metrics = (block or {}).get("metrics", {})
    return {name: {"p75": metrics[key].get("percentile"), "rating": metrics[key].get("category")}
            for key, name in FIELD.items() if key in metrics}


def _measure(url: str) -> dict:
    query = urllib.parse.urlencode({"url": url, "strategy": "mobile", "category": "performance"})
    with urllib.request.urlopen(f"{API}?{query}", timeout=120) as response:
        data = json.load(response)
    lighthouse = data.get("lighthouseResult", {})
    audits = lighthouse.get("audits", {})
    opportunities = sorted(
        ({"id": key, "title": audit.get("title"), "savings_ms": round(audit.get("details", {}).get("overallSavingsMs", 0))}
         for key, audit in audits.items() if audit.get("details", {}).get("overallSavingsMs", 0) > 100),
        key=lambda item: -item["savings_ms"])[:6]
    return {
        "field_page": _field(data.get("loadingExperience")),
        "field_origin": _field(data.get("originLoadingExperience")),
        "lab_score": round((lighthouse.get("categories", {}).get("performance", {}).get("score") or 0) * 100),
        "lab": {name: round(audits[key]["numericValue"], 3) for key, name in LAB.items()
                if key in audits and "numericValue" in audits[key]},
        "opportunities": opportunities,
    }


def run(workspace: Workspace, *, max_pages: int = 12) -> dict:
    conn = workspace.db()
    rows = conn.execute(
        "SELECT family, url, MAX(impr_3m) FROM inventory WHERE indexable=1 GROUP BY family "
        "ORDER BY SUM(impr_3m) DESC, COUNT(*) DESC LIMIT ?", (max_pages,)).fetchall()
    done, failed = 0, []
    for row in rows:
        try:
            payload = _measure(row["url"])
        except Exception as exc:  # noqa: BLE001 - one page failing must not stop the rest
            failed.append({"url": row["url"], "error": str(exc)[:160]})
            time.sleep(5)
            continue
        conn.execute("INSERT OR REPLACE INTO psi(url, family, payload) VALUES (?,?,?)",
                     (row["url"], row["family"], json.dumps(payload)))
        conn.commit()
        done += 1
    conn.close()
    return {"measured": done, "failed": failed}
