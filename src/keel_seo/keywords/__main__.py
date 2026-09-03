"""Command line: one seed to a clustered, prioritised keyword universe."""
from __future__ import annotations

import argparse
import contextlib
import os
import sys

from . import cluster as clustering
from .crawl import crawl, merge_markets
from .report import metadata, write_all
from .proxying import (AVAILABLE as PROXIES_AVAILABLE, MISSING_MESSAGE,
                       PER_PROXY_PER_HOUR, PER_PROXY_PER_MINUTE, PER_PROXY_RPS,
                       DirectEgressRefused, ProxyPool, accept_suggestions,
                       harvest_lock, require_pooled_egress)
from .suggest import DEFAULT_RATE, SuggestCache, SuggestClient

# Requests in flight when a proxy pool is in use. Higher than any pool is likely
# to be: surplus workers block harmlessly in acquire(), while a shortfall leaves
# verified addresses idle and is the difference between 0.4 and 30 queries/second.
POOLED_WORKERS = 120


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
    parser.add_argument("--budget", type=int, default=150000,
                        help=("runaway guard on queries asked (default 150000). "
                              "It is NOT the plan: --levels and --frontier already "
                              "bound a run structurally (~73k at the defaults), and "
                              "a crawl normally ends by converging. Lower it only to "
                              "deliberately cut a run short"))
    parser.add_argument("--saturate", type=int, default=1,
                        help="drill rounds under each truncated response (default 1)")
    parser.add_argument("--frontier", type=int, default=300,
                        help="phrases re-seeded per level (default 300)")
    parser.add_argument("--topics", type=int, default=clustering.DEFAULT_TOPICS,
                        help=(f"how many topics to name (default "
                              f"{clustering.DEFAULT_TOPICS}). Keywords matching none "
                              "of them go to an explicit long-tail group rather than "
                              "the nearest topic"))
    parser.add_argument("--workers", type=int, default=0,
                        help=("concurrent requests. 0 (default) picks for you: 5 when "
                              "asking directly, or one per proxy (capped at 120) when "
                              "using a pool. A fixed small number is what makes a "
                              "large pool useless — see below"))
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE,
                        help=(f"sustained queries per second (default {DEFAULT_RATE}). "
                              "Google blocked an unthrottled run at ~5,000 requests; "
                              "0 disables the throttle"))
    parser.add_argument("--markets", default="us",
                        help=("comma-separated ISO-3166 alpha-2 markets to ask, "
                              "e.g. 'us,in,br' (default 'us'). Each market is a "
                              "separate crawl asked with gl=, and the outputs are "
                              "merged with per-market evidence per keyword - which "
                              "is the only honest way to say where a keyword is "
                              "searched. Cost scales with the number of markets. "
                              "Pass '' to ask no market at all, in which case "
                              "whichever address answers decides the results and "
                              "nothing in the output can name a market"))
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
    parser.add_argument("--proxies", default="auto", metavar="MODE",
                        help=("'auto' (default, and the only mode that runs) "
                              "rotates over a pool of free proxies, each validated "
                              "against the endpoint itself. Needs keel-crawler: "
                              "pip install 'keel-seo[proxies]'. 'off' would ask "
                              "from this machine and is refused: the endpoint's "
                              "block is IP-wide and costs the host far more than "
                              "the harvest is worth"))
    parser.add_argument("--proxy-want", type=int, default=60,
                        help="how many working proxies to collect (default 60)")
    parser.add_argument("--proxy-start-at", type=int, default=10,
                        help=("start crawling once this many proxies answer (default "
                              "10); the rest keep verifying in the background and join "
                              "rotation as they pass"))
    parser.add_argument("--proxy-candidates", type=int, default=900,
                        help=("how many candidates to validate to find them "
                              "(default 900; the measured hit rate is ~6%%)"))
    # The defaults are keel-crawler's, imported and never restated here. Where
    # the extra is not installed they are None and the help text says so rather
    # than printing a number this package would then own a stale copy of.
    owned = "keel-crawler's measured default" if PER_PROXY_RPS else "needs keel-seo[proxies]"
    parser.add_argument("--proxy-rps", type=float, default=PER_PROXY_RPS,
                        help=f"requests per second allowed to EACH proxy ({owned})")
    parser.add_argument("--proxy-per-minute", type=int, default=PER_PROXY_PER_MINUTE,
                        help=f"per-proxy requests per minute ({owned})")
    parser.add_argument("--proxy-per-hour", type=int, default=PER_PROXY_PER_HOUR,
                        help=f"per-proxy requests per hour ({owned})")
    parser.add_argument("--max-seconds", type=float, default=0,
                        help=("stop gracefully after this many seconds and still "
                              "write everything found (0 = no deadline). Always "
                              "prefer this to an external timeout, which kills the "
                              "run between levels and loses the whole harvest"))
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Before anything is created on disk: a run that may not ask should not leave
    # an output directory and a cache file behind as evidence that it tried.
    try:
        require_pooled_egress(args.proxies)
    except DirectEgressRefused as refusal:
        print(str(refusal), file=sys.stderr)
        return 1

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    cache_path = args.cache or os.path.join(args.out, ".suggest-cache.jsonl")
    os.makedirs(args.out, exist_ok=True)

    with contextlib.ExitStack() as stack:
        if harvest_lock is not None:
            # One spender at a time per machine. The per-address budgets that
            # keep the pool alive are enforced in memory, so a second harvest
            # running beside this one charges every address twice its limit and
            # earns the block the budgets exist to prevent. The mutex is keyed on
            # the shared store, so another project's harvest waits here too.
            progress("waiting for the host's proxy-harvest lock ...")
            stack.enter_context(harvest_lock())
            progress("holding the host's proxy-harvest lock")
        return _run(args, progress, cache_path)


def _run(args, progress, cache_path: str) -> int:
    """The harvest itself, once this machine's proxy spend is ours to make."""

    # The egress is resolved before the client is built, because it is part of
    # the cache key: a harvest must never inherit answers collected from another
    # country's exit. There is only one branch here now — asking from this
    # machine's own address is refused in main(), so the pool is the egress or
    # there is no run.
    if not PROXIES_AVAILABLE:
        print(MISSING_MESSAGE, file=sys.stderr)
        return 1
    probe_url = SuggestClient(hl=args.hl, ds=args.ds,
                              client=args.client).endpoint_url("test")
    # accept= is the endpoint-specific half: a captive portal also answers
    # 200, and would otherwise be admitted to the pool and then fail every
    # real request.
    pool = ProxyPool.build(probe_url, want=args.proxy_want,
                           start_at=args.proxy_start_at,
                           candidates=args.proxy_candidates,
                           accept=accept_suggestions, target="suggestqueries.google.com",
                           rps=args.proxy_rps, per_minute=args.proxy_per_minute,
                           per_hour=args.proxy_per_hour, progress=progress)
    if not len(pool):
        print("no proxy answered the endpoint; refusing to start a crawl that "
              "would have no egress", file=sys.stderr)
        return 1
    # A rotating pool leaves from many countries at once. That is how the
    # request volume is afforded, and - now that the market is asked for by
    # name - it has no bearing on which market the answers describe.
    egress = {"ip": "", "country": "mixed",
              "org": f"rotating pool of {len(pool)} proxies"}
    progress(f"egress mixed across {len(pool)} proxies — volume only; the "
             "market comes from --markets")

    # Concurrency has to match what the pool can carry. A pool serves one request
    # per address at a time, so N addresses support N requests in flight; a fixed
    # 5 leaves the rest idle however large the pool is. Measured: a 150-proxy pool
    # at 5 workers ran 0.4 queries/second, about 1/500th of its capacity.
    #
    # It is deliberately NOT sized from len(pool): the pool returns as soon as ten
    # addresses verify and fills the rest in the background, so reading its length
    # here samples it at its smallest and pins concurrency to that. Doing exactly
    # that produced "concurrency: 11 workers" against a pool that grew to
    # hundreds. Over-provisioning costs nothing instead — a worker with no free
    # address simply waits in acquire() until one is ready.
    workers = args.workers
    if workers <= 0:
        workers = POOLED_WORKERS
        progress(f"concurrency: {workers} workers (pooled)")

    markets = [code.strip().upper() for code in args.markets.split(",")
               if code.strip()]
    for code in markets:
        if len(code) != 2 or not code.isalpha():
            print(f"--markets takes ISO-3166 alpha-2 codes; {code!r} is not one",
                  file=sys.stderr)
            return 1
    # One shared cache across the markets: its key already carries the market, so
    # the markets cannot read each other's answers, and a re-run of any of them
    # still replays for free.
    cache = SuggestCache(cache_path, egress=egress.get("country") or "unknown")

    def build_client(market: str) -> SuggestClient:
        return SuggestClient(
            hl=args.hl,
            gl=market,
            ds=args.ds,
            client=args.client,
            workers=workers,
            rate=args.rate,
            pool=pool,
            cache=cache,
        )

    # The deadline is per market, not for the walk: a run given six hours and
    # three markets is asking for six hours of each, and a shared deadline would
    # silently starve the last one.
    per_market: dict = {}
    for code in (markets or [""]):
        if code:
            progress(f"market {code}: asking Google as gl={code.lower()}")
        client = build_client(code)
        per_market[code] = crawl(
            args.seed,
            client,
            levels=args.levels,
            budget=args.budget,
            saturate=args.saturate,
            frontier_cap=args.frontier,
            tight=not args.no_tight,
            wildcards=args.wildcards,
            max_seconds=args.max_seconds,
            progress=progress,
        )
        if code:
            progress(f"market {code}: {len(per_market[code].phrases):,} phrases")

    universe = merge_markets(args.seed, {c: u for c, u in per_market.items() if c})
    if not markets:
        universe = per_market[""]
    elif len(markets) > 1:
        progress(f"merged {len(markets)} markets -> {len(universe.phrases):,} phrases")
    if not universe.phrases:
        print(f"no phrases containing {args.seed!r} were returned", file=sys.stderr)
        return 1

    progress(f"clustering {len(universe.phrases)} phrases ...")
    clusters = clustering.build(universe, topics=args.topics)
    # metadata reads the endpoint settings off a client; every market's client
    # shares them, so the last one is as good as any - name it rather than
    # relying on the loop variable surviving.
    meta = metadata(universe, clusters, egress, build_client(markets[0] if markets else ""))
    paths = write_all(args.out, universe, clusters, meta)

    progress(
        f"{meta['phrases']:,} phrases · {meta['clusters']} clusters · "
        f"{meta['queries_asked']:,} queries · {meta['network_calls']:,} network calls · "
        f"{meta['elapsed_seconds']}s"
    )
    progress(f"proxy pool at end: {pool.stats()}")
    if universe.blocked:
        progress(
            f"WARNING: rate-limited after {meta['network_calls']:,} requests; "
            f"{meta['unexpanded_phrases']:,} phrases left unexpanded. The output is "
            "sound but incomplete — the response cache keeps it, so re-run with a "
            "larger --proxy-want to finish it."
        )
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
