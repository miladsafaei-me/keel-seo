"""Sitemaps API client — list, read, submit and delete a property's sitemaps.

``searchconsole v1``, resource ``sitemaps``. Reading needs the readonly scope and a
Restricted user; ``submit`` and ``delete`` need the read-write scope
(``https://www.googleapis.com/auth/webmasters``) and a Full user or Owner.

What each call is actually for:

* :func:`list_sitemaps` — every sitemap Google holds for the property, each with its
  last download time, warning/error counts and per-type indexed counts. This is the
  fastest honest answer to "did Google process the sitemap we deployed?".
* :func:`submit_sitemap` — (re)submit a sitemap URL. Idempotent: submitting one that
  already exists just re-queues it, which is the correct move after a large content
  release.
* :func:`delete_sitemap` — remove a sitemap from the property. This unregisters the
  *sitemap*, and does nothing to the indexing of the URLs it listed; deleting a
  sitemap is never a way to remove a page from the index.

CLI::

    python -m keel_seo.gsc.sitemaps list
    python -m keel_seo.gsc.sitemaps get https://example.com/sitemap.xml
    python -m keel_seo.gsc.sitemaps submit https://example.com/sitemap.xml
    python -m keel_seo.gsc.sitemaps delete https://example.com/old-sitemap.xml
"""
from __future__ import annotations

import json
import sys

from .auth import SCOPE_READONLY, SCOPE_READWRITE, GscError, execute, resolve_site, service


def list_sitemaps(site: str = "", *, sitemap_index: str = "") -> list:
    """Every sitemap for the property, or the children of one sitemap index."""
    resolved = resolve_site(site)
    kwargs = {"siteUrl": resolved}
    if sitemap_index:
        kwargs["sitemapIndex"] = sitemap_index
    request = service(scopes=(SCOPE_READONLY,)).sitemaps().list(**kwargs)
    return execute(request, what="list sitemaps").get("sitemap", []) or []


def get_sitemap(feedpath: str, site: str = "") -> dict:
    """One sitemap's full record: type, contents, counts, warnings and errors."""
    request = (
        service(scopes=(SCOPE_READONLY,))
        .sitemaps()
        .get(siteUrl=resolve_site(site), feedpath=feedpath)
    )
    return execute(request, what=f"get sitemap {feedpath}")


def submit_sitemap(feedpath: str, site: str = "") -> dict:
    """Submit (or re-submit) a sitemap. Returns a result dict; the API itself
    answers with an empty body on success, so there is nothing to unwrap."""
    _require_absolute(feedpath)
    request = (
        service(scopes=(SCOPE_READWRITE,))
        .sitemaps()
        .submit(siteUrl=resolve_site(site), feedpath=feedpath)
    )
    execute(request, what=f"submit sitemap {feedpath}")
    return {"ok": True, "action": "submit", "feedpath": feedpath}


def delete_sitemap(feedpath: str, site: str = "") -> dict:
    """Unregister a sitemap from the property."""
    _require_absolute(feedpath)
    request = (
        service(scopes=(SCOPE_READWRITE,))
        .sitemaps()
        .delete(siteUrl=resolve_site(site), feedpath=feedpath)
    )
    execute(request, what=f"delete sitemap {feedpath}")
    return {"ok": True, "action": "delete", "feedpath": feedpath}


def _require_absolute(feedpath: str) -> str:
    if not feedpath or not feedpath.startswith(("http://", "https://")):
        raise GscError(
            f"a sitemap feedpath must be the sitemap's absolute URL, got: {feedpath!r}"
        )
    return feedpath


def summarize(entry: dict) -> dict:
    """Flatten one sitemap record into the fields worth reporting or storing."""
    contents = entry.get("contents", []) or []
    return {
        "path": entry.get("path", ""),
        "type": entry.get("type", ""),
        "is_index": bool(entry.get("isSitemapsIndex")),
        "is_pending": bool(entry.get("isPending")),
        "last_submitted": entry.get("lastSubmitted", ""),
        "last_downloaded": entry.get("lastDownloaded", ""),
        "warnings": int(entry.get("warnings", 0) or 0),
        "errors": int(entry.get("errors", 0) or 0),
        "submitted": sum(int(c.get("submitted", 0) or 0) for c in contents),
        "indexed": sum(int(c.get("indexed", 0) or 0) for c in contents),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="keel-seo-sitemaps", description="Search Console Sitemaps API"
    )
    parser.add_argument("--site", default="", help="property (default $GSC_SITE)")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list the property's sitemaps")
    listing.add_argument("--index", default="", help="list the children of this sitemap index")
    listing.add_argument("--json", dest="json_out")

    for name, help_text in (
        ("get", "read one sitemap's record"),
        ("submit", "submit or re-submit a sitemap"),
        ("delete", "unregister a sitemap"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("feedpath")

    args = parser.parse_args()
    try:
        if args.command == "list":
            entries = [summarize(e) for e in list_sitemaps(args.site, sitemap_index=args.index)]
            if args.json_out:
                with open(args.json_out, "w") as handle:
                    json.dump(entries, handle, indent=2)
                print(f"wrote {args.json_out}", file=sys.stderr)
            if not entries:
                print("(no sitemaps registered for this property)")
                return
            print(f"{'path':<60}  {'downloaded':<22}  {'sub':>7}  {'idx':>7}  {'warn':>5}  {'err':>4}")
            for e in entries:
                print(
                    f"{e['path']:<60}  {e['last_downloaded'] or '-':<22}  "
                    f"{e['submitted']:>7}  {e['indexed']:>7}  {e['warnings']:>5}  {e['errors']:>4}"
                )
        elif args.command == "get":
            print(json.dumps(get_sitemap(args.feedpath, args.site), indent=2))
        elif args.command == "submit":
            print(json.dumps(submit_sitemap(args.feedpath, args.site), indent=2))
        else:
            print(json.dumps(delete_sitemap(args.feedpath, args.site), indent=2))
    except GscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
