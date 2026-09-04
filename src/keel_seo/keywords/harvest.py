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

**A seed line may name its own spellings.** ``binary option | binary options`` is
one seed, crawled once, written to one spreadsheet with both spellings evidenced
per keyword. Spellings are a property of the seed rather than of the run, so they
live in the seed list beside it; until they did, a spelling group had to be run as
a hand-written CLI call outside the walk, which put a project's real seeds
somewhere other than its seed file.

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
from typing import NamedTuple

from .proxying import DirectEgressRefused, require_pooled_egress
from .report import slugify

# Six hours per seed, handed to the crawler as its own deadline rather than
# imposed from outside. The distinction is not cosmetic: an external kill lands
# between levels and the harvest writes nothing at all, which cost one completed
# six-hour run that had already found 8,513 phrases.
DEFAULT_SECONDS = 21600


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Seed(NamedTuple):
    """One line of the seed list: a term, and the other spellings of that term.

    The spellings belong to the seed and not to the walk, because they change
    what is crawled for one seed rather than for all of them: ``binary option``
    wants ``binary options`` and nothing else in the same list does. Handing them
    over through ``--extra`` would give every seed the same list, which is how a
    walk ends up crawling ``olymptrade`` as a spelling of ``alpari``.
    """

    term: str
    variants: tuple[str, ...] = ()


def read_seeds(path: str) -> list[Seed]:
    """One seed per line; blank lines and ``#`` comments ignored.

    A line may name the seed's other spellings after a ``|``, comma-separated::

        binary option | binary options
        trading bot   | trade bot, trading robot, trade robot
        alpari

    Those spellings are crawled as part of the same seed and land in **one**
    universe -- one row per keyword, with the other spellings named in the *Also
    written* column -- because they are one demand, and one spreadsheet is what
    anyone reading the results wants. Without this the only way to say it was a
    separate CLI call per group, outside the walk and outside the seed list, so
    the file that is supposed to hold a project's seeds did not hold them.

    The output filename comes from the term alone, so adding a spelling to a line
    never orphans the output already on disk -- but it does mean the finished
    universe no longer matches the line, and ``--refresh`` is what re-earns it.
    """
    seeds: list[Seed] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            entry = line.split("#", 1)[0].strip()
            if not entry:
                continue
            term, _, spellings = entry.partition("|")
            term = term.strip()
            if not term:
                continue
            # dict.fromkeys de-duplicates while keeping the written order, and a
            # spelling identical to the term is dropped: the crawler would drop
            # it too, but a walk that logs "also written: alpari" for the seed
            # alpari reads like a bug in the list rather than a no-op.
            variants = tuple(dict.fromkeys(
                part for part in (piece.strip() for piece in spellings.split(","))
                if part and part.casefold() != term.casefold()))
            seeds.append(Seed(term, variants))
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
    parser.add_argument("--markets", default=None,
                        help=("markets to ask, passed straight through. Left "
                              "unset, the crawler resolves the project's target "
                              "markets - the default belongs there, not in a "
                              "second copy here"))
    parser.add_argument("--levels", type=int, default=2)
    parser.add_argument("--secondary-levels", type=int, default=None,
                        help="depth for markets other than the primary; left "
                             "unset the crawler uses its own default, which is "
                             "shallower than --levels on purpose")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                        help=f"graceful deadline per seed (default {DEFAULT_SECONDS})")
    parser.add_argument("--proxies", default="auto",
                        help="'auto' rotates over the shared pool, and is the only "
                             "mode that runs; 'off' would ask from this machine and "
                             "is refused, because that is what earns an IP-wide "
                             "block after a few thousand requests")
    parser.add_argument("--refresh", action="store_true",
                        help="re-harvest seeds that already have output")
    parser.add_argument("--extra", default="",
                        help="further arguments for the crawler, space-separated")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Checked here as well as in the crawler, rather than left to the subprocess:
    # a walk over forty seeds should be refused once, at the start, not forty
    # times after it has already made an output directory for each of them.
    try:
        require_pooled_egress(args.proxies)
    except DirectEgressRefused as refusal:
        print(str(refusal), file=sys.stderr)
        return 1

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
        if not args.refresh and already_harvested(args.out, seed.term):
            log(f"skipping {seed.term!r} — already harvested (use --refresh to redo)")
            skipped += 1
            continue
        command = [sys.executable, "-m", "keel_seo.keywords", seed.term,
                   "--out", args.out,
                   "--levels", str(args.levels), "--max-seconds", str(args.seconds),
                   "--proxies", args.proxies]
        if args.markets is not None:
            command.extend(["--markets", args.markets])
        if args.secondary_levels is not None:
            command.extend(["--secondary-levels", str(args.secondary_levels)])
        if seed.variants:
            command.extend(["--variants", ",".join(seed.variants)])
        # --extra last, so a run can still override anything above it: argparse
        # takes the final occurrence of a flag.
        if args.extra:
            command.extend(args.extra.split())
        also = f" (also written: {', '.join(seed.variants)})" if seed.variants else ""
        log(f"harvesting {seed.term!r}{also}")
        started = time.time()
        # The crawler's own stderr is the progress narration; let it through.
        result = subprocess.run(command)
        took = int(time.time() - started)
        if result.returncode == 0:
            done += 1
            log(f"done {seed.term!r} in {took}s")
        else:
            # Left for a later invocation rather than retried here: the usual
            # cause is that no proxy answered today, and asking again inside the
            # same walk changes nothing while spending the addresses that did.
            failed += 1
            log(f"FAILED {seed.term!r} after {took}s (exit {result.returncode}); the "
                "response cache keeps its progress for the next run")

    log(f"walk complete — {done} harvested, {skipped} already had output, "
        f"{failed} failed; output in {args.out}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    raise SystemExit(main())
