"""Tests for keel_seo.youtube.

Every case here is a failure mode that was observed on real data on 2026-09-21,
not a hypothetical. The package's whole risk is that it produces a plausible
number from the wrong channel or the wrong baseline, and every one of those
failures is silent.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest

from keel_seo.youtube.analysis import MINIMUM_MATURE_DAYS, analyse
from keel_seo.youtube.api import COST, YouTubeApi
from keel_seo.youtube.channels import _reconcilable
from keel_seo.youtube.feed import parse_feed
from keel_seo.youtube.store import SnapshotStore

FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <yt:channelId>{channel}</yt:channelId>
 <title>{title}</title>
 {entries}
</feed>"""

ENTRY_TEMPLATE = """
 <entry>
  <yt:videoId>{vid}</yt:videoId>
  <title>{title}</title>
  <published>{published}</published>
  <media:group><media:community>
   <media:starRating count="1" average="5.00"/>
   <media:statistics views="{views}"/>
  </media:community></media:group>
 </entry>"""


def make_feed(videos, channel="SQsMdLP053xznP8_fWlNcA", title="Test Channel"):
    """videos is a list of (video_id, views, age_in_days)."""
    now = dt.datetime.now(dt.timezone.utc)
    entries = "".join(
        ENTRY_TEMPLATE.format(
            vid=vid, title=f"video {vid}", views=views,
            published=(now - dt.timedelta(days=age)).isoformat())
        for vid, views, age in videos)
    return parse_feed(FEED_TEMPLATE.format(channel=channel, title=title, entries=entries))


class FeedParsing(unittest.TestCase):
    def test_channel_id_is_normalised_to_its_prefixed_form(self):
        """The feed states the id without "UC"; every other surface uses it."""
        feed = make_feed([("a", 10, 30)])
        self.assertTrue(feed.channel_id.startswith("UC"))

    def test_an_empty_feed_parses_rather_than_raising(self):
        """A dormant channel is a finding, not a fetch failure."""
        feed = make_feed([])
        self.assertEqual(feed.videos, ())
        self.assertEqual(feed.channel_title, "Test Channel")


class Reconciliation(unittest.TestCase):
    def test_a_brands_own_spelling_of_itself_is_accepted(self):
        """Measured titles: "topstep", "Funding Pips", "Apex Trader Funding"."""
        self.assertTrue(_reconcilable("Topstep", "topstep"))
        self.assertTrue(_reconcilable("Funding Pips", "FundingPips"))
        self.assertTrue(_reconcilable("FundingPips", "Funding Pips"))
        self.assertTrue(_reconcilable("Apex Trader Funding", "Apex Trader Funding"))

    def test_the_squatted_handle_is_refused(self):
        """youtube.com/@FTMO is a toy-unboxing channel called "An Toys TV"."""
        self.assertFalse(_reconcilable("FTMO", "An Toys TV"))


class Baselines(unittest.TestCase):
    def test_a_dormant_channel_is_distinguished_from_a_fast_posting_one(self):
        """Both report zero mature uploads and mean opposite things."""
        dormant, _ = analyse(make_feed([]))
        self.assertTrue(dormant.dormant)

        fast = [(f"v{i}", 100 + i, 0.5) for i in range(15)]
        busy, _ = analyse(make_feed(fast))
        self.assertFalse(busy.dormant)
        self.assertEqual(busy.total_videos, 15)

    def test_the_window_narrows_for_a_channel_that_posts_daily(self):
        """15 uploads spanning 11 days matures none of them at 14 days."""
        videos = [(f"v{i}", 1000, float(i)) for i in range(1, 16)]
        baseline, _ = analyse(make_feed(videos), mature_days=14.0)
        self.assertTrue(baseline.adapted)
        self.assertLess(baseline.mature_days, 14.0)
        self.assertGreaterEqual(baseline.mature_days, MINIMUM_MATURE_DAYS)
        self.assertTrue(baseline.usable)

    def test_the_window_is_kept_when_the_feed_can_support_it(self):
        videos = [(f"v{i}", 1000, 30.0 + i) for i in range(10)]
        baseline, _ = analyse(make_feed(videos), mature_days=14.0)
        self.assertFalse(baseline.adapted)
        self.assertEqual(baseline.mature_days, 14.0)

    def test_two_mature_videos_do_not_make_a_baseline(self):
        """A median of two is a coin toss that labels half a feed a breakout."""
        baseline, verdicts = analyse(make_feed([("a", 10, 40), ("b", 900, 40)]))
        self.assertFalse(baseline.usable)
        self.assertTrue(all(v.view_multiple is None for v in verdicts))

    def test_a_young_video_is_reported_but_kept_out_of_the_baseline(self):
        videos = [(f"v{i}", 1000, 40.0) for i in range(5)] + [("new", 5, 0.1)]
        baseline, verdicts = analyse(make_feed(videos), mature_days=14.0)
        self.assertEqual(baseline.median_views, 1000)
        young = next(v for v in verdicts if v.video.video_id == "new")
        self.assertEqual(young.label, "too young to judge")


class Velocity(unittest.TestCase):
    def test_the_pace_needs_two_observations_a_window_apart(self):
        with tempfile.TemporaryDirectory() as root:
            store = SnapshotStore(root)
            feed = make_feed([("a", 100, 40)])
            store.append(feed)

            _, verdicts = analyse(feed, store.history(feed.channel_id))
            self.assertIsNone(verdicts[0].recent_views_per_day,
                              "one observation cannot be a rate")

            # A second observation a week later, at double the views.
            path = store.path_for(feed.channel_id)
            later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "fetched_at": later.isoformat(), "video_id": "a",
                    "title": "video a", "published": "", "views": 800}) + "\n")

            _, verdicts = analyse(feed, store.history(feed.channel_id))
            self.assertAlmostEqual(verdicts[0].recent_views_per_day, 100.0, places=1)

    def test_a_truncated_line_does_not_destroy_the_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = SnapshotStore(root)
            feed = make_feed([("a", 100, 40)])
            store.append(feed)
            with open(store.path_for(feed.channel_id), "a", encoding="utf-8") as handle:
                handle.write('{"fetched_at": "2026-0')
            self.assertEqual(len(store.observations(feed.channel_id)), 1)


class Quota(unittest.TestCase):
    def test_the_uploads_playlist_costs_nothing_to_derive(self):
        self.assertEqual(
            YouTubeApi.uploads_playlist("UCypUrEOeDRA5_uLMnKBVpZg"),
            "UUypUrEOeDRA5_uLMnKBVpZg")

    def test_the_cheap_sweep_stays_inside_one_days_allowance(self):
        """Five channels, a thousand videos each, against a 10,000-unit day."""
        self.assertEqual(YouTubeApi(api_key="x").estimate_units(5, 1000), 200)

    def test_search_is_a_hundred_times_the_price_of_the_route_taken(self):
        self.assertEqual(COST["search.list"], 100)
        self.assertEqual(COST["playlistItems.list"], 1)
        self.assertEqual(COST["videos.list"], 1)


if __name__ == "__main__":
    unittest.main()
