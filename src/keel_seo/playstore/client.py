"""One client for the four Google Play calls a gap search makes.

*Autocomplete* goes through Play's own web RPC (``batchexecute``, rpc id
``IJ4APc``), the call the store's search box makes. Measured 2026-11-08 from a
TR exit: it returns at most five phrases per query, so five means "there was
more" and is the signal to drill further, as in :mod:`keel_seo.keywords`.

*Search, details and reviews* go through google-play-scraper, which parses the
store's page data. Its search returns the top 30 apps for a phrase; details carry
real installs, rating, rating count, last update and the in-app price range.

Every answer is cached in one append-only JSONL file keyed by the full request
(call, language, country, argument), so a run can be stopped and resumed, and a
second seed reuses every app the first one already measured.

The volume of one run is small (a few hundred calls), and it is paced by a shared
throttle and stopped by a breaker after repeated failures, so a refusal ends the
run cleanly instead of grinding into errors.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BATCH_ENDPOINT = "https://play.google.com/_/PlayStoreUi/data/batchexecute"
SUGGEST_RPC = "IJ4APc"
SUGGEST_CAPACITY = 5
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) keel-seo/playstore"

UPDATED_ON = re.compile(r"Updated on</div><div[^>]*>([^<]+)<")

DEFAULT_RATE = 2.0
BREAKER_LIMIT = 8

MISSING_MESSAGE = (
    "Google Play search, details and reviews need google-play-scraper. "
    "Install it with:  pip install 'keel-seo[playstore]'"
)


class Blocked(RuntimeError):
    """Too many consecutive failures; the store is refusing this run."""


class Throttle:
    """A minimum interval between request starts, shared by every worker."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Cache:
    """Append-only JSONL cache, ``{"k": key, "v": value}`` per line."""

    def __init__(self, path: str | None):
        self.path = path
        self._rows: dict[str, object] = {}
        self._lock = threading.Lock()
        self.hits = 0
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._rows[row["k"]] = row["v"]

    def get(self, key: str):
        if key in self._rows:
            self.hits += 1
            return self._rows[key]
        return None

    def has(self, key: str) -> bool:
        return key in self._rows

    def put(self, key: str, value) -> None:
        with self._lock:
            self._rows[key] = value
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"k": key, "v": value},
                                            ensure_ascii=False, default=str) + "\n")


def parse_suggest(body: str) -> list[str]:
    """Pull the phrases out of a batchexecute answer.

    The body is a ``)]}'`` guard, then length-prefixed JSON chunks. The chunk
    whose first row is ``["wrb.fr", "IJ4APc", "<json>", ...]`` carries a second
    JSON document as a string, and its ``[0][0]`` is the list of suggestions,
    each one a row whose first cell is the phrase.
    """
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in chunk:
            if (isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr"
                    and row[1] == SUGGEST_RPC and isinstance(row[2], str)):
                inner = json.loads(row[2])
                try:
                    items = inner[0][0]
                except (IndexError, TypeError):
                    return []
                return [item[0] for item in items or []
                        if isinstance(item, list) and item and isinstance(item[0], str)]
    return []


@dataclass
class PlayClient:
    """Google Play, asked in one language and one country, cached and paced."""

    hl: str = "en"
    gl: str = "us"
    rate: float = DEFAULT_RATE
    timeout: float = 20.0
    retries: int = 3
    cache: Cache = field(default_factory=lambda: Cache(None))
    calls: int = 0
    errors: int = 0

    def __post_init__(self) -> None:
        self._throttle = Throttle(self.rate)
        self._state = threading.Lock()
        self._consecutive = 0

    def _key(self, kind: str, arg: str) -> str:
        return f"{kind}|{self.hl}|{self.gl.lower()}|{arg}"

    def _call(self, kind: str, arg: str, fn):
        """Answer from the cache, or call ``fn`` with retries and the breaker."""
        key = self._key(kind, arg)
        if self.cache.has(key):
            return self.cache.get(key)
        if self._consecutive >= BREAKER_LIMIT:
            raise Blocked(f"{BREAKER_LIMIT} consecutive failures; stopping")
        last = None
        for attempt in range(self.retries):
            self._throttle.wait()
            try:
                value = fn()
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_not_found(exc):
                    value = None
                    break
                last = exc
                time.sleep(2.0 * (attempt + 1))
                continue
            break
        else:
            with self._state:
                self.errors += 1
                self._consecutive += 1
            raise RuntimeError(f"{kind} {arg!r} failed: {last!r}"[:200])
        with self._state:
            self.calls += 1
            self._consecutive = 0
        self.cache.put(key, value)
        return value

    def suggest(self, term: str) -> list[str]:
        def fetch():
            inner = json.dumps([[None, [term], [10], [2], 4]])
            freq = json.dumps([[[SUGGEST_RPC, inner, None, "generic"]]])
            query = urllib.parse.urlencode({
                "rpcids": SUGGEST_RPC, "hl": self.hl, "gl": self.gl.upper(),
                "soc-app": "121", "soc-platform": "1", "soc-device": "1", "rt": "c",
            })
            request = urllib.request.Request(
                f"{BATCH_ENDPOINT}?{query}",
                data=urllib.parse.urlencode({"f.req": freq}).encode(),
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return parse_suggest(response.read().decode("utf-8", "replace"))
        return self._call("suggest", term, fetch) or []

    def search(self, term: str, hits: int = 30) -> list[dict]:
        gps = _library()
        def fetch():
            rows = gps.search(term, lang=self.hl, country=self.gl, n_hits=hits)
            return [{k: row.get(k) for k in ("appId", "title", "score", "genre",
                                              "free", "developer")}
                    for row in rows if row.get("appId")]
        return self._call("search", term, fetch) or []

    def details(self, app_id: str) -> dict | None:
        gps = _library()
        def fetch():
            row = gps.app(app_id, lang=self.hl, country=self.gl)
            keep = ("appId", "title", "installs", "realInstalls", "score", "ratings",
                    "reviews", "updated", "released", "free", "price", "offersIAP",
                    "inAppProductPrice", "containsAds", "genre", "developer", "url",
                    "summary")
            out = {k: row.get(k) for k in keep}
            if not out.get("updated"):
                out["updated"] = self._updated_from_page(app_id)
            return out
        # "details2": the first key cached answers whose update date the library
        # had dropped, before the page fallback existed.
        return self._call("details2", app_id, fetch)

    def _updated_from_page(self, app_id: str):
        """Read "Updated on" from the rendered page when the parser misses it.

        Measured 2026-11-08: the library returned no update date for 50 of 92
        apps, while every one of those pages still printed it in plain HTML.
        """
        query = urllib.parse.urlencode({"id": app_id, "hl": "en", "gl": self.gl.upper()})
        request = urllib.request.Request(
            f"https://play.google.com/store/apps/details?{query}",
            headers={"User-Agent": USER_AGENT})
        try:
            self._throttle.wait()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                html = response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a missing date is not a failed app
            return None
        match = UPDATED_ON.search(html)
        if not match:
            return None
        try:
            stamp = datetime.datetime.strptime(match.group(1).strip(), "%b %d, %Y")
        except ValueError:
            return None
        return int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp())

    def low_reviews(self, app_id: str, count: int = 150) -> list[dict]:
        """The most relevant 1 to 3 star reviews: what the app's users miss."""
        gps = _library()
        def fetch():
            out = []
            for stars in (1, 2, 3):
                rows, _ = gps.reviews(app_id, lang=self.hl, country=self.gl,
                                      sort=gps.Sort.MOST_RELEVANT,
                                      count=max(count // 3, 1),
                                      filter_score_with=stars)
                out.extend({"score": r.get("score"), "at": str(r.get("at") or "")[:10],
                            "thumbs": r.get("thumbsUpCount") or 0,
                            "text": (r.get("content") or "").strip()}
                           for r in rows if (r.get("content") or "").strip())
            return out
        return self._call("reviews", f"{app_id}|{count}", fetch) or []


def _library():
    try:
        import google_play_scraper  # noqa: PLC0415 - optional extra
    except ImportError as exc:  # pragma: no cover - hosts without the extra
        raise RuntimeError(MISSING_MESSAGE) from exc
    return google_play_scraper


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ == "NotFoundError" or "404" in str(exc)
