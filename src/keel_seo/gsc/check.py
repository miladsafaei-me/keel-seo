"""Preflight diagnostic — prove every Search Console capability actually works.

Setting this up spans two consoles (Google Cloud for the APIs and the key, Search
Console for the property grant) and fails in ways that all look like "403". This
module walks the whole chain once and reports, per capability, whether it is usable
and — when it is not — which of the two consoles to go fix.

Each check is deliberately the cheapest real call that proves the capability, not a
simulation: a Search Analytics pull of one row, an inspection of the property's own
home page, a sitemap listing, an Indexing API metadata read. A real call is the only
thing that catches the difference between a scope that is granted and a permission
level that is too low.

Run::

    python -m keel_seo.gsc check --site sc-domain:example.com
    python manage.py keel_seo_gsc_check        # same checks, inside Django
"""
from __future__ import annotations

import json
import sys

from . import sites as sites_api
from .auth import (
    GscError,
    cloud_project_id,
    credentials_path,
    resolve_site,
    service_account_email,
)

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


def _result(name: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def run_checks(site: str = "") -> list:
    """Every capability check, in dependency order. Returns a list of result dicts.

    Order matters: a missing key makes every later check meaningless, and a property
    the key has no permission on explains four failures at once, so each stage short-
    circuits the ones that cannot possibly pass without it.
    """
    results = []

    path = credentials_path()
    if not path.exists():
        results.append(
            _result(
                "service-account key",
                FAILED,
                f"no key at {path}",
                "Cloud console -> IAM & Admin -> Service Accounts -> Keys -> Add key (JSON), "
                "save it at that path with chmod 600 (or point $GSC_CREDENTIALS at it).",
            )
        )
        return results
    try:
        email = service_account_email()
        project = cloud_project_id()
    except GscError as exc:
        results.append(_result("service-account key", FAILED, str(exc), "Re-download the JSON key."))
        return results
    results.append(_result("service-account key", OK, f"{email} (project {project})"))

    try:
        resolved = resolve_site(site)
    except GscError as exc:
        results.append(
            _result("property configured", FAILED, str(exc), "Set $GSC_SITE or KEEL_SEO['gsc_site'].")
        )
        return results
    results.append(_result("property configured", OK, resolved))

    try:
        entries = sites_api.list_sites()
    except GscError as exc:
        results.append(
            _result(
                "Search Console API reachable",
                FAILED,
                str(exc),
                "Enable searchconsole.googleapis.com on the Cloud project, then retry.",
            )
        )
        return results
    results.append(
        _result("Search Console API reachable", OK, f"{len(entries)} propert(ies) visible to this key")
    )

    level = ""
    for entry in entries:
        if entry.get("siteUrl") == resolved:
            level = entry.get("permissionLevel", "")
            break
    if not level:
        visible = ", ".join(e.get("siteUrl", "") for e in entries) or "(none)"
        results.append(
            _result(
                "property permission",
                FAILED,
                f"{resolved} is not among the properties this key can act on. Visible: {visible}",
                "Search Console -> the property -> Settings -> Users and permissions -> Add user "
                f"-> {email} -> Owner. Check the property string matches exactly "
                "(sc-domain:example.com for a Domain property).",
            )
        )
        return results
    results.append(_result("property permission", OK, level))

    results.append(_check_analytics(resolved))
    results.append(_check_inspection(resolved, level))
    results.append(_check_sitemaps(resolved))
    results.append(_check_indexing(resolved, level))
    return results


def _check_analytics(site: str) -> dict:
    from . import analytics

    try:
        start, end = analytics.window(days=7)
        analytics.query(site, start_date=start, end_date=end, dimensions=("date",), row_limit=1)
        return _result("Search Analytics", OK, "query returned")
    except GscError as exc:
        return _result("Search Analytics", FAILED, str(exc), "Restricted permission is enough here.")


def _check_inspection(site: str, level: str) -> dict:
    from . import inspection

    probe = _home_url(site)
    if not probe:
        return _result(
            "URL Inspection",
            SKIPPED,
            f"cannot derive a probe URL from {site}",
            "Run the inspect command against a real URL to test this capability.",
        )
    try:
        raw = inspection.inspect_url(probe, site)
        summary = inspection.summarize(raw)
        return _result(
            "URL Inspection",
            OK,
            f"{probe} -> {summary['verdict'] or '(no verdict)'} / "
            f"{summary['coverage_state'] or '(no coverage state)'}",
        )
    except GscError as exc:
        return _result(
            "URL Inspection",
            FAILED,
            str(exc),
            "Needs Full or Owner permission on the property "
            f"(currently {level or 'unknown'}), plus searchconsole.googleapis.com enabled.",
        )


def _check_sitemaps(site: str) -> dict:
    from . import sitemaps

    try:
        entries = sitemaps.list_sitemaps(site)
        return _result("Sitemaps (read)", OK, f"{len(entries)} sitemap(s) registered")
    except GscError as exc:
        return _result("Sitemaps (read)", FAILED, str(exc))


def _check_indexing(site: str, level: str) -> dict:
    from . import indexing

    probe = _home_url(site)
    if not probe:
        return _result("Indexing API", SKIPPED, f"cannot derive a probe URL from {site}")
    try:
        indexing.url_status(probe)
        return _result("Indexing API", OK, "metadata read")
    except indexing.IndexingError as exc:
        text = str(exc)
        # A 404 here is the healthy answer for a URL that has simply never been
        # submitted through this Cloud project: the call authenticated, authorized
        # and reached the service, which is exactly what this check is proving.
        if "404" in text:
            return _result("Indexing API", OK, "reachable (no prior notification for the probe URL)")
        return _result(
            "Indexing API",
            FAILED,
            text,
            "Needs indexing.googleapis.com enabled AND Owner permission on the property "
            f"(currently {level or 'unknown'}).",
        )


def _home_url(site: str) -> str:
    """A probe URL inside the property: the site root.

    Domain properties (``sc-domain:example.com``) carry no scheme, so the https root
    is assumed — which is what every property here serves.
    """
    if site.startswith("sc-domain:"):
        return f"https://{site.split(':', 1)[1].strip('/')}/"
    if site.startswith(("http://", "https://")):
        return site if site.endswith("/") else site + "/"
    return ""


def format_report(results) -> str:
    """Render the checks as an aligned report with the fix under each failure."""
    lines = []
    width = max((len(r["check"]) for r in results), default=10)
    icons = {OK: "PASS", FAILED: "FAIL", SKIPPED: "SKIP"}
    for r in results:
        lines.append(f"[{icons[r['status']]}] {r['check']:<{width}}  {r['detail']}")
        if r["status"] == FAILED and r["fix"]:
            for fix_line in r["fix"].splitlines():
                lines.append(f"       fix: {fix_line}")
    failures = sum(1 for r in results if r["status"] == FAILED)
    lines.append("")
    lines.append(
        f"{len(results) - failures}/{len(results)} checks passed"
        if failures
        else f"all {len(results)} checks passed"
    )
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="keel-seo-gsc-check", description="Diagnose the whole Search Console setup"
    )
    parser.add_argument("--site", default="")
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args()

    results = run_checks(args.site)
    print(format_report(results))
    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(results, handle, indent=2)
        print(f"wrote {args.json_out}", file=sys.stderr)
    sys.exit(1 if any(r["status"] == FAILED for r in results) else 0)


if __name__ == "__main__":
    main()
