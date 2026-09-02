"""Command line: one seed to a clustered, prioritised keyword universe."""
from __future__ import annotations

import argparse
import os
import sys

from . import cluster as clustering
from .crawl import crawl
from .report import metadata, write_all
from .proxies import (PER_PROXY_PER_HOUR, PER_PROXY_PER_MINUTE, PER_PROXY_RPS,
                      ProxyPool)
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
    parser.add_argument("--proxies", default="off", metavar="MODE",
                        help=("'off' (default) asks directly from this machine; "
                              "'auto' builds a rotating pool of free proxies, each "
                              "validated against the endpoint itself. Use it when "
                              "this IP is blocked, or to keep from blocking it"))
    parser.add_argument("--proxy-want", type=int, default=60,
                        help="how many working proxies to collect (default 60)")
    parser.add_argument("--proxy-candidates", type=int, default=900,
                        help=("how many candidates to validate to find them "
                              "(default 900; the measured hit rate is ~6%%)"))
    parser.add_argument("--proxy-rps", type=float, default=PER_PROXY_RPS,
                        help=(f"requests per second allowed to EACH proxy "
                              f"(default {PER_PROXY_RPS}, i.e. one per "
                              f"{1 / PER_PROXY_RPS:.0f}s)"))
    parser.add_argument("--proxy-per-minute", type=int, default=PER_PROXY_PER_MINUTE,
                        help=f"per-proxy requests per minute (default {PER_PROXY_PER_MINUTE})")
    parser.add_argument("--proxy-per-hour", type=int, default=PER_PROXY_PER_HOUR,
                        help=f"per-proxy requests per hour (default {PER_PROXY_PER_HOUR})")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    cache_path = args.cache or os.path.join(args.out, ".suggest-cache.jsonl")
    os.makedirs(args.out, exist_ok=True)

    # The egress is resolved before the client is built, because it is part of
    # the cache key: a harvest must never inherit answers collected from another
    # country's exit.
    pool = None
    if args.proxies != "off":
        probe_url = SuggestClient(hl=args.hl, ds=args.ds,
                                  client=args.client).endpoint_url("test")
        pool = ProxyPool.build(probe_url, want=args.proxy_want,
                               candidates=args.proxy_candidates,
                               rps=args.proxy_rps, per_minute=args.proxy_per_minute,
                               per_hour=args.proxy_per_hour, progress=progress)
        if not len(pool):
            print("no proxy answered the endpoint; refusing to start a crawl that "
                  "would have no egress", file=sys.stderr)
            return 1
        # A rotating pool leaves from many countries at once, so this harvest has
        # no single geography and must not claim one.
        egress = {"ip": "", "country": "mixed",
                  "org": f"rotating pool of {len(pool)} proxies"}
        progress(f"egress mixed across {len(pool)} proxies — this harvest is "
                 "deliberately multi-country")
    else:
        egress = egress_identity()
        progress(f"egress {egress['country']} ({egress['ip']}) — this is the harvest's "
                 "geography; autocomplete ignores gl=")

    client = SuggestClient(
        hl=args.hl,
        ds=args.ds,
        client=args.client,
        workers=args.workers,
        rate=args.rate,
        pool=pool,
        cache=SuggestCache(cache_path, egress=egress.get("country") or "unknown"),
    )

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
    if pool is not None:
        progress(f"proxy pool at end: {pool.stats()}")
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
