"""Command line: one seed to a clustered, prioritised keyword universe."""
from __future__ import annotations

import argparse
import os
import sys

from . import cluster as clustering
from .crawl import crawl
from .report import metadata, write_all
from .suggest import DEFAULT_RATE, SuggestCache, SuggestClient, egress_identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m keel_seo.keywords",
        description=(
            "Crawl one seed's complete Google-autocomplete keyword universe, "
            "cluster it by shared wording, and write it out in priority order."
        ),
        epilog=(
            "Autocomplete never returns search volume. The priority score ranks "
            "demand shape, not demand size."
        ),
    )
    parser.add_argument("seed", help="the term to build the universe around")
    parser.add_argument("--out", default=".", help="directory for the three output files")
    parser.add_argument("--levels", type=int, default=2,
                        help="re-seeding rounds after the seed itself (default 2)")
    parser.add_argument("--budget", type=int, default=12000,
                        help="hard cap on queries asked (default 12000)")
    parser.add_argument("--saturate", type=int, default=1,
                        help="drill rounds under each truncated response (default 1)")
    parser.add_argument("--frontier", type=int, default=300,
                        help="phrases re-seeded per level (default 300)")
    parser.add_argument("--threshold", type=float, default=clustering.DEFAULT_THRESHOLD,
                        help=("cluster similarity cut, 0-1 "
                              f"(default {clustering.DEFAULT_THRESHOLD}, tuned on a "
                              "4,273-phrase harvest)"))
    parser.add_argument("--workers", type=int, default=5, help="concurrent requests")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE,
                        help=(f"sustained queries per second (default {DEFAULT_RATE}). "
                              "Google blocked an unthrottled run at ~5,000 requests; "
                              "0 disables the throttle"))
    parser.add_argument("--hl", default="en", help="interface language (default en)")
    parser.add_argument("--ds", default="",
                        help="vertical: yt, sh, nws, bks. Default is web search")
    parser.add_argument("--client", default="chrome", choices=("chrome", "firefox"),
                        help="chrome returns 15 suggestions and relevance scores")
    parser.add_argument("--cache", default="",
                        help="JSONL cache path; makes a re-run nearly free")
    parser.add_argument("--no-tight", action="store_true",
                        help="skip the no-space suffix sweep (quotexa, quotexb, ...)")
    parser.add_argument("--wildcards", action="store_true",
                        help="also ask '*' templates (measured as low yield)")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    cache_path = args.cache or os.path.join(args.out, ".suggest-cache.jsonl")
    os.makedirs(args.out, exist_ok=True)

    client = SuggestClient(
        hl=args.hl,
        ds=args.ds,
        client=args.client,
        workers=args.workers,
        rate=args.rate,
        cache=SuggestCache(cache_path),
    )

    egress = egress_identity()
    progress(f"egress {egress['country']} ({egress['ip']}) — this is the harvest's "
             "geography; autocomplete ignores gl=")

    universe = crawl(
        args.seed,
        client,
        levels=args.levels,
        budget=args.budget,
        saturate=args.saturate,
        frontier_cap=args.frontier,
        tight=not args.no_tight,
        wildcards=args.wildcards,
        progress=progress,
    )
    if not universe.phrases:
        print(f"no phrases containing {args.seed!r} were returned", file=sys.stderr)
        return 1

    progress(f"clustering {len(universe.phrases)} phrases ...")
    clusters = clustering.build(universe, threshold=args.threshold)
    meta = metadata(universe, clusters, egress, client)
    paths = write_all(args.out, universe, clusters, meta)

    progress(
        f"{meta['phrases']:,} phrases · {meta['clusters']} clusters · "
        f"{meta['queries_asked']:,} queries · {meta['network_calls']:,} network calls · "
        f"{meta['elapsed_seconds']}s"
    )
    if universe.blocked:
        progress(
            f"WARNING: rate-limited after {meta['network_calls']:,} requests; "
            f"{meta['unexpanded_phrases']:,} phrases left unexpanded. The output is "
            "sound but incomplete — re-run later with a lower --rate to continue."
        )
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
