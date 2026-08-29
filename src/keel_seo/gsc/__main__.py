"""One entry point for every Search Console capability.

Each capability keeps its own ``python -m keel_seo.gsc.<module>`` CLI; this dispatcher
exists so the whole surface is discoverable from a single command, and so a runbook
can say ``python -m keel_seo.gsc <thing>`` without the reader needing to know which
module implements it.

    python -m keel_seo.gsc check                     # diagnose the whole setup
    python -m keel_seo.gsc sites list                # properties this key can act on
    python -m keel_seo.gsc inspect url <url>         # URL Inspection API
    python -m keel_seo.gsc index publish <url>       # Indexing API
    python -m keel_seo.gsc sitemaps list             # Sitemaps API
    python -m keel_seo.gsc analytics --dimensions query,page
    python -m keel_seo.gsc registry sync             # durable query registry
    python -m keel_seo.gsc pulse --days 28           # recurring measurement engine
"""
from __future__ import annotations

import sys

COMMANDS = {
    "check": "keel_seo.gsc.check",
    "sites": "keel_seo.gsc.sites",
    "inspect": "keel_seo.gsc.inspection",
    "index": "keel_seo.gsc.indexing",
    "sitemaps": "keel_seo.gsc.sitemaps",
    "analytics": "keel_seo.gsc.analytics",
    "query": "keel_seo.gsc.connector",
    "registry": "keel_seo.gsc.registry",
    "pulse": "keel_seo.gsc.pulse",
}


def _usage() -> str:
    lines = ["usage: python -m keel_seo.gsc <command> [args...]", "", "commands:"]
    width = max(len(name) for name in COMMANDS)
    descriptions = {
        "check": "diagnose credentials, permissions and every API in one pass",
        "sites": "list / get / add / delete properties",
        "inspect": "URL Inspection API: index status, canonical, crawl, coverage",
        "index": "Indexing API: URL_UPDATED / URL_DELETED notifications",
        "sitemaps": "list / get / submit / delete sitemaps",
        "analytics": "Search Analytics with filters, types, aggregation, pagination",
        "query": "the simple Search Analytics query CLI",
        "registry": "durable query registry (sync / stats / xlsx)",
        "pulse": "recurring measurement engine (trend + deep window)",
    }
    for name in COMMANDS:
        lines.append(f"  {name:<{width}}  {descriptions[name]}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(_usage())
        sys.exit(0 if len(sys.argv) > 1 else 1)
    command = sys.argv[1]
    module_name = COMMANDS.get(command)
    if not module_name:
        print(f"unknown command: {command}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        sys.exit(1)
    from importlib import import_module

    module = import_module(module_name)
    # Hand the sub-CLI an argv that looks like its own invocation, so its argparse
    # usage strings and error messages stay correct rather than naming this wrapper.
    sys.argv = [f"python -m keel_seo.gsc {command}"] + sys.argv[2:]
    module.main()


if __name__ == "__main__":
    main()
