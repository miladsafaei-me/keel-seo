"""CLI of the audit engine: ``python -m keel_seo.audit <command> <workspace>``."""
from __future__ import annotations

import argparse
import json
import sys

from .store import STARTER_PROJECT, Workspace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m keel_seo.audit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, text: str):
        command = sub.add_parser(name, help=text)
        command.add_argument("workspace", help="the audit workspace folder")
        return command

    add("init", "write a starter project.json")
    crawl = add("crawl", "discover and crawl the site through the proxy pool")
    crawl.add_argument("--refresh", action="store_true", help="fetch every known URL again")
    crawl.add_argument("--limit", type=int, default=0, help="fetch at most this many URLs, without following links")
    add("gsc", "pull Search Console data")
    psi = add("psi", "PageSpeed numbers for one sample page per family")
    psi.add_argument("--max", type=int, default=12)
    add("build", "build families, the inventory and every candidate list")
    facts = add("facts", "print a module's fact file, or one page's record")
    facts.add_argument("module", help="a module name, 'overview', 'family' or 'page'")
    facts.add_argument("target", nargs="?", default="", help="a URL for 'page', a family for 'family'")
    facts.add_argument("--limit", type=int, default=40)
    serp = add("serp-plan", "list the priority queries for a SERP lookup")
    serp.add_argument("--max", type=int, default=25)
    findings = add("findings", "validate agent findings and reconcile them per URL")
    findings.add_argument("action", choices=("validate", "reconcile", "export"))
    findings.add_argument("module", nargs="?", default="")

    args = parser.parse_args(argv)
    workspace = Workspace(args.workspace)

    if args.command == "init":
        workspace.ensure()
        if workspace.project_path.exists():
            print(f"{workspace.project_path} already exists", file=sys.stderr)
            return 1
        workspace.project_path.write_text(json.dumps(STARTER_PROJECT, indent=2) + "\n", encoding="utf-8")
        print(workspace.project_path)
        return 0
    if args.command == "crawl":
        from . import crawl as crawl_module
        result = crawl_module.run(workspace, refresh=args.refresh, limit=args.limit)
    elif args.command == "gsc":
        from . import gscdata
        result = gscdata.run(workspace)
    elif args.command == "psi":
        from . import psi as psi_module
        result = psi_module.run(workspace, max_pages=args.max)
    elif args.command == "build":
        from . import analyze
        result = analyze.run(workspace)
    elif args.command == "facts":
        from . import facts as facts_module
        result = facts_module.show(workspace, args.module, args.target, limit=args.limit)
    elif args.command == "serp-plan":
        from . import facts as facts_module
        result = facts_module.serp_plan(workspace, maximum=args.max)
    else:
        from . import findings as findings_module
        result = findings_module.run(workspace, args.action, args.module)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
