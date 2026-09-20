"""Write the measurement out in the two forms it gets read in.

Markdown is for a person deciding what to film this week, so it leads with the
verdict and hides the arithmetic. CSV is for a spreadsheet or a later pass, so
it carries every number including the ones the Markdown rounds away.

Both state the baseline each multiple was computed against. A multiple without
its denominator is a number that cannot be checked, and this file's whole job is
to produce numbers somebody will act on.
"""
from __future__ import annotations

import csv
import datetime as dt

from .analysis import ChannelBaseline, VideoVerdict

COLUMNS = [
    "channel_id", "channel_title", "video_id", "title", "url", "published",
    "age_days", "views", "view_multiple", "lifetime_views_per_day",
    "recent_views_per_day", "pace_window_days", "label",
    "baseline_median_views", "baseline_mature_videos", "baseline_total_videos",
    "baseline_mature_days", "baseline_window_adapted",
]


def write_csv(path: str, rows: list[tuple[ChannelBaseline, VideoVerdict]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for baseline, verdict in rows:
            writer.writerow([
                baseline.channel_id,
                baseline.channel_title,
                verdict.video.video_id,
                verdict.title,
                verdict.url,
                verdict.video.published,
                _round(verdict.age_days, 1),
                verdict.video.views if verdict.video.views is not None else "",
                _round(verdict.view_multiple, 2),
                _round(verdict.lifetime_views_per_day, 1),
                _round(verdict.recent_views_per_day, 1),
                _round(verdict.pace_window_days, 2),
                verdict.label,
                _round(baseline.median_views, 0),
                baseline.mature_videos,
                baseline.total_videos,
                _round(baseline.mature_days, 1),
                "yes" if baseline.adapted else "no",
            ])


def _round(value: float | None, digits: int) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}" if digits else f"{value:.0f}"


def _thousands(value) -> str:
    return f"{int(value):,}" if value is not None else "—"


def write_markdown(
    path: str,
    grouped: list[tuple[ChannelBaseline, list[VideoVerdict]]],
    top: int = 5,
) -> None:
    """One section per channel, strongest videos first."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# YouTube channel outliers",
        "",
        f"Measured {stamp} from the public channel feeds. No API key was used.",
        "",
        "Every multiple below is a video's view total divided by the median total",
        "of its own channel's mature uploads, so the numbers are comparable within",
        "a channel and never between channels.",
        "",
    ]
    for baseline, verdicts in grouped:
        lines.append(f"## {baseline.channel_title or baseline.channel_id}")
        lines.append("")
        if baseline.dormant:
            lines.append(
                "The feed is empty. This channel publishes nothing, which usually "
                "means the registry holds a dormant duplicate of a brand that is "
                "active under a different handle. Replace it or drop it."
            )
            lines.append("")
            continue
        if not baseline.usable:
            lines.append(
                f"No usable baseline: {baseline.total_videos} upload(s) in the feed "
                f"but only {baseline.mature_videos} older than the maturity window, "
                f"and at least 3 are needed before a median means anything. A channel "
                f"whose whole feed is younger than the window is posting faster than "
                f"the window is wide, which is itself the finding."
            )
            lines.append("")
            continue
        window = (
            f"{baseline.mature_days:.1f} days"
            if baseline.mature_days % 1 else f"{baseline.mature_days:.0f} days")
        note = (" — narrowed from the requested window because this channel posts "
                "faster than the window is wide" if baseline.adapted else "")
        lines.append(
            f"Baseline: median {_thousands(baseline.median_views)} views across "
            f"{baseline.mature_videos} uploads older than {window}{note} "
            f"({baseline.young_videos} too young to judge)."
        )
        lines.append("")
        lines.append("| Video | Views | Multiple | Views/day now | Verdict |")
        lines.append("|---|---:|---:|---:|---|")
        for verdict in verdicts[:top]:
            pace = (f"{verdict.recent_views_per_day:,.0f}"
                    if verdict.recent_views_per_day is not None else "no history yet")
            multiple = (f"{verdict.view_multiple:.1f}x"
                        if verdict.view_multiple is not None else "—")
            title = verdict.title.replace("|", "\\|")
            lines.append(
                f"| [{title}]({verdict.url}) | {_thousands(verdict.video.views)} "
                f"| {multiple} | {pace} | {verdict.label} |"
            )
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
