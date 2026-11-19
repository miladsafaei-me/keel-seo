"""The brand's social profiles: what the site declares, and what each profile shows in public.

Discovery reads the site's own homepage: the ``sameAs`` list of its top-level
Organization, Person or WebSite node in JSON-LD, and the links to social
platforms in its header, footer and navigation (referral links to other
companies' profiles, which carry ``utm_source``, are left out). Profiles the
owner names in ``project.json`` under ``social_profiles`` are added.

Collection opens each profile the way a visitor who is not logged in sees it:
a headless browser for profile pages, the public feed for YouTube and the public
preview for Telegram. A platform that shows nothing without a login is recorded
as ``login_wall``; no number is ever guessed. The result is ``facts/social.json``,
read by the social specialist and by ``report-data``.

Rendering needs Playwright (``pip install playwright``; the Chromium build it
names must be installed once with ``playwright install chromium``).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import requests
from lxml import html as lxml_html

from .store import Workspace

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PLATFORMS = {
    "instagram.com": "instagram", "x.com": "x", "twitter.com": "x", "facebook.com": "facebook", "fb.com": "facebook",
    "linkedin.com": "linkedin", "youtube.com": "youtube", "t.me": "telegram", "telegram.me": "telegram",
    "tiktok.com": "tiktok", "threads.net": "threads", "threads.com": "threads", "pinterest.com": "pinterest",
    "reddit.com": "reddit", "medium.com": "medium", "github.com": "github", "wikidata.org": "wikidata",
    "discord.gg": "discord", "discord.com": "discord", "aparat.com": "aparat", "bsky.app": "bluesky",
}
ENTITY_ONLY = {"wikidata", "github", "discord"}
TOP_TYPES = {"Organization", "Corporation", "NewsMediaOrganization", "OnlineBusiness", "LocalBusiness", "Person",
             "WebSite", "FinancialService", "EducationalOrganization", "Brand"}
LOGIN_WORDS = ("Log in", "Sign in", "Join LinkedIn", "Sign up")
MONTHS = 6


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def normalize(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    if host.endswith("linkedin.com"):
        host = "linkedin.com"
    path = re.sub(r"/+$", "", parts.path) or "/"
    return f"https://{host}{path}"


def number(text: str) -> int | None:
    """'1,234' -> 1234, '1.2K' -> 1200, '3M' -> 3000000."""
    if text is None:
        return None
    match = re.match(r"\s*([\d.,]+)\s*([KkMmBb]?)", str(text))
    if not match:
        return None
    digits, unit = match.groups()
    if unit:
        value = float(digits.replace(",", ""))
        return round(value * {"k": 1e3, "m": 1e6, "b": 1e9}[unit.lower()])
    return int(re.sub(r"[.,]", "", digits)) if digits.strip(".,") else None


def _top_nodes(data) -> list:
    if isinstance(data, list):
        return [node for item in data for node in _top_nodes(item)]
    if isinstance(data, dict):
        if "@graph" in data:
            return _top_nodes(data["@graph"])
        return [data]
    return []


def discover(workspace: Workspace, rendered_links: list | None = None) -> dict:
    """The profiles the site declares. ``rendered_links`` are the hrefs of the homepage after it ran its
    scripts: many sites draw the footer in the browser, so the server's HTML alone misses those links."""
    project = workspace.project
    site = project["site"].rstrip("/")
    host = urlsplit(site).netloc.lower().removeprefix("www.")
    response = requests.get(site, headers={"User-Agent": UA}, timeout=30)
    tree = lxml_html.fromstring(response.content)
    same_as, nav_links = [], []
    for block in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(block.text or "")
        except ValueError:
            continue
        for node in _top_nodes(data):
            kinds = node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]
            if not TOP_TYPES & {kind for kind in kinds if isinstance(kind, str)}:
                continue
            values = node.get("sameAs") or []
            same_as += [values] if isinstance(values, str) else [value for value in values if isinstance(value, str)]
    for href in [anchor.get("href", "") for anchor in tree.xpath("//a[@href]")] + list(rendered_links or []):
        if not platform_of(href) or "utm_source" in parse_qs(urlsplit(href).query):
            continue
        nav_links.append(href)
    profiles: dict = {}
    for source, urls in (("sameAs", same_as), ("site links", nav_links),
                         ("project card", project.get("social_profiles", []))):
        for url in urls:
            if not platform_of(url):
                continue
            key = normalize(url)
            entry = profiles.setdefault(key, {"url": url, "platform": platform_of(url), "found_in": []})
            if source not in entry["found_in"]:
                entry["found_in"].append(source)
    return {"home": response.url, "site_host": host, "same_as": same_as,
            "site_links": sorted({normalize(url) for url in nav_links}), "profiles": list(profiles.values())}


def _meta(page) -> dict:
    return page.evaluate("""() => {
        const pick = sel => (document.querySelector(sel) || {}).content || '';
        return {title: pick('meta[property="og:title"]') || document.title, description: pick('meta[property="og:description"]') || pick('meta[name="description"]'),
                text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 6000)};
    }""")


def _render(context, url: str, wait_ms: int = 5000) -> tuple[dict, object]:
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(wait_ms)
    return _meta(page), page


def _identity(name: str, url: str, brand: list) -> bool:
    """Does the profile show the brand's name? The name it displays decides; the handle only when no name shows."""
    if name:
        return any(term in name.lower() for term in brand)
    handle = urlsplit(url).path.lower()
    return any(term.replace(" ", "") in handle for term in brand)


def _base(profile: dict) -> dict:
    return {**profile, "status": "unreachable", "name": "", "identity_match": None, "followers": None, "following": None,
            "posts_total": None, "joined": "", "links_to_site": None, "recent": [], "note": ""}


def _instagram(context, profile: dict, host: str, brand: list) -> dict:
    out = _base(profile)
    meta, page = _render(context, profile["url"])
    match = re.search(r"([\d.,]+[KM]?) Followers, ([\d.,]+[KM]?) Following, ([\d.,]+[KM]?) Posts", meta["description"])
    if not match:
        page.close()
        out.update(status="login_wall" if any(word in meta["text"] for word in LOGIN_WORDS) else "unreachable")
        return out
    out.update(followers=number(match.group(1)), following=number(match.group(2)), posts_total=number(match.group(3)),
               name=re.sub(r"\s*\(@.*$", "", meta["title"]).strip(), links_to_site=host in meta["text"].lower(), status="measured")
    links = page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href*="/p/"],a[href*="/reel/"]')].map(a => a.href))].slice(0, 12)""")
    page.close()
    for link in links:
        try:
            post, post_page = _render(context, link, 3000)
            post_page.close()
        except Exception:  # noqa: BLE001 - one post failing must not lose the profile
            continue
        found = re.search(r"([\d.,]+[KM]?) likes?, ([\d.,]+[KM]?) comments? - .*? on ([A-Z][a-z]+ \d{1,2}, \d{4})", post["description"])
        if found:
            out["recent"].append({"date": datetime.strptime(found.group(3), "%B %d, %Y").date().isoformat(),
                                  "likes": number(found.group(1)), "comments": number(found.group(2)), "views": None,
                                  "links_to_site": host in post["description"].lower()})
    out["identity_match"] = _identity(out["name"], profile["url"], brand)
    if links and not out["recent"]:
        out["status"] = "partial"
        out["note"] = "Post pages did not show likes and comments without a login."
    return out


def _youtube(context, profile: dict, host: str, brand: list) -> dict:
    out = _base(profile)
    base = re.sub(r"/(videos|about|featured|shorts|streams)/?$", "", profile["url"].rstrip("/"))
    raw = requests.get(base, headers={"User-Agent": UA, "Accept-Language": "en-US,en"}, timeout=30).text
    channel = re.search(r'"externalId":"(UC[\w-]+)"', raw) or re.search(r'"channelId":"(UC[\w-]+)"', raw)
    meta, page = _render(context, base + "/about")
    page.close()
    text = meta["text"]
    out["name"] = re.sub(r"\s*-\s*YouTube$", "", meta["title"]).strip()
    subscribers = re.search(r"([\d.,]+[KM]?) subscribers?", text)
    videos = re.search(r"([\d.,]+[KM]?) videos?", text)
    joined = re.search(r"Joined ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    out.update(followers=number(subscribers.group(1)) if subscribers else (0 if "doesn't have any content" in text else None),
               posts_total=number(videos.group(1)) if videos else (0 if "doesn't have any content" in text else None),
               joined=datetime.strptime(joined.group(1), "%b %d, %Y").date().isoformat() if joined else "",
               links_to_site=host in text.lower(), status="measured" if channel else "partial")
    if channel:
        feed = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.group(1)}", timeout=30)
        names = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
        for entry in ET.fromstring(feed.content).findall("a:entry", names):
            stats = entry.find(".//m:statistics", names)
            rating = entry.find(".//m:starRating", names)
            description = entry.findtext(".//m:description", "", names)
            out["recent"].append({"date": (entry.findtext("a:published", "", names) or "")[:10],
                                  "likes": int(rating.get("count")) if rating is not None else None, "comments": None,
                                  "views": int(stats.get("views")) if stats is not None else None,
                                  "links_to_site": host in description.lower()})
    out["identity_match"] = _identity(out["name"], profile["url"], brand)
    return out


def _telegram(context, profile: dict, host: str, brand: list) -> dict:
    out = _base(profile)
    name = urlsplit(profile["url"]).path.strip("/").split("/")[-1]
    raw = requests.get(f"https://t.me/s/{name}", headers={"User-Agent": UA}, timeout=30)
    tree = lxml_html.fromstring(raw.content)
    counters = {(node.findtext('.//span[@class="counter_type"]') or "").strip(): node.findtext('.//span[@class="counter_value"]')
                for node in tree.xpath('//div[contains(@class,"tgme_header_counter")] | //div[@class="tgme_channel_info_counter"]')}
    title = (tree.xpath('string(//div[contains(@class,"tgme_channel_info_header_title")])') or "").strip()
    if not title:
        out["status"] = "unreachable"
        return out
    out.update(name=title, followers=number(counters.get("subscribers") or counters.get("members") or ""),
               posts_total=None, status="measured")
    description = tree.xpath('string(//div[contains(@class,"tgme_channel_info_description")])').lower()
    out["links_to_site"] = host in description
    for message in tree.xpath('//div[contains(@class,"tgme_widget_message ")]'):
        stamp = message.xpath('string(.//time/@datetime)')
        views = message.xpath('string(.//span[@class="tgme_widget_message_views"])')
        hrefs = " ".join(message.xpath('.//div[contains(@class,"tgme_widget_message_text")]//a/@href')).lower()
        out["recent"].append({"date": stamp[:10], "likes": None, "comments": None, "views": number(views),
                              "links_to_site": host in hrefs})
    out["identity_match"] = _identity(title, profile["url"], brand)
    return out


def _generic(context, profile: dict, host: str, brand: list) -> dict:
    out = _base(profile)
    meta, page = _render(context, profile["url"], 8000)
    page.close()
    blob = f"{meta['description']} {meta['text']}"
    followers = re.search(r"([\d.,]+[KM]?)\s+(followers|Followers|likes|subscribers|members)", blob)
    following = re.search(r"([\d.,]+[KM]?)\s+(following|Following)", blob)
    posts = re.search(r"([\d.,]+[KM]?)\s+(posts|Posts|videos|Videos)", blob)
    out["name"] = re.sub(r"\s*[|/(-].*$", "", meta["title"] or "").strip() or meta["title"]
    if not meta["text"].strip() or (not followers and any(word in meta["text"] for word in LOGIN_WORDS)):
        out["status"] = "login_wall"
        out["note"] = "The platform shows nothing measurable without a login."
    else:
        out.update(followers=number(followers.group(1)) if followers else None,
                   following=number(following.group(1)) if following else None,
                   posts_total=number(posts.group(1)) if posts else None,
                   links_to_site=host in blob.lower(), status="measured" if followers else "partial")
    if out["status"] == "login_wall":
        out["name"] = ""
    if profile["platform"] == "facebook":
        for day, month, year in re.findall(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d{1,2}) "
                                           r"(January|February|March|April|May|June|July|August|September|October|"
                                           r"November|December) (\d{4}) at", meta["text"]):
            out["recent"].append({"date": datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date().isoformat(),
                                  "likes": None, "comments": None, "views": None, "links_to_site": None})
    if profile["platform"] == "linkedin":
        out["note"] = (out["note"] + " " if out["note"] else "") + (
            "A personal profile URL (/in/), not a company page (/company/)." if "/in/" in profile["url"] else "")
    if out["status"] != "login_wall":
        out["identity_match"] = _identity(out["name"], profile["url"], brand)
    return out


COLLECTORS = {"instagram": _instagram, "youtube": _youtube, "telegram": _telegram}


def summarize(profile: dict, today: date) -> dict:
    """Posts per month for the last six months, the last post, and the mean reactions per recent post."""
    months = [f"{(today.year * 12 + today.month - 1 - offset) // 12}-{(today.year * 12 + today.month - 1 - offset) % 12 + 1:02d}"
              for offset in range(MONTHS)][::-1]
    dated = [post for post in profile["recent"] if post.get("date")]
    per_month = Counter(post["date"][:7] for post in dated)
    since = (today - timedelta(days=91)).isoformat()
    window = [post for post in dated if post["date"] >= since]

    def mean(key: str):
        values = [post[key] for post in window if post.get(key) is not None]
        return round(sum(values) / len(values), 1) if values else None
    return {"posts_by_month": {month: per_month.get(month, 0) for month in months},
            "posts_91d": len(window) if dated or profile["status"] == "measured" else None,
            "last_post": max((post["date"] for post in dated), default=""),
            "mean_likes": mean("likes"), "mean_comments": mean("comments"), "mean_views": mean("views"),
            "posts_linking_to_site": sum(1 for post in dated if post.get("links_to_site")),
            "recent_read": len(dated)}


def run(workspace: Workspace) -> dict:
    project = workspace.project
    brand = [term.lower() for term in project.get("brand_terms", [])] or [project.get("name", "").lower()]
    today = datetime.now(timezone.utc).date()
    results = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="en-US", user_agent=UA, viewport={"width": 1280, "height": 1600})
        home = context.new_page()
        home.goto(project["site"], wait_until="domcontentloaded", timeout=45000)
        home.wait_for_timeout(4000)
        rendered = home.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href)")
        scripted = home.evaluate("""() => [...document.querySelectorAll('body *:not(a)')].flatMap(e => [...e.attributes]
            .filter(a => a.name !== 'style' && /^https?:\\/\\//.test(a.value)).map(a => a.value))""")
        home.close()
        found = discover(workspace, rendered)
        found["scripted_only"] = sorted({normalize(url) for url in scripted if platform_of(url)}
                                        - set(found["site_links"]))
        host = found["site_host"]
        for profile in found["profiles"]:
            if profile["platform"] in ENTITY_ONLY:
                results.append({**_base(profile), "status": "entity", "note": "An entity record, not a social profile."})
                continue
            collector = COLLECTORS.get(profile["platform"], _generic)
            try:
                result = collector(context, profile, host, brand)
            except Exception as error:  # noqa: BLE001 - one platform failing must not stop the others
                result = {**_base(profile), "note": f"{type(error).__name__}: {str(error)[:160]}"}
            result["summary"] = summarize(result, today)
            results.append(result)
        browser.close()
    payload = {
        "note": "Public view of each profile, not logged in, read on the date below. site_links are real <a href> links "
                "from the homepage to a profile; scripted_only are profile URLs the homepage reaches only through a "
                "script (a button with a data attribute), which search engines do not follow. A login_wall status means the "
                "platform shows nothing measurable without a login; such numbers are unknown, not zero. Social "
                "referral visits to the site are not in Search Console and are not measured here.",
        "read_on": today.isoformat(), "site_host": host, "home": found["home"],
        "same_as": found["same_as"], "site_links": found["site_links"], "scripted_only": found["scripted_only"],
        "profiles": results,
    }
    path = workspace.write_fact("social", payload)
    return {"file": str(path), "profiles": [(item["platform"], item["status"], item.get("followers")) for item in results]}
