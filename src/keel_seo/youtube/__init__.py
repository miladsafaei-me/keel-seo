"""YouTube audience research, keyless where it can be.

This package is the second half of what :mod:`keel_seo.keywords` already
started. That module harvests what people *type* into YouTube (the autocomplete
endpoint answers the YouTube vertical when asked with ``--ds yt``); this one
measures what they *watched*, which is the only externally visible evidence of
how YouTube's recommendation surface behaved.

The split matters because the two surfaces are scored differently. A video that
arrives through search was chosen by a query, so the phrasing of the title is
the lever. A video that arrives through the home feed or the sidebar was chosen
by a model optimising click-through and watch time, and no keyword predicts it.
The nearest externally measurable proxy is how far a video ran past its own
channel's normal, which is what :mod:`keel_seo.youtube.analysis` computes.

Nothing here needs an API key. The channel feed at
``/feeds/videos.xml?channel_id=...`` is public, carries the fifteen most recent
uploads, and reports a view count per video. The YouTube Data API is a separate,
optional path; it is richer and still free, but it is quota-metered and needs a
Google Cloud project, so the keyless path is what runs unattended.
"""
from .channels import Channel, ChannelResolutionError, resolve, resolve_many
from .feed import Feed, Video, fetch_feed, feed_url
from .store import SnapshotStore
from .analysis import ChannelBaseline, VideoVerdict, analyse

__all__ = [
    "Channel",
    "ChannelResolutionError",
    "resolve",
    "resolve_many",
    "Feed",
    "Video",
    "fetch_feed",
    "feed_url",
    "SnapshotStore",
    "ChannelBaseline",
    "VideoVerdict",
    "analyse",
]
