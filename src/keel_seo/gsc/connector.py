#!/usr/bin/env python3
"""Google Search Console connector (headless, service-account).

A small, dependency-light CLI to pull Search Analytics data (queries / pages /
clicks / impressions / CTR / position) from a verified property.

Auth: a Google Cloud *service account* JSON key, kept OUTSIDE the repo at
``$GSC_CREDENTIALS`` (default ``~/.config/keel-seo/gsc-service-account.json``). The
service account's email must be added as a user of the Search Console property
(Settings -> Users and permissions -> Add user; Restricted is enough).

Run it with a dedicated venv that has the ``[gsc]`` extra installed:
    python -m keel_seo.gsc.connector <command>

Commands:
    whoami                 print the service-account email to add in Search Console
    sites                  list the properties this service account can read (connection test)
    query [options]        run a Search Analytics query and print / export rows

Query options:
    --site SITE            property (default $GSC_SITE; required — no built-in default)
    --days N               trailing window ending 2 days ago (GSC data lag), default 28
    --start / --end DATE   explicit YYYY-MM-DD window (overrides --days)
    --dimensions LIST      comma list of query,page,country,device,date  (default: query)
    --limit N              row cap, default 100 (max 25000 per API call)
    --json PATH            also write raw rows as JSON
    --csv PATH             also write rows as CSV
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DEFAULT_CREDENTIALS = Path.home() / ".config" / "keel-seo" / "gsc-service-account.json"
# No built-in property: a host supplies its own via $GSC_SITE or --site.
DEFAULT_SITE = os.environ.get("GSC_SITE", "")
DATA_LAG_DAYS = 2  # GSC finalizes data ~2 days late; querying "today" returns partial/empty rows


def _credentials_path() -> Path:
    return Path(os.environ.get("GSC_CREDENTIALS", str(DEFAULT_CREDENTIALS))).expanduser()


class ConnectorError(RuntimeError):
    """A connector-level failure (bad/missing key, API error, bad args).

    This module is dual-purpose: a standalone CLI (``__main__``) AND a library
    imported by ``keel_seo.gsc.live`` for the dashboard's live-API path. ``_fail``
    used to call ``sys.exit`` directly, which raises ``SystemExit`` — a
    ``BaseException``, not caught by the ``except Exception`` the dashboard wraps
    every live call in. A malformed/placeholder service-account key (e.g. a `{}`
    stub, present before real GSC credentials are issued) would then abort the
    whole WSGI worker instead of the dashboard degrading to its "live pull
    failed" state. Raising here and letting ``main()`` translate to stderr+exit
    keeps the CLI UX identical while making library calls behave like normal
    Python calls.
    """


def _fail(msg: str, code: int = 1) -> "None":
    raise ConnectorError(msg)


def _require_site(site: str) -> str:
    if not site:
        _fail(
            "no property set. Pass --site (e.g. sc-domain:example.com) or export $GSC_SITE."
        )
    return site


def _load_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        _fail(
            "google client libraries are missing. Install the connector extra:\n"
            "  pip install 'keel-seo[gsc]'"
        )

    path = _credentials_path()
    if not path.exists():
        _fail(
            f"service-account key not found at {path}\n"
            "Download the JSON key from Google Cloud (IAM -> Service Accounts -> Keys)\n"
            "and save it there (chmod 600). Or point $GSC_CREDENTIALS at it."
        )
    try:
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
    except Exception as exc:  # malformed / wrong file
        _fail(f"could not read the service-account key at {path}: {exc}")
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False), creds


def _service_account_email() -> str:
    path = _credentials_path()
    if not path.exists():
        _fail(f"service-account key not found at {path}")
    try:
        return json.loads(path.read_text()).get("client_email", "(no client_email in key file)")
    except Exception as exc:
        _fail(f"could not parse the key file: {exc}")


def cmd_whoami(_args) -> None:
    print(_service_account_email())


def cmd_sites(_args) -> None:
    service, _ = _load_service()
    try:
        resp = service.sites().list().execute()
    except Exception as exc:
        _fail(_explain_api_error(exc))
    entries = resp.get("siteEntry", [])
    if not entries:
        print(
            "Connected OK, but this service account has access to NO properties yet.\n"
            f"Add its email as a user in Search Console:\n  {_service_account_email()}",
            file=sys.stderr,
        )
        return
    print("Properties this service account can read:")
    for e in entries:
        print(f"  {e.get('permissionLevel','?'):<12} {e.get('siteUrl','?')}")


def _window(args) -> "tuple[str, str]":
    if args.start or args.end:
        if not (args.start and args.end):
            _fail("--start and --end must be given together")
        return args.start, args.end
    end = dt.date.today() - dt.timedelta(days=DATA_LAG_DAYS)
    start = end - dt.timedelta(days=args.days - 1)
    return start.isoformat(), end.isoformat()


def cmd_query(args) -> None:
    site = _require_site(args.site)
    service, _ = _load_service()
    start, end = _window(args)
    dimensions = [d.strip() for d in args.dimensions.split(",") if d.strip()]
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": dimensions,
        "rowLimit": args.limit,
    }
    try:
        resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
    except Exception as exc:
        _fail(_explain_api_error(exc))
    rows = resp.get("rows", [])
    print(f"# {site}   {start} -> {end}   dims={','.join(dimensions)}   rows={len(rows)}")
    if not rows:
        print("(no rows — try a wider --days window or check the property has traffic)")
        return

    header = dimensions + ["clicks", "impressions", "ctr", "position"]
    widths = [max(len(h), 10) for h in header]
    table = []
    for r in rows:
        keys = r.get("keys", [])
        line = list(keys) + [
            str(int(r.get("clicks", 0))),
            str(int(r.get("impressions", 0))),
            f"{r.get('ctr', 0) * 100:.2f}%",
            f"{r.get('position', 0):.1f}",
        ]
        table.append(line)
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(str(cell)))

    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*header))
    for line in table:
        print(fmt.format(*[str(c) for c in line]))

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}", file=sys.stderr)
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(table)
        print(f"wrote {args.csv}", file=sys.stderr)


def _explain_api_error(exc: Exception) -> str:
    text = str(exc)
    if "403" in text:
        return (
            "403 Permission denied. Two usual causes:\n"
            "  1. The Search Console API is not enabled on the Cloud project — enable it at\n"
            "     https://console.cloud.google.com/apis/library/searchconsole.googleapis.com\n"
            "  2. The service account is not a user of the property. Add this email in\n"
            "     Search Console -> Settings -> Users and permissions -> Add user:\n"
            f"     {_service_account_email()}"
        )
    if "404" in text:
        return "404 — the --site value does not match a verified property. Run `sites` to see valid values."
    return text


def main() -> None:
    p = argparse.ArgumentParser(prog="keel-seo-gsc", description="Search Console connector")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="print the service-account email").set_defaults(func=cmd_whoami)
    sub.add_parser("sites", help="list readable properties (connection test)").set_defaults(func=cmd_sites)

    q = sub.add_parser("query", help="run a Search Analytics query")
    q.add_argument("--site", default=DEFAULT_SITE)
    q.add_argument("--days", type=int, default=28)
    q.add_argument("--start")
    q.add_argument("--end")
    q.add_argument("--dimensions", default="query")
    q.add_argument("--limit", type=int, default=100)
    q.add_argument("--json")
    q.add_argument("--csv")
    q.set_defaults(func=cmd_query)

    args = p.parse_args()
    try:
        args.func(args)
    except ConnectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
