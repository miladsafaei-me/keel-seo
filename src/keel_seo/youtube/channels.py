"""Turn whatever a human pasted into a channel id, and prove it is the right one.

A channel id is the only stable address YouTube offers. Handles are not: they
are claimed, renamed and squatted, and the resolution silently succeeds on the
wrong channel when one has been taken. Measured 2026-09-21: ``youtube.com/@FTMO``
resolves, returns HTTP 200, and is a toy-unboxing channel called "An Toys TV".
FTMO's own channel is ``@FTMOcom``. Nothing in the response says the first one is
wrong, and a resolver that returns a bare id would have fed that id to every
later step.

So every resolution carries the channel's own title back with it, and the
caller is expected to look. :func:`resolve_many` goes further and fails a line
whose ``expect`` label cannot be reconciled with the title that came back,
because a registry built once and trusted for a year is exactly where a silent
swap does its damage.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Iterable

# A desktop UA. The channel page served to an unknown client is a stub with no
# externalId in it, so this is not politeness theatre - without it the resolver
# returns "not found" for every channel that exists.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
EXTERNAL_ID = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')


class ChannelResolutionError(RuntimeError):
    """Raised when an input cannot be turned into a verified channel."""


@dataclass(frozen=True)
class Channel:
    """One resolved channel: the id, and the title that proves which one it is."""

    channel_id: str
    title: str
    source: str

    def as_dict(self) -> dict:
        return asdict(self)


def _normalise(target: str) -> str:
    """Return the channel page URL for a handle, a URL or a bare id."""
    target = (target or "").strip()
    if not target:
        raise ChannelResolutionError("empty channel reference")
    if CHANNEL_ID.fullmatch(target):
        return f"https://www.youtube.com/channel/{target}"
    if target.startswith("@"):
        return f"https://www.youtube.com/{target}"
    if target.startswith(("http://", "https://")):
        return target
    if target.startswith(("channel/", "c/", "user/", "@")):
        return f"https://www.youtube.com/{target}"
    return f"https://www.youtube.com/@{target}"


def _get(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def resolve(target: str, timeout: float = 25.0) -> Channel:
    """Resolve one handle, URL or id to a :class:`Channel`.

    The id is read from ``externalId`` rather than from the URL, because a
    handle URL does not contain one and a vanity URL can redirect. The title is
    read from ``og:title`` in the same response, so the two always describe the
    same fetch - reading the title from a second request would open a window in
    which they disagree.
    """
    url = _normalise(target)
    try:
        body = _get(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ChannelResolutionError(
                f"{target!r}: no such channel (HTTP 404 at {url})") from exc
        raise ChannelResolutionError(f"{target!r}: HTTP {exc.code} at {url}") from exc
    except OSError as exc:
        raise ChannelResolutionError(f"{target!r}: {exc}") from exc

    match = EXTERNAL_ID.search(body)
    if not match:
        raise ChannelResolutionError(
            f"{target!r}: no channel id in the response from {url} "
            f"({len(body)} bytes). A short body usually means YouTube served an "
            f"error stub rather than a channel page.")
    title = OG_TITLE.search(body)
    return Channel(
        channel_id=match.group(1),
        title=(title.group(1) if title else "").strip(),
        source=url,
    )


def _reconcilable(expected: str, actual: str) -> bool:
    """Whether a resolved title can plausibly be the brand that was asked for.

    Deliberately loose. Brands write their channel title as "topstep",
    "Funding Pips" or "Apex Trader Funding" against registry names of "Topstep",
    "FundingPips" and "Apex Trader Funding", so an equality test would reject
    every correct answer. What it has to catch is the case this function exists
    for: a completely unrelated channel, which shares no alphanumeric run with
    the name asked for.
    """
    def squash(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    want, got = squash(expected), squash(actual)
    if not want or not got:
        return False
    if want in got or got in want:
        return True
    # A brand written as two words against a title written as one, or the other
    # way round: accept when the longer contains every word of the shorter.
    words = [squash(part) for part in re.split(r"[\s_-]+", expected) if part]
    return bool(words) and all(word and word in got for word in words)


def resolve_many(entries: Iterable[str], timeout: float = 25.0) -> tuple[list[Channel], list[str]]:
    """Resolve a registry file's lines, returning the channels and the problems.

    Each line is ``<handle or url>`` or ``<handle or url> | <expected name>``.
    Where an expected name is given it is checked against the title that came
    back, and a mismatch is reported rather than resolved - see this module's
    docstring for the measurement that made that non-optional.
    """
    resolved: list[Channel] = []
    problems: list[str] = []
    for raw in entries:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        target, _, expected = line.partition("|")
        target, expected = target.strip(), expected.strip()
        try:
            channel = resolve(target, timeout=timeout)
        except ChannelResolutionError as exc:
            problems.append(str(exc))
            continue
        if expected and not _reconcilable(expected, channel.title):
            problems.append(
                f"{target!r} resolved to {channel.channel_id} titled "
                f"{channel.title!r}, which does not look like {expected!r}. "
                f"Check the handle before trusting it.")
            continue
        resolved.append(channel)
    return resolved, problems


def load_registry(path: str) -> list[Channel]:
    """Read a resolved-channel registry written by the CLI."""
    with open(path, encoding="utf-8") as handle:
        return [Channel(**row) for row in json.load(handle)["channels"]]


def save_registry(path: str, channels: list[Channel]) -> None:
    """Write the registry, ids and titles together so a swap stays visible."""
    payload = {"channels": [channel.as_dict() for channel in channels]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
