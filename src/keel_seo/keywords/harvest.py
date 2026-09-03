"""Walk a list of seeds and harvest each one's keyword universe, unattended.

**Why this lives in the package and not in a project's `ops/`.** It began as a
bash script in one consumer, which meant every other project wanting a keyword
harvest would have copied it — and with it the six things that are not obvious
and were each learned from a failure: that an external ``timeout`` kills a run
between levels and writes nothing, that two harvests on one host double every
proxy's spend, that a seed already harvested must be skipped rather than paid
for twice, that the deadline belongs to the crawl and not to the walk, that a
failed seed must not be retried in the same pass, and that the output has to be
written per seed rather than at the end of the walk. A project's `ops/` keeps
only what is genuinely its own: which seeds it cares about, and where they land.

**One-shot, not scheduled.** A seed's keyword universe is a property of the
language, not of the day: what people type into a search box does not move week
to week, so this runs when someone wants a seed harvested and then stops. It was
briefly on a weekly timer, which would have re-paid for six unchanged universes
every Sunday and, because the response cache makes a re-run nearly free, would
not even have found anything new. Ask for a refresh explicitly instead.

**Idempotent.** A seed whose output is already on disk is skipped, so pointing
this at a list that is nine-tenths done costs nine-tenths of nothing. Pass
``--refresh`` to re-harvest regardless, which is what a changed market list or a
deliberately deeper crawl wants.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time

from .report import slugify

# Six hours per seed, handed to the crawler as its own deadline rather than
# imposed from outside. The distinction is not cosmetic: an external kill lands
# between levels and the harvest writes nothing at all, which cost one completed
# six-hour run that had already found 8,513 phrases.
DEFAULT_SECONDS = 21600


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_seeds(path: str) -> list[str]:
    """One seed per line; blank lines and ``#`` comments ignored."""
    seeds: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            seed = line.split("#", 1)[0].strip()
            if seed:
                seeds.append(seed)
    return seeds


def already_harvested(out: str, seed: str) -> bool:
    """Whether this seed's workbook and record are both already on disk."""
    slug = slugify(seed)
    return all(os.path.exists(os.path.join(out, f"{slug}.{kind}"))
               for kind in ("json", "md"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m keel_seo.keywords.harvest",
        description="Harvest every seed in a list, once each, and write one "
                    "clustered universe per seed.",
    )
    parser.add_argument("--seeds", required=True, help="file of seeds, one per line")
    parser.add_argument("--out", required=True, help="directory for the outputs")
    parser.add_argument("--log", default="", help="append progress here as well as "
                                                  "to stderr")
    parser.add_argument("--markets", default="us",
                        help="markets to ask, passed straight through (default us)")
    parser.add_argument("--levels", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                        help=f"graceful deadline per seed (default {DEFAULT_SECONDS})")
    parser.add_argument("--proxies", default="auto",
                        help="'auto' rotates over the shared pool (default); 'off' "
                             "asks from this machine, which is what earns an IP-wide "
                             "block after a few thousand requests")
    parser.add_argument("--refresh", action="store_true",
                        help="re-harvest seeds that already have output")
    parser.add_argument("--extra", default="",
                        help="further arguments for the crawler, space-separated")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    def log(message: str) -> None:
        line = f"{stamp()} {message}"
        print(line, file=sys.stderr, flush=True)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    try:
        seeds = read_seeds(args.seeds)
    except OSError as exc:
        log(f"cannot read seeds: {exc}")
        return 1
    if not seeds:
        log(f"no seeds in {args.seeds} — nothing to do")
        return 0

    done = skipped = failed = 0
    for seed in seeds:
        if not args.refresh and already_harvested(args.out, seed):
            log(f"skipping {seed!r} — already harvested (use --refresh to redo)")
            skipped += 1
            continue
        command = [sys.executable, "-m", "keel_seo.keywords", seed,
                   "--out", args.out, "--markets", args.markets,
                   "--levels", str(args.levels), "--max-seconds", str(args.seconds),
                   "--proxies", args.proxies]
        if args.extra:
            command.extend(args.extra.split())
        log(f"harvesting {seed!r}")
        started = time.time()
        # The crawler's own stderr is the progress narration; let it through.
        result = subprocess.run(command)
        took = int(time.time() - started)
        if result.returncode == 0:
            done += 1
            log(f"done {seed!r} in {took}s")
        else:
            # Left for a later invocation rather than retried here: the usual
            # cause is that no proxy answered today, and asking again inside the
            # same walk changes nothing while spending the addresses that did.
            failed += 1
            log(f"FAILED {seed!r} after {took}s (exit {result.returncode}); the "
                "response cache keeps its progress for the next run")

    log(f"walk complete — {done} harvested, {skipped} already had output, "
        f"{failed} failed; output in {args.out}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    raise SystemExit(main())
