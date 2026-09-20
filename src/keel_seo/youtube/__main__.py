"""The YouTube research CLI: resolve channels, record them, read the verdict.

Three commands, run in that order, and the middle one is meant to be a timer.

    python -m keel_seo.youtube resolve  --channels channels.txt --out registry.json
    python -m keel_seo.youtube snapshot --registry registry.json --store snapshots/
    python -m keel_seo.youtube report   --registry registry.json --store snapshots/ --out report/

``resolve`` is run once per new competitor and never on a schedule: it turns
handles into ids and makes a human confirm each title, which is the step that
stops a squatted handle entering the registry (see :mod:`.channels`).

``snapshot`` is the one that must repeat. A single run produces a usable report,
but the column worth having - how fast a video is gaining views right now -
exists only from the second run onwards, so the value of this whole package is a
function of how long the timer has been running. Weekly is the right cadence:
often enough to catch a video the model has picked up, rare enough that the
windows stay clean.
"""
from __future__ import annotations

import argparse
import os
import sys

from .analysis import DEFAULT_MATURE_DAYS, analyse
from .channels import load_registry, resolve_many, save_registry
from .feed import FeedError, fetch_feed
from .report import write_csv, write_markdown
from .store import SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m keel_seo.youtube",
        description="Measure competitor channels from the public feeds, keylessly.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_cmd = sub.add_parser(
        "resolve", help="turn handles into verified channel ids")
    resolve_cmd.add_argument("--channels", required=True,
                             help="file of '<handle or url> | <expected name>' lines")
    resolve_cmd.add_argument("--out", required=True, help="registry JSON to write")
    resolve_cmd.add_argument("--allow-unverified", action="store_true",
                             help="write the registry even if some lines failed")

    snapshot_cmd = sub.add_parser(
        "snapshot", help="fetch every registered channel's feed and record it")
    snapshot_cmd.add_argument("--registry", required=True)
    snapshot_cmd.add_argument("--store", required=True, help="directory for the logs")

    report_cmd = sub.add_parser(
        "report", help="read the store and write the outlier tables")
    report_cmd.add_argument("--registry", required=True)
    report_cmd.add_argument("--store", required=True)
    report_cmd.add_argument("--out", required=True, help="directory for the outputs")
    report_cmd.add_argument("--mature-days", type=float, default=DEFAULT_MATURE_DAYS,
                            help=("a video younger than this is reported but kept "
                                  f"out of the baseline (default {DEFAULT_MATURE_DAYS:g})"))
    report_cmd.add_argument("--top", type=int, default=5,
                            help="videos per channel in the Markdown (default 5)")
    return parser


def cmd_resolve(args) -> int:
    """Resolve, then probe each feed, because a right title is not a live channel.

    A brand often owns several channels and abandons all but one. Both answer
    with the brand's name, so the title check passes on the dead one and the
    registry silently measures a channel nobody posts to. Measured 2026-09-21 on
    this project's first registry: two of five entries were dormant duplicates,
    and the only thing that distinguished them was that their feeds were empty.
    Probing costs one request per channel, once, at registration.
    """
    with open(args.channels, encoding="utf-8") as handle:
        channels, problems = resolve_many(handle)
    dormant = []
    for channel in channels:
        try:
            feed = fetch_feed(channel.channel_id)
            latest = max((v.published for v in feed.videos if v.published), default="")
            activity = (f"{len(feed.videos)} recent upload(s), newest {latest[:10]}"
                        if feed.videos else "EMPTY FEED — publishes nothing")
            if not feed.videos:
                dormant.append(channel.title)
        except FeedError as exc:
            activity = f"feed unreadable: {exc}"
        print(f"  {channel.channel_id}  {channel.title}")
        print(f"      {activity}")
    for problem in problems:
        print(f"  PROBLEM: {problem}", file=sys.stderr)
    if problems and not args.allow_unverified:
        print(f"\n{len(problems)} line(s) unresolved; nothing written. Fix them, or "
              f"pass --allow-unverified to keep the {len(channels)} that worked.",
              file=sys.stderr)
        return 1
    save_registry(args.out, channels)
    print(f"\n{len(channels)} channel(s) written to {args.out}")
    if dormant:
        print(f"WARNING: {len(dormant)} channel(s) publish nothing "
              f"({', '.join(dormant)}). They are in the registry and will be "
              f"measured as silent. Find the brand's active handle instead.",
              file=sys.stderr)
    return 0


def cmd_snapshot(args) -> int:
    store = SnapshotStore(args.store)
    registry = load_registry(args.registry)
    recorded = failed = 0
    for channel in registry:
        try:
            feed = fetch_feed(channel.channel_id)
        except FeedError as exc:
            print(f"  FAILED {channel.title}: {exc}", file=sys.stderr)
            failed += 1
            continue
        written = store.append(feed)
        recorded += written
        print(f"  {channel.title}: {written} video(s) recorded")
    print(f"\n{recorded} observation(s) appended to {args.store}; {failed} channel(s) failed")
    # A partial run is still progress worth keeping, so a failure is reported and
    # does not discard the channels that answered.
    return 1 if failed and not recorded else 0


def cmd_report(args) -> int:
    store = SnapshotStore(args.store)
    registry = load_registry(args.registry)
    os.makedirs(args.out, exist_ok=True)

    grouped = []
    flat = []
    for channel in registry:
        try:
            feed = fetch_feed(channel.channel_id)
        except FeedError as exc:
            print(f"  FAILED {channel.title}: {exc}", file=sys.stderr)
            continue
        baseline, verdicts = analyse(
            feed, store.history(channel.channel_id), mature_days=args.mature_days)
        grouped.append((baseline, verdicts))
        flat.extend((baseline, verdict) for verdict in verdicts)

    if not grouped:
        print("no channel answered; nothing written", file=sys.stderr)
        return 1

    csv_path = os.path.join(args.out, "youtube-outliers.csv")
    md_path = os.path.join(args.out, "youtube-outliers.md")
    write_csv(csv_path, flat)
    write_markdown(md_path, grouped, top=args.top)

    with_pace = sum(1 for _, verdict in flat if verdict.recent_views_per_day is not None)
    print(f"\n{len(flat)} video(s) across {len(grouped)} channel(s)")
    print(f"  {csv_path}")
    print(f"  {md_path}")
    if not with_pace:
        print("\nNo pace column yet: it needs a second snapshot taken at least half a "
              "day after the first. Run 'snapshot' again next week.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "resolve": cmd_resolve,
        "snapshot": cmd_snapshot,
        "report": cmd_report,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
