"""Append-only observation log, because one fetch cannot answer the real question.

A feed reports a running view total. From a single fetch, the best that can be
said about a video is how it did over its whole life - which flatters whatever
is oldest and tells you nothing about what the recommendation surface is pushing
this week. Two fetches a week apart turn the same free endpoint into a velocity
measurement, and velocity is the thing worth acting on.

So nothing here overwrites. One line per (video, fetch), appended, for ever. The
file is small by construction: fifteen videos per channel per fetch, so a weekly
run over twenty channels writes about 15,600 lines a year.

One file per channel rather than one per run, because every question asked of
this data is asked about one channel over time, and that shape makes the common
read a single open instead of a scan of every run ever recorded.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass

from .feed import Feed


@dataclass(frozen=True)
class Observation:
    """One video's view total at one moment."""

    fetched_at: str
    video_id: str
    title: str
    published: str
    views: int | None

    def fetched_datetime(self) -> dt.datetime | None:
        try:
            stamp = dt.datetime.fromisoformat(self.fetched_at)
        except (TypeError, ValueError):
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


class SnapshotStore:
    """A directory of per-channel JSONL logs."""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def path_for(self, channel_id: str) -> str:
        return os.path.join(self.root, f"{channel_id}.jsonl")

    def append(self, feed: Feed) -> int:
        """Record every video in this feed. Returns how many lines were written.

        A video whose view count is missing is still recorded, with ``None``.
        Dropping it would make a feed that briefly omitted statistics look like a
        channel that briefly deleted its videos.
        """
        path = self.path_for(feed.channel_id)
        written = 0
        with open(path, "a", encoding="utf-8") as handle:
            for video in feed.videos:
                handle.write(json.dumps({
                    "fetched_at": feed.fetched_at,
                    "video_id": video.video_id,
                    "title": video.title,
                    "published": video.published,
                    "views": video.views,
                }, ensure_ascii=False) + "\n")
                written += 1
        return written

    def observations(self, channel_id: str) -> list[Observation]:
        """Every line ever recorded for one channel, in the order written.

        A malformed line is skipped rather than raised on: the log is appended to
        by long-running unattended jobs, and one truncated write at the end of a
        killed run should not make a year of history unreadable.
        """
        path = self.path_for(channel_id)
        if not os.path.exists(path):
            return []
        rows: list[Observation] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    rows.append(Observation(
                        fetched_at=row["fetched_at"],
                        video_id=row["video_id"],
                        title=row.get("title", ""),
                        published=row.get("published", ""),
                        views=row.get("views"),
                    ))
                except (json.JSONDecodeError, KeyError):
                    continue
        return rows

    def history(self, channel_id: str) -> dict[str, list[Observation]]:
        """Per video, its observations oldest first."""
        grouped: dict[str, list[Observation]] = {}
        for row in self.observations(channel_id):
            grouped.setdefault(row.video_id, []).append(row)
        for rows in grouped.values():
            rows.sort(key=lambda row: row.fetched_at)
        return grouped

    def channels(self) -> list[str]:
        return sorted(
            name[:-len(".jsonl")]
            for name in os.listdir(self.root)
            if name.endswith(".jsonl")
        )
