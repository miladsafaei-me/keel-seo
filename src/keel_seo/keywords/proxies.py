"""A rotating pool of free proxies, so one blocked IP cannot end the research.

The autocomplete endpoint answers by IP and blocks by IP. Measured here: about
5,000 requests from one address earned an ``HTTP 403`` that was still in force
**sixteen hours later**, and it was machine-wide — ``weather`` was refused
exactly like the harvested seed. A crawler with one exit address is therefore one
mistake away from losing the source for a day, and no throttle setting is known
to prevent it.

Rotation changes the shape of that problem. Each proxy is a separate address with
its own budget, so a pool of two hundred is two hundred budgets, and a proxy that
does get blocked is evicted rather than retried.

**Free proxies are mostly dead, and that is fine.** Of 600 sampled, 37 returned a
clean 200 with real suggestions — but of those actually alive, two thirds got
through, so Google is not broadly refusing free proxy addresses; the pool is just
mostly rubbish. The response is to validate against the real endpoint rather than
against a liveness URL, keep only what answers, and refill when the live set runs
down.

**Validation must use the target endpoint.** A proxy that happily fetches
``httpbin.org`` may still be refused by Google, and only asking Google reveals it.

Geography is deliberately *not* pinned here. A rotating pool egresses from many
countries at once, so a harvest through it is mixed by construction — the run
records which countries it actually used instead of claiming one.
"""
from __future__ import annotations

import subprocess
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

# Lists that were reachable and productive when this was written. Each returns
# plain "ip:port" lines. They overlap heavily, which is harmless - the pool
# de-duplicates - and having several means one going offline is not fatal.
SOURCES = (
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) keel-seo/keywords"

# Consecutive failures before a proxy is dropped. Free proxies are erratic rather
# than cleanly up or down, so one timeout is not evidence of death; a refusal
# from the target is, and is handled separately.
FAILURE_LIMIT = 3

# What one proxy is allowed to do, on three timescales at once. A pool is only
# worth having if each address in it stays unblocked, and a single global rate
# does not ensure that: as proxies are evicted the survivors absorb the whole
# rate, so the last few inherit exactly the traffic that got the first ones
# blocked. These are per-address and enforced together.
#
# The numbers are deliberately far below the one blocking event actually
# measured - ~5,000 requests at 57 q/s from a single IP earned a 403 that held
# for sixteen hours. Since it is still unknown whether the trigger is rate or
# cumulative count, all three timescales are capped rather than just the rate:
# one request per five seconds is slower than a person typing, 10/minute is
# within what an ordinary search session produces, and 200/hour keeps a proxy
# an order of magnitude under the only figure known to have tripped a block.
PER_PROXY_RPS = 0.2
PER_PROXY_PER_MINUTE = 10
PER_PROXY_PER_HOUR = 200


@dataclass
class Budget:
    """Per-proxy usage limits on three timescales, enforced together.

    Keeps the timestamps of recent requests and answers one question: at the
    earliest, when may this proxy be used again? Old entries fall out of the
    window as they age, so memory stays proportional to the hourly cap.
    """

    rps: float = PER_PROXY_RPS
    per_minute: int = PER_PROXY_PER_MINUTE
    per_hour: int = PER_PROXY_PER_HOUR
    _times: deque = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] >= 3600.0:
            self._times.popleft()

    def ready_at(self, now: float) -> float:
        """The earliest moment all three limits allow another request."""
        self._prune(now)
        earliest = now
        if self._times:
            gap = 1.0 / self.rps if self.rps > 0 else 0.0
            earliest = max(earliest, self._times[-1] + gap)
        if self.per_minute and len(self._times) >= self.per_minute:
            recent = [t for t in self._times if now - t < 60.0]
            if len(recent) >= self.per_minute:
                earliest = max(earliest, recent[-self.per_minute] + 60.0)
        if self.per_hour and len(self._times) >= self.per_hour:
            earliest = max(earliest, self._times[-self.per_hour] + 3600.0)
        return earliest

    def record(self, now: float) -> None:
        self._times.append(now)


@dataclass(frozen=True)
class Proxy:
    addr: str
    kind: str

    @property
    def url(self) -> str:
        # socks5h, not socks5: the "h" resolves DNS at the proxy, so the lookup
        # does not leak from - or get answered by - the local network.
        return f"{'socks5h' if self.kind == 'socks5' else 'http'}://{self.addr}"


def fetch_candidates(sources=SOURCES, timeout: float = 25.0) -> list[Proxy]:
    """Pull raw proxy lists. Never raises: a dead source is skipped."""
    seen: dict[str, Proxy] = {}
    for kind, url in sources:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - one dead list must not end a run
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and ":" in line and not line.startswith("#"):
                seen.setdefault(line, Proxy(line, kind))
    return list(seen.values())


def fetch_through(proxy: Proxy, url: str, timeout: float = 10.0) -> tuple[int, str]:
    """GET `url` through `proxy`, returning (status, body). Status 0 means no reply.

    Uses curl rather than urllib for two reasons that are not stylistic. It
    speaks SOCKS5 without a Python dependency and without PySocks' process-wide
    default-proxy state, which is unusable from a thread pool. And it takes
    ``--noproxy ""``, which is required: with ``NO_PROXY=*`` in the environment -
    a common setting, and present on the machine this was written on - urllib
    silently ignores an explicit proxy and connects directly, so the code appears
    to rotate while every request leaves from the same blocked address. That
    failure is invisible: the crawl looks healthy and every request is spent on
    the one IP the pool exists to stop using.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), "--noproxy", "",
             "--proxy", proxy.url, "-H", f"User-Agent: {USER_AGENT}",
             "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 6,
        )
    except Exception:  # noqa: BLE001
        return 0, ""
    parts = result.stdout.rsplit("\n", 1)
    if len(parts) != 2:
        return 0, ""
    body, code = parts
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, body


def probe(proxy: Proxy, url: str, timeout: float = 10.0) -> bool:
    """True when this proxy returns a usable answer from the real endpoint."""
    status, body = fetch_through(proxy, url, timeout)
    return status == 200 and body.startswith("[")


@dataclass
class ProxyPool:
    """Thread-safe rotation over proxies proven to answer the target endpoint."""

    live: list[Proxy] = field(default_factory=list)
    validated_from: int = 0
    blocked: int = 0
    retired: int = 0
    rps: float = PER_PROXY_RPS
    per_minute: int = PER_PROXY_PER_MINUTE
    per_hour: int = PER_PROXY_PER_HOUR
    waited: float = 0.0
    served: int = 0
    _failures: dict[str, int] = field(default_factory=dict)
    _budgets: dict[str, Budget] = field(default_factory=dict)
    _busy: set = field(default_factory=set)
    _lock: threading.Condition = field(default_factory=threading.Condition)

    def __post_init__(self) -> None:
        for proxy in self.live:
            self._budgets.setdefault(
                proxy.addr,
                Budget(rps=self.rps, per_minute=self.per_minute, per_hour=self.per_hour),
            )

    def __len__(self) -> int:
        return len(self.live)

    @classmethod
    def build(cls, probe_url: str, *, want: int = 60, candidates: int = 900,
              workers: int = 120, timeout: float = 10.0,
              rps: float = PER_PROXY_RPS, per_minute: int = PER_PROXY_PER_MINUTE,
              per_hour: int = PER_PROXY_PER_HOUR, progress=None) -> "ProxyPool":
        """Collect and validate proxies until `want` of them answer `probe_url`."""
        from concurrent.futures import ThreadPoolExecutor

        found = fetch_candidates()
        if progress:
            progress(f"proxy pool: {len(found)} candidates from {len(SOURCES)} lists")
        # Validation is the expensive part, so it runs on a bounded slice rather
        # than the whole list; the hit rate measured ~6%, so `candidates` should
        # be roughly 15x `want`.
        batch = found[:candidates]
        live: list[Proxy] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for proxy, ok in zip(batch, pool.map(lambda p: probe(p, probe_url, timeout), batch)):
                if ok:
                    live.append(proxy)
                    if len(live) >= want:
                        break
        if progress:
            progress(f"proxy pool: {len(live)} of {len(batch)} probed proxies answered "
                     f"the endpoint ({100 * len(live) / max(1, len(batch)):.1f}%)")
            progress(f"proxy pool: each address limited to {rps}/s, {per_minute}/min, "
                     f"{per_hour}/hour — pool ceiling ≈ {len(live) * rps:.0f} q/s, "
                     f"{len(live) * per_hour:,}/hour")
        return cls(live=live, validated_from=len(batch), rps=rps,
                   per_minute=per_minute, per_hour=per_hour)

    def acquire(self, max_wait: float = 120.0) -> Proxy | None:
        """Hand out the most-rested idle proxy, waiting if every one is spending.

        Three rules together produce "many proxies at once, each of them gently":

        *One request at a time per address.* A proxy already mid-request is not
        offered again, so worker threads necessarily spread across different
        addresses instead of colliding on one. Concurrency is therefore bounded
        by how many proxies are rested, not by the thread count.

        *Least-recently-used first*, rather than round-robin. Round-robin looks
        fair but is not once proxies start being evicted: the survivors inherit
        the whole load in the same order. Picking whichever address has been idle
        longest keeps the spend even as the pool shrinks.

        *Nobody goes early.* A proxy is only offered once its own per-second,
        per-minute and per-hour budgets all allow it. When none is ready the
        caller waits for the earliest one rather than borrowing from a proxy that
        is over budget - which is the moment a pool would start burning itself.

        Returns None only when the pool is empty, which is a real dead end; the
        crawl treats that as having no egress left rather than as a rate limit.
        """
        deadline = time.monotonic() + max_wait
        with self._lock:
            while True:
                if not self.live:
                    return None
                now = time.monotonic()
                idle = [p for p in self.live if p.addr not in self._busy]
                if idle:
                    ready = [(self._budgets[p.addr].ready_at(now), p) for p in idle]
                    ready.sort(key=lambda item: (item[0], item[1].addr))
                    when, proxy = ready[0]
                    if when <= now:
                        self._busy.add(proxy.addr)
                        self._budgets[proxy.addr].record(now)
                        self.served += 1
                        return proxy
                    delay = min(when - now, deadline - now)
                else:
                    delay = min(0.25, max(0.0, deadline - now))
                if delay <= 0 and time.monotonic() >= deadline:
                    return None
                self.waited += max(delay, 0.0)
                # Waiting on the condition, not sleeping, so a proxy released by
                # another thread wakes this one immediately.
                self._lock.wait(max(delay, 0.01))

    def release(self, proxy: Proxy) -> None:
        """Mark a proxy idle again. Always call this, including after a failure."""
        with self._lock:
            self._busy.discard(proxy.addr)
            self._lock.notify_all()

    def report_ok(self, proxy: Proxy) -> None:
        with self._lock:
            self._failures.pop(proxy.addr, None)

    def report_failure(self, proxy: Proxy) -> None:
        """Count a flaky call; drop the proxy once it is consistently unusable."""
        with self._lock:
            count = self._failures.get(proxy.addr, 0) + 1
            self._failures[proxy.addr] = count
            if count >= FAILURE_LIMIT:
                self._drop(proxy)
                self.retired += 1

    def report_blocked(self, proxy: Proxy) -> None:
        """The target refused this address. One refusal is enough - drop it."""
        with self._lock:
            self._drop(proxy)
            self.blocked += 1

    def _drop(self, proxy: Proxy) -> None:
        # Caller holds the lock.
        self.live = [p for p in self.live if p.addr != proxy.addr]
        self._failures.pop(proxy.addr, None)
        self._busy.discard(proxy.addr)
        self._lock.notify_all()

    def stats(self) -> dict:
        with self._lock:
            return {
                "live": len(self.live),
                "validated_from": self.validated_from,
                "blocked_by_target": self.blocked,
                "retired_unreliable": self.retired,
                "requests_served": self.served,
                "per_proxy_limits": {
                    "per_second": self.rps,
                    "per_minute": self.per_minute,
                    "per_hour": self.per_hour,
                },
            }
