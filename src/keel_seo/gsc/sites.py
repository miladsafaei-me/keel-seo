"""Sites API client — the properties this service account can act on.

``searchconsole v1``, resource ``sites``. ``list``/``get`` need the readonly scope;
``add``/``delete`` need the read-write scope and Owner permission.

A note on what ``add`` does, because it reads as more than it is: it registers a
property under this account, it does **not** verify it. An unverified property
returns ``siteUnverifiedUser`` as its permission level and every data call against it
fails until ownership is verified through Search Console (DNS TXT record for a Domain
property, or an HTML file / meta tag / Analytics for a URL-prefix property).
Verification itself has no public API — it is a browser step, once per property.

CLI::

    python -m keel_seo.gsc.sites list
    python -m keel_seo.gsc.sites get sc-domain:example.com
"""
from __future__ import annotations

import json
import sys

from .auth import (
    SCOPE_READONLY,
    SCOPE_READWRITE,
    GscError,
    execute,
    service,
    service_account_email,
)


def list_sites() -> list:
    """Every property this key can act on, with its permission level."""
    request = service(scopes=(SCOPE_READONLY,)).sites().list()
    return execute(request, what="list sites").get("siteEntry", []) or []


def get_site(site_url: str) -> dict:
    """One property's record (mainly its ``permissionLevel``)."""
    request = service(scopes=(SCOPE_READONLY,)).sites().get(siteUrl=site_url)
    return execute(request, what=f"get site {site_url}")


def add_site(site_url: str) -> dict:
    """Register a property under this account (does not verify it)."""
    request = service(scopes=(SCOPE_READWRITE,)).sites().add(siteUrl=site_url)
    execute(request, what=f"add site {site_url}")
    return {"ok": True, "action": "add", "site": site_url}


def delete_site(site_url: str) -> dict:
    """Unregister a property from this account."""
    request = service(scopes=(SCOPE_READWRITE,)).sites().delete(siteUrl=site_url)
    execute(request, what=f"delete site {site_url}")
    return {"ok": True, "action": "delete", "site": site_url}


def permission_level(site_url: str) -> str:
    """The permission level this key holds on a property, or "" when it holds none.

    Used by the preflight check to explain *why* a capability is unavailable: the
    difference between "not a user of this property at all" and "a user, but only
    Restricted" points at two completely different fixes.
    """
    for entry in list_sites():
        if entry.get("siteUrl") == site_url:
            return entry.get("permissionLevel", "")
    return ""


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="keel-seo-sites", description="Search Console Sites API")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list every property this key can act on")
    for name, help_text in (
        ("get", "read one property's record"),
        ("add", "register a property (does not verify it)"),
        ("delete", "unregister a property"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("site_url")

    args = parser.parse_args()
    try:
        if args.command == "list":
            entries = list_sites()
            if not entries:
                print(
                    "Connected OK, but this service account is a user of NO properties yet.\n"
                    "Add its email in Search Console -> Settings -> Users and permissions:\n"
                    f"  {service_account_email()}",
                    file=sys.stderr,
                )
                return
            print(f"{'permission':<20}  property")
            for entry in entries:
                print(f"{entry.get('permissionLevel','?'):<20}  {entry.get('siteUrl','?')}")
        elif args.command == "get":
            print(json.dumps(get_site(args.site_url), indent=2))
        elif args.command == "add":
            print(json.dumps(add_site(args.site_url), indent=2))
        else:
            print(json.dumps(delete_site(args.site_url), indent=2))
    except GscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
