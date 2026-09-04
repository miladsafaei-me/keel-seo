"""Command line: one seed to a clustered, prioritised keyword universe."""
from __future__ import annotations

import argparse
import contextlib
import os
import sys

from . import cluster as clustering
from . import markets as target_markets
from .crawl import (DEFAULT_MAX_VARIANTS, PROBE_NOVELTY_FLOOR,
                    PROBE_NOVELTY_SHARE, PROBE_QUERIES, crawl, merge_markets,
                    probe_market, worth_crawling)
from .markets import ENV_NAME as MARKET_ENV, SETTING_NAME as MARKET_SETTING
from .markets import TARGET_MARKETS, UnknownMarket
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
    parser.add_argument("--markets", default=None,
                        help=("comma-separated ISO-3166 alpha-2 markets to ask, "
                              "e.g. 'us,in,br'. Default is the project's target "
                              f"markets ({len(TARGET_MARKETS)} countries: "
                              f"{', '.join(TARGET_MARKETS)}), overridable per "
                              f"project with KEEL_SEO[\"{MARKET_SETTING}\"] or the "
                              f"{MARKET_ENV} environment variable. 'target' names "
                              "that list explicitly. Each market is a separate "
                              "crawl asked with gl=, merged afterwards with "
                              "per-market evidence on every keyword - the only "
                              "honest way to say where a keyword is searched. Cost "
                              "scales with the number of markets. Pass '' to ask no "
                              "market at all, in which case whichever address "
                              "answers decides the results and nothing in the "
                              "output can name a market"))
    parser.add_argument("--hl", default="",
                        help=("interface language. Default asks each market in the "
                              "language its search is conducted in - Germany in "
                              "German, Brazil in Portuguese, India in English - "
                              "because asking a market in a language it does not "
                              "search in returns a small and unrepresentative "
                              "slice of it. Set this to ask every market in one "
                              "language"))
    parser.add_argument("--primary", default="",
                        help=("the market crawled in full and used as the "
                              "reference for every other one (default: the first "
                              "in --markets, which is US in the target list)"))
    parser.add_argument("--probe", type=int, default=PROBE_QUERIES,
                        help=(f"queries used to sample each secondary market "
                              f"before deciding whether to crawl it (default "
                              f"{PROBE_QUERIES}, 0 crawls every market in full). "
                              "Most markets return the primary market's own "
                              "answers, and crawling those is the largest "
                              "avoidable cost in a multi-market run"))
    parser.add_argument("--probe-share", type=float, default=PROBE_NOVELTY_SHARE,
                        help=(f"share of a probe's phrases that must be unseen in "
                              f"the primary market for it to earn a full crawl "
                              f"(default {PROBE_NOVELTY_SHARE:.2f})"))
    parser.add_argument("--probe-floor", type=int, default=PROBE_NOVELTY_FLOOR,
                        help=(f"and how many unseen phrases at minimum (default "
                              f"{PROBE_NOVELTY_FLOOR}). Both tests must pass: a "
                              "market returning four phrases, all new, is 100%% "
                              "novel and worth nothing"))
    parser.add_argument("--variants", default="",
                        help=("comma-separated alternative spellings of the seed to "
                              "crawl as well, e.g. 'funding pips'. Rarely needed: "
                              "the crawl finds the spellings Google itself returns"))
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS,
                        help=(f"how many discovered spellings to chase (default "
                              f"{DEFAULT_MAX_VARIANTS}, 0 disables discovery). Each "
                              "one costs a full seed-tier expansion"))
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

    try:
        markets = target_markets.resolve(args.markets)
    except UnknownMarket as bad:
        print(str(bad), file=sys.stderr)
        return 1
    if markets:
        asked = ", ".join(f"{code}/{target_markets.language_for(code, args.hl)}"
                          for code in markets)
        progress(f"markets ({len(markets)}): {asked}")
    # One shared cache across the markets: its key already carries the market, so
    # the markets cannot read each other's answers, and a re-run of any of them
    # still replays for free.
    cache = SuggestCache(cache_path, egress=egress.get("country") or "unknown")

    def build_client(market: str) -> SuggestClient:
        return SuggestClient(
            # A market is asked in the language it searches in, unless the caller
            # said otherwise. Asking Brazil in English returns the small English
            # slice of Brazilian demand and calls it Brazil.
            hl=target_markets.language_for(market, args.hl),
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
    given = tuple(v.strip() for v in args.variants.split(",") if v.strip())

    def full_crawl(code: str, variants: tuple[str, ...]):
        if code:
            language = target_markets.language_for(code, args.hl)
            progress(f"market {code}: asking Google as gl={code.lower()} hl={language}")
        return crawl(
            args.seed,
            build_client(code),
            levels=args.levels,
            budget=args.budget,
            saturate=args.saturate,
            frontier_cap=args.frontier,
            tight=not args.no_tight,
            wildcards=args.wildcards,
            max_seconds=args.max_seconds,
            variants=variants,
            max_variants=args.max_variants,
            progress=progress,
        )

    per_market: dict = {}
    probes: dict = {}
    if not markets:
        per_market[""] = full_crawl("", given)
    else:
        # The primary market is crawled in full and becomes the reference every
        # other market is measured against. It is first in the list rather than a
        # separate setting, because "the market this site is written for" is
        # already what the head of that list means.
        primary = args.primary.upper() if args.primary else markets[0]
        if primary not in markets:
            markets = [primary] + markets
        rest = [code for code in markets if code != primary]
        per_market[primary] = full_crawl(primary, given)
        progress(f"market {primary}: {len(per_market[primary].phrases):,} phrases "
                 "(primary, the reference every other market is measured against)")

        # A secondary market is sampled before it is bought. Most of them return
        # the primary market's own answers in a different accent, and crawling
        # those in full was the largest avoidable cost in a sixteen-market run.
        spellings = tuple(per_market[primary].variants)
        # The reference is what the primary answered to the SAME sample, not its
        # whole universe, so the comparison does not shift with --levels. It costs
        # nothing: the primary crawl already asked every one of these queries, so
        # all of them come back from the cache.
        reference: set = set()
        if args.probe > 0 and rest:
            reference_sample, _ = probe_market(
                args.seed, build_client(primary), (),
                queries=args.probe, tight=not args.no_tight,
                wildcards=args.wildcards, variants=spellings)
            reference = set(reference_sample.phrases)
            progress(f"probe reference: {len(reference)} phrases from {primary} "
                     f"on the same {args.probe} queries")
        earned: list[str] = []
        for code in rest:
            if args.probe <= 0:
                earned.append(code)
                continue
            language = target_markets.language_for(code, args.hl)
            sampled, verdict = probe_market(
                args.seed, build_client(code), reference,
                queries=args.probe, tight=not args.no_tight,
                wildcards=args.wildcards, variants=spellings)
            keep = worth_crawling(verdict, share=args.probe_share,
                                  floor=args.probe_floor)
            verdict["kept"] = keep
            verdict["language"] = language
            probes[code] = verdict
            progress(f"probe {code} (hl={language}): {verdict['phrases']} phrases, "
                     f"{verdict['new']} unseen ({verdict['novelty']:.0%}) — "
                     f"{'crawling in full' if keep else 'set aside'}")
            if keep:
                earned.append(code)
            elif sampled.phrases:
                # Kept, not discarded: these requests were already paid for, and
                # what they found is true even where it did not justify more.
                per_market[code] = sampled
        for code in earned:
            per_market[code] = full_crawl(code, spellings)
            progress(f"market {code}: {len(per_market[code].phrases):,} phrases")
        if probes:
            progress(f"markets: {1 + len(earned)} crawled in full, "
                     f"{len(probes) - len(earned)} probed and set aside")

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
    asked_in = ", ".join(sorted({target_markets.language_for(code, args.hl)
                                 for code in markets})) if markets else args.hl
    meta = metadata(universe, clusters, egress,
                    build_client(markets[0] if markets else ""),
                    asked_in=asked_in, probes=probes)
    meta["probe_queries"] = args.probe
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
