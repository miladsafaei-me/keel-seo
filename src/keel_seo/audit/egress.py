"""Pooled fetching for the audit crawl.

Every request leaves through keel-crawler's rotating pool, never from the
machine running the audit (seo-core, "Crawling never leaves from our own
address"). The pool's own ``fetch_through`` returns only a status and a body;
an audit also needs the response headers and each redirect hop, so this module
drives curl itself and keeps the pool for rotation, budgets and health.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from urllib.parse import urljoin

try:
    from keel_crawler.proxy.pool import USER_AGENT, ProxyPool
    AVAILABLE = True
except ImportError:  # pragma: no cover - hosts without keel-crawler
    AVAILABLE = False
    ProxyPool = None  # type: ignore[assignment]
    USER_AGENT = "Mozilla/5.0"

MISSING_MESSAGE = (
    "the audit crawl needs keel-crawler's proxy pool. Install it with: "
    "pip install 'keel-seo[proxies]'"
)
MAX_HOPS = 6
ATTEMPTS = 7


@dataclass
class Response:
    """One resolved fetch: the final hop plus every redirect before it."""

    url: str
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: str = ""
    chain: list = field(default_factory=list)
    attempts: int = 0
    error: str = ""

    @property
    def final_url(self) -> str:
        return self.chain[-1]["to"] if self.chain else self.url


def _parse(raw: bytes) -> tuple[int, dict, str]:
    """Split curl ``-i`` output into status, lower-cased headers and body."""
    while True:
        head, sep, rest = raw.partition(b"\r\n\r\n")
        if not sep:
            return 0, {}, ""
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            return 0, {}, ""
        status = int(parts[1])
        if 100 <= status < 200:
            raw = rest
            continue
        headers: dict = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if not colon:
                continue
            key = name.strip().lower()
            value = value.strip()
            headers[key] = f"{headers[key]}, {value}" if key in headers else value
        return status, headers, rest.decode("utf-8", errors="replace")


def _curl(proxy_url: str, url: str, timeout: float) -> tuple[int, dict, str]:
    try:
        result = subprocess.run(
            ["curl", "-s", "-i", "--compressed", "--suppress-connect-headers",
             "--max-time", str(int(timeout)), "--noproxy", "", "--proxy", proxy_url,
             "-H", f"User-Agent: {USER_AGENT}",
             "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
             "-H", "Accept-Language: en-US,en;q=0.8",
             url],
            capture_output=True, timeout=timeout + 6,
        )
    except Exception:  # noqa: BLE001 - a dead proxy is an expected outcome
        return 0, {}, ""
    return _parse(result.stdout)


def _complete(status: int, headers: dict, body: str) -> bool:
    """Whether a hop is a whole answer rather than a truncated or injected one."""
    if status in (301, 302, 303, 307, 308):
        return bool(headers.get("location"))
    if status == 200 and "html" in headers.get("content-type", "").lower():
        return "</html>" in body[-2000:].lower()
    return status in (200, 204, 304, 400, 401, 403, 404, 405, 410, 451, 500, 502, 503)


def build_pool(probe_url: str, accept, *, candidates: int = 900):
    """A pool verified against the audited site itself."""
    if not AVAILABLE:
        raise SystemExit(MISSING_MESSAGE)
    return ProxyPool.build(probe_url, accept=accept, start_at=10, candidates=candidates,
                           progress=lambda *args, **kwargs: None)


def fetch(pool, url: str, *, timeout: float = 20.0, blocked=None) -> Response:
    """Fetch ``url`` and follow its redirects by hand, retrying each hop.

    ``blocked(status, headers, body)`` says whether an answer is the site
    refusing the proxy (a challenge page) rather than answering the request.
    """
    response = Response(url=url)
    current = url
    for _hop in range(MAX_HOPS):
        status, headers, body = 0, {}, ""
        for _attempt in range(ATTEMPTS):
            proxy = pool.acquire(max_wait=90)
            if proxy is None:
                response.error = "pool empty"
                return response
            response.attempts += 1
            try:
                status, headers, body = _curl(proxy.url, current, timeout)
            finally:
                pool.release(proxy)
            if blocked is not None and status and blocked(status, headers, body):
                pool.report_blocked(proxy)
                status = 0
                continue
            if _complete(status, headers, body):
                pool.report_ok(proxy)
                break
            pool.report_failure(proxy)
            status = 0
        if not status:
            response.error = response.error or "no complete answer"
            return response
        if status in (301, 302, 303, 307, 308):
            target = urljoin(current, headers["location"])
            response.chain.append({"from": current, "to": target, "status": status})
            current = target
            continue
        response.status, response.headers, response.body = status, headers, body
        return response
    response.error = "redirect loop"
    return response
