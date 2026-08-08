#!/usr/bin/env python3
"""Google Indexing API client (headless, service-account).

Submits URL-update / URL-delete notifications to Google's Web Search Indexing API
so freshly-published or newly-indexable pages get crawled sooner than sitemap
discovery alone. It shares the Search Console connector's service-account key, but
is a DIFFERENT API, scope and permission level:

* API / scope: ``indexing v3`` / ``https://www.googleapis.com/auth/indexing``
  (the connector uses ``searchconsole v1`` / ``webmasters.readonly``).
* Permission: the service account must be an *Owner* of the Search Console property
  — a Restricted or Full user is NOT enough for the Indexing API.
* The "Web Search Indexing API" must be enabled on the Cloud project.

Auth key path: ``$GSC_CREDENTIALS`` (shared with the connector), default
``~/.config/keel-seo/gsc-service-account.json``. The Google client libraries are
the ``[gsc]`` extra (``pip install 'keel-seo[gsc]'``) and are imported lazily, so a
host that never calls this carries none of that weight and importing the module is
always safe.

CLI::

    python -m keel_seo.gsc.indexing publish <url>   # notify URL_UPDATED
    python -m keel_seo.gsc.indexing remove  <url>   # notify URL_DELETED
    python -m keel_seo.gsc.indexing status  <url>   # read last-notification metadata (connection test)

NOTE: Google officially supports the Indexing API for JobPosting and BroadcastEvent
pages only; general pages are technically unsupported but widely and effectively
nudged this way. It COMPLEMENTS a correct sitemap — it never replaces it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/indexing"]
DEFAULT_CREDENTIALS = Path.home() / ".config" / "keel-seo" / "gsc-service-account.json"

URL_UPDATED = "URL_UPDATED"
URL_DELETED = "URL_DELETED"


class IndexingError(RuntimeError):
    """Raised when the Indexing API is unreachable, unconfigured, or refuses a request."""


def _credentials_path() -> Path:
    return Path(os.environ.get("GSC_CREDENTIALS", str(DEFAULT_CREDENTIALS))).expanduser()


def _service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise IndexingError(
            "google client libraries are missing; install the extra: pip install 'keel-seo[gsc]'"
        ) from exc
    path = _credentials_path()
    if not path.exists():
        raise IndexingError(
            f"service-account key not found at {path} (set $GSC_CREDENTIALS to its path)"
        )
    try:
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
    except Exception as exc:  # malformed / wrong file
        raise IndexingError(f"could not read the service-account key at {path}: {exc}") from exc
    return build("indexing", "v3", credentials=creds, cache_discovery=False)


def _require_absolute(url: str) -> str:
    if not url or not url.startswith(("http://", "https://")):
        raise IndexingError(f"url must be absolute (http/https), got: {url!r}")
    return url


def notify_url(url: str, type_: str = URL_UPDATED) -> dict:
    """Submit one URL notification. Returns a result dict; raises :class:`IndexingError`."""
    _require_absolute(url)
    svc = _service()
    try:
        resp = svc.urlNotifications().publish(body={"url": url, "type": type_}).execute()
    except Exception as exc:  # googleapiclient.errors.HttpError et al.
        raise IndexingError(_explain(exc)) from exc
    return {"ok": True, "url": url, "type": type_, "response": resp}


def notify_urls(urls, type_: str = URL_UPDATED) -> list:
    """Submit several URLs. Per-URL errors are captured (never raised) so one bad URL
    or a mid-batch quota hit cannot abort the rest. Returns a list of result dicts."""
    out = []
    svc = None
    for url in urls:
        try:
            _require_absolute(url)
            if svc is None:
                svc = _service()
            resp = svc.urlNotifications().publish(body={"url": url, "type": type_}).execute()
            out.append({"ok": True, "url": url, "type": type_, "response": resp})
        except IndexingError as exc:
            out.append({"ok": False, "url": url, "error": str(exc)})
        except Exception as exc:
            out.append({"ok": False, "url": url, "error": _explain(exc)})
    return out


def url_status(url: str) -> dict:
    """Read the last-notification metadata for a URL (read-only; a safe connection test)."""
    _require_absolute(url)
    svc = _service()
    try:
        return svc.urlNotifications().getMetadata(url=url).execute()
    except Exception as exc:
        raise IndexingError(_explain(exc)) from exc


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        return (
            "the Web Search Indexing API is not enabled on the Cloud project. Enable it at "
            "https://console.cloud.google.com/apis/library/indexing.googleapis.com then retry "
            "(propagation can take a few minutes). [" + text + "]"
        )
    if "403" in text or "PERMISSION_DENIED" in text:
        return (
            "403 permission denied — for the Indexing API the service account must be an *Owner* "
            "of the Search Console property (Settings -> Users and permissions -> Add user -> "
            "Permission: Owner). Restricted/Full is not enough. [" + text + "]"
        )
    if "404" in text:
        return "404 — no notification metadata for this URL yet (it has never been submitted). [" + text + "]"
    return text


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="keel-seo-indexing", description="Google Indexing API client")
    sub = p.add_subparsers(dest="command", required=True)
    for name, typ in (("publish", URL_UPDATED), ("remove", URL_DELETED)):
        sp = sub.add_parser(name, help=f"notify {typ}")
        sp.add_argument("url")
        sp.set_defaults(_type=typ, _read=False)
    st = sub.add_parser("status", help="read last-notification metadata (connection test)")
    st.add_argument("url")
    st.set_defaults(_read=True)

    args = p.parse_args()
    try:
        if getattr(args, "_read", False):
            print(json.dumps(url_status(args.url), indent=2))
        else:
            print(json.dumps(notify_url(args.url, args._type), indent=2, default=str))
    except IndexingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
