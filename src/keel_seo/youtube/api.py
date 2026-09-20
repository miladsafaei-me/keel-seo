"""The optional keyed path, built to stay inside the free quota.

The Data API gives what the feed cannot: a channel's whole back catalogue rather
than its newest fifteen, subscriber counts, durations, and the Shorts the feed
leaves out. It is free, but it is metered - 10,000 units a day per Google Cloud
project - and the meter is where every naive integration dies.

**The expensive call is ``search.list``, at 100 units.** A hundred searches and
the day is gone. Nothing in this module calls it, because nothing here needs to:
a channel's uploads are reachable without searching at all.

The cheap route, and the arithmetic that makes it cheap:

1. A channel's uploads playlist id is its channel id with ``UC`` replaced by
   ``UU``. That is a documented identity, not a guess, so it costs **0 units**
   where ``channels.list`` would cost 1.
2. ``playlistItems.list`` walks that playlist 50 videos at a time for **1 unit**
   a page.
3. ``videos.list`` returns statistics for 50 ids at a time, also **1 unit**.

So a thousand videos with full statistics costs 20 units + 20 units = 40, and
the daily allowance covers about 250,000 videos. The same thousand videos
through ``search.list`` would cost 2,000 units and return less.

Nothing here writes. Uploading is a different scope, a different quota bucket
and a different consent flow; see this package's README for why it is not worth
wiring until a channel is publishing at volume.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE = "https://www.googleapis.com/youtube/v3"

# Unit costs, from the official quota calculator. Kept here so `spent` is a real
# measurement rather than a call count, and so the one expensive method is
# visible in source next to the methods that replace it.
COST = {
    "playlistItems.list": 1,
    "videos.list": 1,
    "channels.list": 1,
    "search.list": 100,
}

PAGE = 50


class QuotaExceeded(RuntimeError):
    """Raised when the API reports the daily allowance is gone."""


class ApiError(RuntimeError):
    """Raised for any other API failure."""


@dataclass
class YouTubeApi:
    """A read-only client that counts what it spends.

    ``spent`` is the whole point of the dataclass. A quota that is only observed
    when it runs out is a quota that runs out mid-job, and the failure arrives as
    a 403 on whichever call happened to be next rather than on the expensive one
    that caused it.
    """

    api_key: str
    timeout: float = 30.0
    spent: int = field(default=0)

    def _get(self, method: str, params: dict) -> dict:
        params = {**params, "key": self.api_key}
        url = f"{BASE}/{method.split('.')[0]}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403 and "quota" in body.lower():
                raise QuotaExceeded(
                    f"daily quota exhausted after {self.spent} unit(s) this run. "
                    f"It resets at midnight Pacific.") from exc
            raise ApiError(f"{method}: HTTP {exc.code}: {body[:400]}") from exc
        except OSError as exc:
            raise ApiError(f"{method}: {exc}") from exc
        self.spent += COST.get(method, 1)
        return payload

    @staticmethod
    def uploads_playlist(channel_id: str) -> str:
        """The channel's uploads playlist, for free.

        ``UCxxxx`` -> ``UUxxxx``. Documented and stable; resolving it through
        ``channels.list`` returns the same string and costs a unit.
        """
        if not channel_id.startswith("UC"):
            raise ValueError(f"not a channel id: {channel_id!r}")
        return "UU" + channel_id[2:]

    def uploads(self, channel_id: str, limit: int = 200) -> list[dict]:
        """Every upload id and title, newest first, one unit per 50."""
        playlist = self.uploads_playlist(channel_id)
        items: list[dict] = []
        page_token = ""
        while len(items) < limit:
            params = {
                "part": "snippet,contentDetails",
                "playlistId": playlist,
                "maxResults": min(PAGE, limit - len(items)),
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get("playlistItems.list", params)
            items.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break
        return items[:limit]

    def video_stats(self, video_ids: list[str]) -> dict[str, dict]:
        """Statistics and durations for up to 50 ids a unit, keyed by video id."""
        out: dict[str, dict] = {}
        for start in range(0, len(video_ids), PAGE):
            batch = video_ids[start:start + PAGE]
            payload = self._get("videos.list", {
                "part": "statistics,contentDetails,snippet",
                "id": ",".join(batch),
            })
            for item in payload.get("items", []):
                out[item["id"]] = item
        return out

    def channel_stats(self, channel_ids: list[str]) -> dict[str, dict]:
        """Subscriber and view totals for up to 50 channels a unit."""
        out: dict[str, dict] = {}
        for start in range(0, len(channel_ids), PAGE):
            batch = channel_ids[start:start + PAGE]
            payload = self._get("channels.list", {
                "part": "statistics,snippet,contentDetails",
                "id": ",".join(batch),
            })
            for item in payload.get("items", []):
                out[item["id"]] = item
        return out

    def estimate_units(self, channels: int, videos_per_channel: int) -> int:
        """What a full sweep would cost, before spending any of it."""
        pages = -(-videos_per_channel // PAGE)
        return channels * pages * (COST["playlistItems.list"] + COST["videos.list"])
