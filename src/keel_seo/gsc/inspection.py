"""URL Inspection API client — what Google actually knows about one of your URLs.

This is the API behind the "URL Inspection" panel in the Search Console UI:
``searchconsole v1``, method ``urlInspection.index.inspect``. It answers the
questions a sitemap and an analytics query cannot:

* Is this URL in the index at all, and if not, why not (``coverageState``)?
* Which URL does Google consider canonical for it, and does that match ours?
* When was it last crawled, with which user agent, and did the fetch succeed?
* Does robots.txt allow it? Is it excluded by a ``noindex``?
* Which sitemaps and which referring pages did Google discover it through?
* Do its mobile-usability, rich-result and AMP checks pass?

Two hard limits shape every batch here: **2,000 inspections per property per day**
and **600 per minute**. :func:`inspect_urls` paces itself against both and stops
cleanly at the daily cap rather than hammering into 429s, because a burned daily
quota costs a whole day of visibility.

Permission: the service account must be a **Full user or Owner** of the property.
Restricted is enough for Search Analytics but *not* for inspection.

CLI::

    python -m keel_seo.gsc.inspection url https://example.com/page/
    python -m keel_seo.gsc.inspection urls urls.txt --json out.json
"""
from __future__ import annotations

import json
import sys
import time

from .auth import SCOPE_READONLY, GscError, execute, resolve_site, service

DAILY_QUOTA = 2000
PER_MINUTE_QUOTA = 600
MIN_INTERVAL = 60.0 / PER_MINUTE_QUOTA

# The verdicts Google returns in every *Result block. PASS means the check succeeded;
# a URL can be PASS on mobile usability while being NEUTRAL (not indexed) overall.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_NEUTRAL = "NEUTRAL"

# coverageState strings that mean the URL is genuinely in the index. Google returns
# free text here and adds new phrasings over time, so INDEXED_STATES is a fast path
# and is_indexed() falls back to a substring test rather than pretending the list is
# exhaustive.
INDEXED_STATES = {
    "Submitted and indexed",
    "Indexed, not submitted in sitemap",
    "Indexed, low interest",
}


def _service():
    return service(scopes=(SCOPE_READONLY,))


def inspect_url(url: str, site: str = "", *, language_code: str = "en-US") -> dict:
    """Inspect one URL. Returns the raw ``inspectionResult`` payload.

    ``url`` must be inside ``site`` — the API refuses a URL outside the property it
    is asked about, which is the single most common 403 here.
    """
    if not url or not url.startswith(("http://", "https://")):
        raise GscError(f"inspectionUrl must be absolute (http/https), got: {url!r}")
    resolved_site = resolve_site(site)
    body = {"inspectionUrl": url, "siteUrl": resolved_site, "languageCode": language_code}
    request = _service().urlInspection().index().inspect(body=body)
    resp = execute(request, what=f"inspect {url}")
    return resp.get("inspectionResult", {})


def is_indexed(summary: dict) -> bool:
    """Whether a summarized result describes a URL that is actually in the index."""
    state = (summary.get("coverage_state") or "").strip()
    if state in INDEXED_STATES:
        return True
    lowered = state.lower()
    return "indexed" in lowered and "not indexed" not in lowered


def summarize(result: dict) -> dict:
    """Flatten a raw ``inspectionResult`` into the fields worth storing and reporting.

    The raw payload nests four independent verdict blocks under an outer result and
    uses camelCase; everything downstream (the model, the management command, a
    report) wants one flat snake_case record. Missing blocks collapse to empty values
    rather than None-checks at every call site: Google omits ``ampResult`` entirely
    for a non-AMP page, and omits ``richResultsResult`` for a page with no structured
    data, and neither absence is an error.
    """
    index_status = result.get("indexStatusResult", {}) or {}
    mobile = result.get("mobileUsabilityResult", {}) or {}
    rich = result.get("richResultsResult", {}) or {}
    amp = result.get("ampResult", {}) or {}

    summary = {
        "verdict": index_status.get("verdict", ""),
        "coverage_state": index_status.get("coverageState", ""),
        "indexing_state": index_status.get("indexingState", ""),
        "robots_txt_state": index_status.get("robotsTxtState", ""),
        "page_fetch_state": index_status.get("pageFetchState", ""),
        "last_crawl_time": index_status.get("lastCrawlTime", ""),
        "crawled_as": index_status.get("crawledAs", ""),
        "google_canonical": index_status.get("googleCanonical", ""),
        "user_canonical": index_status.get("userCanonical", ""),
        "sitemaps": list(index_status.get("sitemap", []) or []),
        "referring_urls": list(index_status.get("referringUrls", []) or []),
        "mobile_verdict": mobile.get("verdict", ""),
        "mobile_issues": [i.get("message", "") for i in (mobile.get("issues") or [])],
        "rich_results_verdict": rich.get("verdict", ""),
        "rich_results_types": [
            item.get("richResultType", "") for item in (rich.get("detectedItems") or [])
        ],
        "amp_verdict": amp.get("verdict", ""),
        "amp_url": amp.get("ampUrl", ""),
        "inspection_link": result.get("inspectionResultLink", ""),
    }
    summary["indexed"] = is_indexed(summary)
    # A canonical mismatch is the quiet killer: the page is fetched fine, reports no
    # error, and simply consolidates its signals into a different URL. Surface it as
    # its own flag so a report can rank it above cosmetic verdicts.
    google_canonical = summary["google_canonical"]
    user_canonical = summary["user_canonical"]
    summary["canonical_mismatch"] = bool(
        google_canonical and user_canonical and google_canonical != user_canonical
    )
    return summary


def inspect_urls(urls, site: str = "", *, language_code: str = "en-US",
                 max_calls: int = DAILY_QUOTA, on_result=None) -> list:
    """Inspect many URLs, pacing against both published quotas.

    Per-URL failures are captured, never raised, so one URL outside the property (or
    one transient refusal that outlived its retries) cannot abort a 500-URL sweep.
    Each entry is ``{"url", "ok", "summary"|"error", "result"}``; ``on_result`` is
    called with each entry as it lands, so a caller can persist incrementally and
    keep whatever a mid-run interruption already paid for.
    """
    results = []
    resolved_site = resolve_site(site)
    last_call = 0.0
    for index, url in enumerate(urls):
        if index >= max_calls:
            break
        gap = MIN_INTERVAL - (time.monotonic() - last_call)
        if gap > 0:
            time.sleep(gap)
        last_call = time.monotonic()
        try:
            raw = inspect_url(url, resolved_site, language_code=language_code)
            entry = {"url": url, "ok": True, "result": raw, "summary": summarize(raw)}
        except GscError as exc:
            entry = {"url": url, "ok": False, "error": str(exc)}
        except Exception as exc:
            entry = {"url": url, "ok": False, "error": str(exc)}
        results.append(entry)
        if on_result is not None:
            on_result(entry)
    return results


def coverage_report(entries) -> dict:
    """Aggregate inspection entries into the counts a sweep should report."""
    report = {
        "total": len(entries),
        "ok": 0,
        "failed": 0,
        "indexed": 0,
        "not_indexed": 0,
        "canonical_mismatch": 0,
        "robots_blocked": 0,
        "fetch_problem": 0,
        "coverage_states": {},
    }
    for entry in entries:
        if not entry.get("ok"):
            report["failed"] += 1
            continue
        report["ok"] += 1
        summary = entry.get("summary", {})
        if summary.get("indexed"):
            report["indexed"] += 1
        else:
            report["not_indexed"] += 1
        if summary.get("canonical_mismatch"):
            report["canonical_mismatch"] += 1
        if summary.get("robots_txt_state") not in ("", "ALLOWED"):
            report["robots_blocked"] += 1
        if summary.get("page_fetch_state") not in ("", "SUCCESSFUL"):
            report["fetch_problem"] += 1
        state = summary.get("coverage_state") or "(unknown)"
        report["coverage_states"][state] = report["coverage_states"].get(state, 0) + 1
    return report


def _print_summary(url: str, summary: dict) -> None:
    print(f"# {url}")
    rows = [
        ("verdict", summary["verdict"]),
        ("coverage", summary["coverage_state"]),
        ("indexed", "yes" if summary["indexed"] else "no"),
        ("indexing state", summary["indexing_state"]),
        ("robots.txt", summary["robots_txt_state"]),
        ("page fetch", summary["page_fetch_state"]),
        ("last crawl", summary["last_crawl_time"]),
        ("crawled as", summary["crawled_as"]),
        ("google canonical", summary["google_canonical"]),
        ("user canonical", summary["user_canonical"]),
        ("canonical match", "no" if summary["canonical_mismatch"] else "yes"),
        ("sitemaps", ", ".join(summary["sitemaps"]) or "-"),
        ("mobile", summary["mobile_verdict"] or "-"),
        ("rich results", summary["rich_results_verdict"] or "-"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label:<{width}}  {value or '-'}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="keel-seo-inspect", description="Google URL Inspection API client"
    )
    parser.add_argument("--site", default="", help="property (default $GSC_SITE)")
    parser.add_argument("--language", default="en-US")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("url", help="inspect a single URL")
    one.add_argument("url")
    one.add_argument("--raw", action="store_true", help="print the full API payload")

    many = sub.add_parser("urls", help="inspect every URL in a newline-delimited file")
    many.add_argument("file")
    many.add_argument("--limit", type=int, default=DAILY_QUOTA)
    many.add_argument("--json", dest="json_out")

    args = parser.parse_args()
    try:
        if args.command == "url":
            raw = inspect_url(args.url, args.site, language_code=args.language)
            if args.raw:
                print(json.dumps(raw, indent=2))
            else:
                _print_summary(args.url, summarize(raw))
        else:
            with open(args.file) as handle:
                urls = [line.strip() for line in handle if line.strip()]
            entries = inspect_urls(
                urls, args.site, language_code=args.language, max_calls=args.limit
            )
            for entry in entries:
                if entry["ok"]:
                    _print_summary(entry["url"], entry["summary"])
                else:
                    print(f"# {entry['url']}\n  ERROR {entry['error']}")
            report = coverage_report(entries)
            print(
                f"\n{report['total']} inspected  "
                f"indexed={report['indexed']}  not-indexed={report['not_indexed']}  "
                f"canonical-mismatch={report['canonical_mismatch']}  failed={report['failed']}"
            )
            if args.json_out:
                with open(args.json_out, "w") as handle:
                    json.dump(entries, handle, indent=2)
                print(f"wrote {args.json_out}", file=sys.stderr)
    except GscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
