"""Bring finished harvests down from the host that ran them.

**Why a pull and never a push.** The harvest runs on a server because the
endpoint blocks by IP and the block is machine-wide and long — sixteen-plus hours
measured on a laptop, which is also asleep half the week. The laptop is behind
NAT and accepts no inbound connection, so the server cannot deliver anything to
it. Every transfer has to start from the receiving side.

**Why it is safe to run repeatedly.** rsync copies only what changed, so a run
with nothing new costs one SSH round trip. Each format is written in a single
open/write/close at the end of a seed, so a partial file cannot be picked up
mid-write.

**Why this is here rather than in a project's `ops/`.** Nothing in it is
project-specific: the host, the key, and the two directories are all arguments.
Keeping it beside the harvester means the one thing that *is* specific to this
tool — which file extensions constitute a harvest — has a single definition.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# What a harvest consists of. The response cache lives in the same directory and
# is deliberately not on this list: it is tens of megabytes, it is only useful on
# the machine that will re-run the crawl, and copying it down would make every
# sync look like it had found new results.
FORMATS = ("xlsx", "json", "csv", "md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m keel_seo.keywords.sync",
        description="Copy finished keyword harvests from a remote host to a "
                    "local directory.",
    )
    parser.add_argument("--host", required=True, help="user@host running the harvest")
    parser.add_argument("--remote", required=True, help="the harvest output directory "
                                                        "on that host")
    parser.add_argument("--local", required=True, help="where to put the files here")
    parser.add_argument("--key", default="", help="SSH identity file")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(args.local, exist_ok=True)

    def say(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.key:
        ssh += ["-i", args.key]

    # An unreachable server is not an error worth failing over: a laptop on a
    # train is the normal case, and the next invocation will get the files.
    reachable = subprocess.run(ssh + [args.host, "true"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if reachable.returncode != 0:
        say("server unreachable; nothing copied")
        return 0

    includes: list[str] = ["--include=*/"]
    for suffix in FORMATS:
        includes.append(f"--include=*.{suffix}")
    # --include=*/ before the exclude, or rsync refuses to descend into any
    # subdirectory and a per-market or per-project layout arrives empty.
    command = (["rsync", "-rtu", "--out-format=%n", "-e", " ".join(ssh)]
               + includes + ["--exclude=*",
                             f"{args.host}:{args.remote.rstrip('/')}/",
                             args.local.rstrip("/") + "/"])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"rsync failed ({result.returncode}): {result.stderr.strip()}",
              file=sys.stderr)
        return result.returncode

    # rsync reports a directory whose mtime it touched as a transferred item, so
    # counting raw lines reports "1 file" on a run that copied nothing.
    changed = [line for line in result.stdout.splitlines()
               if line.strip() and not line.rstrip().endswith("/")]
    if changed:
        say(f"copied {len(changed)} file(s)")
        for name in changed:
            print(name)
    else:
        say("nothing new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
