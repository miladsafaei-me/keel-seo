"""The keyless half: a channel's fifteen most recent uploads, with view counts.

``/feeds/videos.xml?channel_id=...`` needs no key, no quota and no account. It
returns the fifteen newest uploads with a title, a publish time and - the part
that makes it a research instrument rather than a notification feed - a view
count per video, inside ``media:community/media:statistics``.

Fifteen is the whole feed and there is no paging parameter, so this is a
*recent* window and never a back catalogue. That bound is a good fit for the
question being asked: a channel's last fifteen uploads are the ones its current
audience and the current model actually saw, and a baseline drawn from four-year
-old uploads would compare a video against a channel that no longer exists.

The view count is a running total at the moment of the fetch. One fetch
therefore says how a video did *in total*; two fetches a week apart say how it
is doing *now*, which is the signal the recommendation surface actually moves.
:mod:`keel_seo.youtube.store` exists to make the second question askable.
"""
from __future__ import annotations

import datetime as dt
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict

FEED = "https://www.youtube.com/feeds/videos.xml"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

USER_AGENT = "keel-seo/youtube (+https://github.com/miladsafaei-me/keel-seo)"


class FeedError(RuntimeError):
    """Raised when a feed cannot be fetched or parsed."""


@dataclass(frozen=True)
class Video:
    """One entry of a channel feed, as the feed stated it."""

    video_id: str
    channel_id: str
    title: str
    published: str
    views: int | None
    rating_count: int | None
    rating_average: float | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def published_at(self) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(self.published)
        except (TypeError, ValueError):
            return None

    def age_days(self, now: dt.datetime | None = None) -> float | None:
        """Days since publication, as a float so a one-day-old video is not zero."""
        stamp = self.published_at()
        if stamp is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return max((now - stamp).total_seconds() / 86400.0, 0.0)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Feed:
    """One channel's feed at one moment."""

    channel_id: str
    channel_title: str
    fetched_at: str
    videos: tuple[Video, ...]


def feed_url(channel_id: str) -> str:
    return f"{FEED}?channel_id={channel_id}"


def _text(node, path: str) -> str:
    found = node.find(path, NS)
    return (found.text or "").strip() if found is not None else ""


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_feed(xml_text: str, fetched_at: str | None = None) -> Feed:
    """Parse a channel feed. An empty feed is a valid answer, not an error.

    A channel with no public uploads returns a well-formed document with no
    ``entry`` elements. Treating that as a failure would make every new or
    dormant channel look like a broken fetch.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"unparseable feed: {exc}") from exc

    channel_id = _text(root, "yt:channelId")
    if channel_id and not channel_id.startswith("UC"):
        # The feed states the id without its "UC" prefix; every other surface
        # uses the prefixed form, so normalise here rather than at each caller.
        channel_id = f"UC{channel_id}"
    channel_title = _text(root, "atom:title")

    videos: list[Video] = []
    for entry in root.findall("atom:entry", NS):
        group = entry.find("media:group", NS)
        stats = group.find("media:community/media:statistics", NS) if group is not None else None
        star = group.find("media:community/media:starRating", NS) if group is not None else None
        videos.append(Video(
            video_id=_text(entry, "yt:videoId"),
            channel_id=channel_id,
            title=_text(entry, "atom:title"),
            published=_text(entry, "atom:published"),
            views=_int(stats.get("views")) if stats is not None else None,
            rating_count=_int(star.get("count")) if star is not None else None,
            rating_average=_float(star.get("average")) if star is not None else None,
        ))

    return Feed(
        channel_id=channel_id,
        channel_title=channel_title,
        fetched_at=fetched_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        videos=tuple(videos),
    )


def fetch_feed(channel_id: str, timeout: float = 25.0) -> Feed:
    """Fetch and parse one channel's feed."""
    url = feed_url(channel_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise FeedError(f"{channel_id}: HTTP {exc.code} at {url}") from exc
    except OSError as exc:
        raise FeedError(f"{channel_id}: {exc}") from exc
    feed = parse_feed(body)
    if feed.channel_id and feed.channel_id != channel_id:
        raise FeedError(
            f"asked for {channel_id} and the feed answered for {feed.channel_id}")
    return feed
