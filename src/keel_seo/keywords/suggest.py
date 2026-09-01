"""The one collector: Google's autocomplete endpoint, keyless.

Autocomplete is the only free source that returns the phrasings people actually
type. It never returns volume, and no parameter exists that would make it — a
tool built on it answers "what is asked, and in what shape", not "how much".

Two properties of the endpoint decide the whole crawler design above this file,
and both were measured rather than assumed (2026-09-01, from a TR egress):

*Truncation is the signal.* A response is capped at the client's capacity — 15
for ``chrome``, 10 for ``firefox``. A full response therefore means "Google had
more to say and stopped", which is what makes saturation a reliable instruction
to drill further. A short response means that corner of the query space is
exhausted.

*Responses are deterministic.* The same query asked twice returned byte-identical
results, so the on-disk cache is safe and a long crawl can be interrupted and
resumed without re-paying for what it already collected.

*The endpoint blocks, and it blocks hard.* At roughly 5,000 requests in a few
minutes it began answering ``HTTP 403`` to everything, and was still refusing a
single hand-made request more than a hundred seconds later. There is no quota
header and no ``Retry-After`` to read, so the block is only visible as a status
code. This is why the client throttles by default, treats 403/429/503 as a
distinct condition rather than a generic failure, and gives up deliberately
through a circuit breaker instead of grinding a whole budget into errors — which
is exactly what the first unthrottled run did.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, Iterator

ENDPOINT = "https://suggestqueries.google.com/complete/search"

# How many suggestions each client is allowed to return. `chrome` returns 50%
# more than `firefox` for an identical request, and is the only one that carries
# google:suggestrelevance, so it is the default.
CAPACITY = {"chrome": 15, "firefox": 10}

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) keel-seo/keywords"

# Status codes that mean "you are asking too much", as opposed to a genuine
# failure. Google answers 403 rather than 429 for this endpoint, which is why a
# naive client mistakes a rate limit for a permanent error.
BLOCK_CODES = frozenset({403, 429, 503})

# Sustained request rate, in queries per second. An unthrottled run measured 57
# q/s and was blocked after about 5,000 requests; 6 q/s is deliberately far
# under that, because a blocked harvest costs far more time than a slow one.
DEFAULT_RATE = 6.0

# Consecutive rate-limited responses before the crawl is abandoned. Set above
# the worker count so that one bad moment across every in-flight request does
# not end a run that would have recovered.
BLOCK_LIMIT = 40

# Waits between retries of a rate-limited request. Longer than the generic
# retry path: a block is not a flaky connection and retrying it quickly only
# deepens it.
BLOCK_BACKOFF = (5.0, 20.0, 60.0)


class RateLimited(Exception):
    """The endpoint refused the request because we asked too often."""


class Throttle:
    """A shared minimum interval between request starts, across all workers.

    Every thread takes the next slot in a single timeline, so the ceiling is the
    whole client's rate rather than each thread's.
    """

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True)
class Suggestion:
    """One phrase Google offered for one query."""

    phrase: str
    rank: int
    relevance: int


@dataclass(frozen=True)
class Response:
    """Everything one query returned, plus whether Google truncated it."""

    query: str
    suggestions: tuple[Suggestion, ...]
    capacity: int
    error: str | None = None

    @property
    def saturated(self) -> bool:
        """True when Google filled the response and had more it could not fit.

        This is the crawler's instruction to keep drilling underneath this query.
        """
        return len(self.suggestions) >= self.capacity


class SuggestCache:
    """Append-only JSONL cache keyed by the full request identity.

    Keyed by (egress country, client, hl, ds, query). Language and vertical
    change the answer, so caching on the query text alone would serve a YouTube
    result to a web-vertical request. Geography belongs in the key for a sharper
    reason: autocomplete answers according to the requesting IP, so a cache
    carried between two exit routes would blend two different markets into one
    harvest that claims to be a single one. Including the egress means a run from
    a new country starts its own namespace instead of quietly inheriting another
    country's answers.
    """

    def __init__(self, path: str | None, egress: str = "unknown"):
        self.path = path
        self.egress = egress
        self._rows: dict[str, list] = {}
        self._lock = threading.Lock()
        self.hits = 0
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._rows[row["k"]] = row["v"]

    def key(self, client: str, hl: str, ds: str, query: str) -> str:
        return f"{self.egress}|{client}|{hl}|{ds}|{query}"

    def get(self, key: str) -> list | None:
        value = self._rows.get(key)
        if value is not None:
            self.hits += 1
        return value

    def put(self, key: str, value: list) -> None:
        with self._lock:
            if key in self._rows:
                return
            self._rows[key] = value
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n")


@dataclass
class SuggestClient:
    """Fetches suggestions, with retries, caching and bounded concurrency.

    Measured throughput from this laptop: 57 queries/second across 364 sustained
    requests at 12 workers with zero errors. The default is deliberately lower —
    a keyless public endpoint is a courtesy, not an entitlement, and a crawl that
    finishes in 90 seconds instead of 60 costs nothing.
    """

    hl: str = "en"
    ds: str = ""
    client: str = "chrome"
    workers: int = 5
    timeout: float = 15.0
    retries: int = 3
    rate: float = DEFAULT_RATE
    cache: SuggestCache = field(default_factory=lambda: SuggestCache(None))
    calls: int = 0
    errors: int = 0
    rate_limited: int = 0
    blocked: bool = False

    def __post_init__(self) -> None:
        self._throttle = Throttle(self.rate)
        self._consecutive_blocks = 0
        self._state = threading.Lock()

    @property
    def capacity(self) -> int:
        return CAPACITY.get(self.client, 10)

    def _note_block(self) -> None:
        """Count a refusal, and trip the breaker once they stop being isolated."""
        with self._state:
            self.rate_limited += 1
            self._consecutive_blocks += 1
            if self._consecutive_blocks >= BLOCK_LIMIT:
                self.blocked = True

    def _note_success(self) -> None:
        with self._state:
            self._consecutive_blocks = 0

    def _request(self, query: str) -> list | None:
        params = {"client": self.client, "hl": self.hl, "q": query}
        if self.ds:
            params["ds"] = self.ds
        url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(self.retries):
            self._throttle.wait()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code not in BLOCK_CODES:
                    if attempt == self.retries - 1:
                        raise
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if attempt == self.retries - 1:
                    raise RateLimited(f"HTTP {exc.code}") from exc
                time.sleep(BLOCK_BACKOFF[min(attempt, len(BLOCK_BACKOFF) - 1)])
            except Exception:  # noqa: BLE001 - one flaky call must not end a crawl
                if attempt == self.retries - 1:
                    raise
                time.sleep(1.0 * (attempt + 1))
        return None

    def fetch(self, query: str) -> Response:
        key = self.cache.key(self.client, self.hl, self.ds, query)
        payload = self.cache.get(key)
        if payload is None:
            if self.blocked:
                # The breaker has tripped. Answering from here costs nothing and
                # keeps a stopping crawl from spending its budget on refusals.
                return Response(query, (), self.capacity, error="blocked")
            try:
                payload = self._parse(self._request(query))
            except RateLimited as exc:
                self._note_block()
                return Response(query, (), self.capacity, error=f"rate-limited: {exc}")
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                return Response(query, (), self.capacity, error=repr(exc)[:120])
            self._note_success()
            self.calls += 1
            self.cache.put(key, payload)
        return Response(
            query=query,
            suggestions=tuple(
                Suggestion(phrase, rank, relevance)
                for rank, (phrase, relevance) in enumerate(payload, 1)
            ),
            capacity=self.capacity,
        )

    @staticmethod
    def _parse(raw: list | None) -> list:
        """Flatten the endpoint's positional payload into (phrase, relevance) pairs.

        The response is a list whose second element is the phrases and whose fifth
        is a metadata dict; google:suggestrelevance lives there and is absent for
        the firefox client, in which case relevance is recorded as 0.
        """
        if not raw or len(raw) < 2:
            return []
        phrases = raw[1] or []
        meta = raw[4] if len(raw) > 4 and isinstance(raw[4], dict) else {}
        scores = meta.get("google:suggestrelevance") or []
        return [
            [phrase, int(scores[i]) if i < len(scores) else 0]
            for i, phrase in enumerate(phrases)
        ]

    def fetch_many(self, queries: Iterable[str]) -> Iterator[Response]:
        queries = list(queries)
        if not queries:
            return
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            yield from pool.map(self.fetch, queries)


def egress_identity(timeout: float = 8.0) -> dict:
    """Report the IP the crawl actually left from, and the country it maps to.

    Autocomplete's geography is decided by the requesting IP, never by a ``gl=``
    parameter - asking ``gl=us``, ``gl=in`` and ``gl=br`` returns byte-identical
    results, while the same query from two different egress IPs does not. A
    harvest is therefore only labelable by where it egressed, so every run
    stamps this into its output instead of a country the caller asked for.
    """
    try:
        request = urllib.request.Request(
            "https://ipinfo.io/json", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return {
            "ip": data.get("ip", ""),
            "country": data.get("country", ""),
            "org": data.get("org", ""),
        }
    except Exception:  # noqa: BLE001 - never fail a crawl over a label
        return {"ip": "", "country": "unknown", "org": ""}
