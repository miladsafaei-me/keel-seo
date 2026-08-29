#!/usr/bin/env python3
"""Google Indexing API client — ask Google to (re)crawl or drop a URL.

Submits URL-update / URL-delete notifications to the Web Search Indexing API so
freshly-published or newly-removed pages are picked up sooner than sitemap discovery
alone would manage. It shares the Search Console connector's service-account key, but
is a DIFFERENT API, scope and permission level:

* API / scope: ``indexing v3`` / ``https://www.googleapis.com/auth/indexing``
  (Search Console itself is ``searchconsole v1`` / ``webmasters[.readonly]``).
* Permission: the service account must be an **Owner** of the Search Console
  property — Restricted or Full is not enough for the Indexing API.
* The "Web Search Indexing API" must be enabled on the Cloud project.
* Quota: **200 publish requests per day per Cloud project** by default (not per
  property — five sites on one key share the same 200), and 600 per minute.

What ``URL_DELETED`` does and does not do
-----------------------------------------
``URL_DELETED`` tells Google the page is gone so it re-crawls sooner and drops it
from the index once it confirms the 404/410. It is a *notification*, not the
Removals tool: it cannot hide a page that still returns 200, and Google will not act
on it until a crawl confirms the removal. The Search Console **Removals** tool (the
temporary ~6-month block) has no public API at all and remains a browser-only action.
The durable removal path stays: return 410/404 (or serve ``noindex``), then notify
``URL_DELETED`` to accelerate the re-crawl. :func:`removal_guidance` states this at
the call site so a caller never mistakes one for the other.

CLI::

    python -m keel_seo.gsc.indexing publish <url>   # notify URL_UPDATED
    python -m keel_seo.gsc.indexing remove  <url>   # notify URL_DELETED
    python -m keel_seo.gsc.indexing status  <url>   # last-notification metadata
    python -m keel_seo.gsc.indexing batch urls.txt  # paced, quota-capped submission

NOTE: Google officially supports the Indexing API for JobPosting and BroadcastEvent
pages only; general pages are technically unsupported but widely and effectively
nudged this way. It COMPLEMENTS a correct sitemap — it never replaces it.
"""
from __future__ import annotations

import json
import sys
import time

from .auth import INDEXING, SCOPE_INDEXING, GscError, credentials_path, execute, service

SCOPES = [SCOPE_INDEXING]

URL_UPDATED = "URL_UPDATED"
URL_DELETED = "URL_DELETED"

DAILY_QUOTA = 200
PER_MINUTE_QUOTA = 600
MIN_INTERVAL = 60.0 / PER_MINUTE_QUOTA


class IndexingError(GscError):
    """Raised when the Indexing API is unreachable, unconfigured, or refuses a request.

    Subclasses :class:`~keel_seo.gsc.auth.GscError` so a caller can catch either the
    specific Indexing failure or every Search Console family failure uniformly,
    while existing ``except IndexingError`` call sites keep working unchanged.
    """


def _credentials_path():
    return credentials_path()


def _service():
    try:
        return service(api=INDEXING, scopes=(SCOPE_INDEXING,))
    except GscError as exc:
        raise IndexingError(str(exc)) from exc


def _require_absolute(url: str) -> str:
    if not url or not url.startswith(("http://", "https://")):
        raise IndexingError(f"url must be absolute (http/https), got: {url!r}")
    return url


def notify_url(url: str, type_: str = URL_UPDATED) -> dict:
    """Submit one URL notification. Returns a result dict; raises :class:`IndexingError`."""
    _require_absolute(url)
    svc = _service()
    try:
        resp = execute(
            svc.urlNotifications().publish(body={"url": url, "type": type_}),
            what=f"notify {type_} {url}",
        )
    except GscError as exc:
        raise IndexingError(str(exc)) from exc
    return {"ok": True, "url": url, "type": type_, "response": resp}


def remove_url(url: str) -> dict:
    """Notify ``URL_DELETED`` for a URL. See :func:`removal_guidance` for what this
    does and does not achieve."""
    return notify_url(url, URL_DELETED)


def notify_urls(urls, type_: str = URL_UPDATED, *, max_calls: int = DAILY_QUOTA,
                on_result=None) -> list:
    """Submit several URLs, paced against the per-minute quota and capped at the daily one.

    Per-URL errors are captured (never raised) so one bad URL or a mid-batch quota hit
    cannot abort the rest. ``on_result`` receives each result as it lands, so a caller
    can persist incrementally. Returns a list of result dicts.
    """
    out = []
    svc = None
    last_call = 0.0
    for index, url in enumerate(urls):
        if index >= max_calls:
            break
        try:
            _require_absolute(url)
            if svc is None:
                svc = _service()
            gap = MIN_INTERVAL - (time.monotonic() - last_call)
            if gap > 0:
                time.sleep(gap)
            last_call = time.monotonic()
            resp = execute(
                svc.urlNotifications().publish(body={"url": url, "type": type_}),
                what=f"notify {type_} {url}",
            )
            entry = {"ok": True, "url": url, "type": type_, "response": resp}
        except GscError as exc:
            entry = {"ok": False, "url": url, "type": type_, "error": str(exc)}
        except Exception as exc:
            entry = {"ok": False, "url": url, "type": type_, "error": str(exc)}
        out.append(entry)
        if on_result is not None:
            on_result(entry)
    return out


def url_status(url: str) -> dict:
    """Read the last-notification metadata for a URL (read-only; a safe connection test)."""
    _require_absolute(url)
    svc = _service()
    try:
        return execute(svc.urlNotifications().getMetadata(url=url), what=f"status {url}")
    except GscError as exc:
        raise IndexingError(_explain_missing(str(exc))) from exc


def url_statuses(urls) -> list:
    """Read last-notification metadata for several URLs, capturing per-URL errors."""
    out = []
    for url in urls:
        try:
            out.append({"ok": True, "url": url, "metadata": url_status(url)})
        except IndexingError as exc:
            out.append({"ok": False, "url": url, "error": str(exc)})
    return out


def removal_guidance(url: str) -> dict:
    """The correct removal path for a URL, alongside what the API can and cannot do.

    Returned rather than printed so a management command, a view or a report can all
    present the same statement of the boundary without restating it.
    """
    return {
        "url": url,
        "api_action": "Indexing API URL_DELETED notification (accelerates re-crawl only)",
        "requires": "the URL must already return 404/410, or serve noindex, before Google will drop it",
        "no_api": (
            "The Search Console Removals tool (temporary ~6-month block) has no public API; "
            "it stays a manual step at https://search.google.com/search-console/removals"
        ),
        "durable_path": [
            "1. Make the URL return 410 Gone (or 404), or serve a noindex robots meta tag.",
            "2. Do NOT block it in robots.txt — a blocked URL cannot be re-crawled, so Google "
            "never sees the 410/noindex and the page lingers in the index.",
            "3. Remove it from the sitemap.",
            "4. Notify URL_DELETED here to speed up the confirming crawl.",
            "5. Verify with the URL Inspection API until coverageState stops reporting it as indexed.",
        ],
    }


def _explain_missing(text: str) -> str:
    if "404" in text:
        return (
            "404 — no notification metadata for this URL yet (it has never been submitted "
            f"through this Cloud project). [{text}]"
        )
    return text


def _explain(exc: Exception) -> str:
    """Back-compatible error explainer for callers that imported it directly."""
    from .auth import explain

    return explain(exc)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="keel-seo-indexing", description="Google Indexing API client")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, typ in (("publish", URL_UPDATED), ("remove", URL_DELETED)):
        sp = sub.add_parser(name, help=f"notify {typ}")
        sp.add_argument("url")
        sp.set_defaults(_type=typ, _mode="notify")
    st = sub.add_parser("status", help="read last-notification metadata (connection test)")
    st.add_argument("url")
    st.set_defaults(_mode="status")
    ba = sub.add_parser("batch", help="notify every URL in a newline-delimited file")
    ba.add_argument("file")
    ba.add_argument("--type", dest="_type", default=URL_UPDATED, choices=[URL_UPDATED, URL_DELETED])
    ba.add_argument("--limit", type=int, default=DAILY_QUOTA)
    ba.set_defaults(_mode="batch")
    gu = sub.add_parser("removal-guidance", help="print the correct removal path for a URL")
    gu.add_argument("url")
    gu.set_defaults(_mode="guidance")

    args = parser.parse_args()
    try:
        if args._mode == "status":
            print(json.dumps(url_status(args.url), indent=2))
        elif args._mode == "guidance":
            print(json.dumps(removal_guidance(args.url), indent=2))
        elif args._mode == "batch":
            with open(args.file) as handle:
                urls = [line.strip() for line in handle if line.strip()]
            results = notify_urls(urls, args._type, max_calls=args.limit)
            ok = sum(1 for r in results if r["ok"])
            for r in results:
                if not r["ok"]:
                    print(f"FAILED {r['url']}: {r['error']}", file=sys.stderr)
            print(f"{ok}/{len(results)} notified as {args._type} (daily quota {DAILY_QUOTA})")
        else:
            print(json.dumps(notify_url(args.url, args._type), indent=2, default=str))
            if args._type == URL_DELETED:
                print(
                    "\nnote: URL_DELETED only accelerates a re-crawl. The page must already "
                    "return 404/410 or noindex — run `removal-guidance` for the full path.",
                    file=sys.stderr,
                )
    except IndexingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
