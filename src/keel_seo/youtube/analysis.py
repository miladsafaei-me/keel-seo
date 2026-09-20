"""How far a video ran past its own channel's normal, and how fast it is running now.

Two numbers, and they answer different questions.

**The multiple** is a video's view total divided by the median total of that
channel's other mature uploads. It is the closest externally visible proxy for
"the recommendation surface picked this one up", because a channel's own median
already contains its subscriber count, its niche and its posting cadence - which
is exactly what makes cross-channel view comparisons worthless and this one
usable. A 40k-view video on a channel whose median is 6k carries information; the
same 40k on a channel whose median is 300k carries the opposite information.

**The pace** is views gained per day between the two most recent observations of
the same video. It needs history and is therefore unavailable on a first run.
It is the better signal of the two when it exists: a multiple is a verdict on a
video's whole life, while the pace says what is being served *this week*, and a
two-year-old video that suddenly gains pace is the clearest evidence a free
endpoint can give that YouTube has started recommending something again.

**Young videos are excluded from the baseline and reported separately.** A video
published yesterday has not had time to accumulate, so including it drags the
median down and then flags every older video as an outlier against it. The
default maturity window is fourteen days.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from .feed import Feed, Video
from .store import Observation

DEFAULT_MATURE_DAYS = 14.0

# The floor the adaptive window will not go below. Under two days a view count
# is still the launch spike rather than a result, and a baseline drawn from
# videos that young measures how many subscribers saw a notification.
MINIMUM_MATURE_DAYS = 2.0

# How much of a feed the adaptive window aims to keep. Half is the point of a
# median: a window that admits nearly everything stops excluding anything, and
# one that admits three videos out of fifteen throws away the channel.
ADAPTIVE_KEEP = 0.5

BREAKOUT = 3.0
OUTPERFORMER = 1.8
UNDERPERFORMER = 0.5


@dataclass(frozen=True)
class ChannelBaseline:
    """What normal looks like on this channel right now."""

    channel_id: str
    channel_title: str
    mature_days: float
    adapted: bool
    total_videos: int
    mature_videos: int
    young_videos: int
    median_views: float | None
    median_views_per_day: float | None

    @property
    def dormant(self) -> bool:
        """Whether the feed carried nothing at all.

        Kept separate from "no mature uploads" because the two look identical in
        a count and mean opposite things: a dormant channel is one to drop from
        the registry, while a channel whose fifteen uploads are all a week old is
        the most active competitor on the list. Measured 2026-09-21, the first
        registry built for this project contained one of each and reported both
        as "0 mature uploads".
        """
        return self.total_videos == 0

    @property
    def usable(self) -> bool:
        """Whether a multiple computed against this baseline means anything.

        Three mature videos is the floor. A median of one or two is not a
        central tendency, it is a coin toss that will label half the feed a
        breakout.
        """
        return self.mature_videos >= 3 and bool(self.median_views)


@dataclass(frozen=True)
class VideoVerdict:
    """One video, measured against its own channel."""

    video: Video
    age_days: float | None
    mature: bool
    view_multiple: float | None
    lifetime_views_per_day: float | None
    recent_views_per_day: float | None
    pace_window_days: float | None
    label: str

    @property
    def title(self) -> str:
        return self.video.title

    @property
    def url(self) -> str:
        return self.video.url


def _recent_pace(observations: list[Observation]) -> tuple[float | None, float | None]:
    """Views per day between the two most recent usable observations.

    Returns ``(None, None)`` where history cannot support the number: fewer than
    two observations, a missing view count, or a window shorter than half a day -
    a window that short turns ordinary rounding into a nonsense rate.
    """
    usable = [row for row in observations if row.views is not None and row.fetched_datetime()]
    if len(usable) < 2:
        return None, None
    latest, previous = usable[-1], usable[-2]
    window = (latest.fetched_datetime() - previous.fetched_datetime()).total_seconds() / 86400.0
    if window < 0.5:
        return None, None
    gained = (latest.views or 0) - (previous.views or 0)
    return gained / window, window


def _label(multiple: float | None, mature: bool) -> str:
    if not mature:
        return "too young to judge"
    if multiple is None:
        return "no baseline"
    if multiple >= BREAKOUT:
        return "breakout"
    if multiple >= OUTPERFORMER:
        return "outperformer"
    if multiple <= UNDERPERFORMER:
        return "underperformer"
    return "normal"


def _choose_window(ages: list[float], requested: float) -> tuple[float, bool]:
    """Pick the maturity window this channel can actually support.

    The requested window is kept whenever it leaves a usable baseline. It often
    does not: a channel posting daily has fifteen uploads spanning eleven days,
    so a fourteen-day window matures none of them and the channel reports no
    baseline for ever. Measured 2026-09-21, three of this project's five
    competitors were in exactly that state.

    The fallback keeps the older half of the feed, which is the widest window
    that still leaves something to take a median of, and never goes below
    :data:`MINIMUM_MATURE_DAYS`. The chosen window travels on the baseline so a
    report always states which one produced its numbers - an adaptive threshold
    that does not say what it adapted to is how two channels end up compared
    against different rulers without anybody noticing.
    """
    usable = sorted(age for age in ages if age is not None)
    if len(usable) < 3:
        return requested, False
    if sum(1 for age in usable if age >= requested) >= 3:
        return requested, False
    index = max(int(len(usable) * ADAPTIVE_KEEP) - 1, 2)
    fallback = max(usable[len(usable) - 1 - index], MINIMUM_MATURE_DAYS)
    return (requested, False) if fallback >= requested else (fallback, True)


def analyse(
    feed: Feed,
    history: dict[str, list[Observation]] | None = None,
    mature_days: float = DEFAULT_MATURE_DAYS,
    now: dt.datetime | None = None,
) -> tuple[ChannelBaseline, list[VideoVerdict]]:
    """Measure one channel's feed against itself.

    ``history`` is what :meth:`keel_seo.youtube.store.SnapshotStore.history`
    returns for this channel, or ``None`` on a first run. Its absence costs the
    pace column and nothing else, which is what makes the first run still worth
    doing.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    history = history or {}

    ages = {video.video_id: video.age_days(now) for video in feed.videos}
    mature_days, adapted = _choose_window(
        [age for age in ages.values() if age is not None], mature_days)
    mature = [
        video for video in feed.videos
        if video.views is not None
        and ages.get(video.video_id) is not None
        and ages[video.video_id] >= mature_days
    ]

    median_views = statistics.median([v.views for v in mature]) if mature else None
    per_day = [
        v.views / ages[v.video_id]
        for v in mature
        if ages[v.video_id] and ages[v.video_id] > 0
    ]
    median_per_day = statistics.median(per_day) if per_day else None

    baseline = ChannelBaseline(
        channel_id=feed.channel_id,
        channel_title=feed.channel_title,
        mature_days=mature_days,
        adapted=adapted,
        total_videos=len(feed.videos),
        mature_videos=len(mature),
        young_videos=len(feed.videos) - len(mature),
        median_views=median_views,
        median_views_per_day=median_per_day,
    )

    verdicts: list[VideoVerdict] = []
    for video in feed.videos:
        age = ages.get(video.video_id)
        is_mature = age is not None and age >= mature_days
        multiple = None
        if baseline.usable and video.views is not None:
            multiple = video.views / baseline.median_views
        lifetime = (video.views / age) if (video.views is not None and age and age > 0) else None
        pace, window = _recent_pace(history.get(video.video_id, []))
        verdicts.append(VideoVerdict(
            video=video,
            age_days=age,
            mature=is_mature,
            view_multiple=multiple,
            lifetime_views_per_day=lifetime,
            recent_views_per_day=pace,
            pace_window_days=window,
            label=_label(multiple, is_mature),
        ))

    # Strongest first, and a video with a pace outranks one without: the pace is
    # the fresher evidence, so a run that has history should not bury it under
    # lifetime multiples earned two years ago.
    verdicts.sort(key=lambda row: (
        row.recent_views_per_day is not None,
        row.recent_views_per_day or 0.0,
        row.view_multiple or 0.0,
    ), reverse=True)
    return baseline, verdicts
