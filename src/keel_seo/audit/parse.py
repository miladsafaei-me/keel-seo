"""Turn one HTML document into the fields an audit judges.

Reads the raw server HTML only. A page whose content exists only after
JavaScript runs shows up here as thin, and the technical module lists it for a
rendered check instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from lxml import html as lxml_html

DROP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")
CHROME_TAGS = ("nav", "header", "footer", "aside", "form")
ZONES = ("nav", "header", "footer", "aside", "main", "article")
WS = re.compile(r"\s+")
WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Properties Google's documentation lists as required for a rich result.
SCHEMA_REQUIRED = {
    "Article": ("headline",),
    "NewsArticle": ("headline",),
    "BlogPosting": ("headline",),
    "BreadcrumbList": ("itemListElement",),
    "FAQPage": ("mainEntity",),
    "Product": ("name",),
    "Review": ("itemReviewed", "author"),
    "Organization": ("name",),
    "Person": ("name",),
    "VideoObject": ("name", "thumbnailUrl", "uploadDate"),
    "HowTo": ("name", "step"),
}
SCHEMA_RECOMMENDED = {
    "Article": ("author", "datePublished", "image", "dateModified"),
    "NewsArticle": ("author", "datePublished", "image", "dateModified"),
    "BlogPosting": ("author", "datePublished", "image", "dateModified"),
    "Product": ("review", "aggregateRating", "offers"),
    "Organization": ("url", "logo", "sameAs"),
    "Review": ("reviewRating", "datePublished"),
}


def clean(text: str) -> str:
    return WS.sub(" ", text or "").strip()


def normalize_url(url: str, base: str = "") -> str:
    """Absolute URL with no fragment, a lower-cased host and no trailing ``?``."""
    absolute = urljoin(base, (url or "").strip()) if base else (url or "").strip()
    absolute, _fragment = urldefrag(absolute)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return ""
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def same_site(url: str, host: str) -> bool:
    netloc = urlsplit(url).netloc.lower()
    return netloc == host or netloc == f"www.{host}" or f"www.{netloc}" == host


def _zone(element) -> str:
    for ancestor in element.iterancestors():
        if ancestor.tag in ZONES:
            return "main" if ancestor.tag == "article" else ancestor.tag
    return "body"


def _flatten_schema(node, found: list) -> None:
    if isinstance(node, list):
        for item in node:
            _flatten_schema(item, found)
    elif isinstance(node, dict):
        if "@graph" in node:
            _flatten_schema(node["@graph"], found)
        if "@type" in node:
            found.append(node)
        for key, value in node.items():
            if key != "@graph" and isinstance(value, (dict, list)):
                _flatten_schema(value, found)


def _schema(tree) -> tuple[list, list, dict]:
    """Types present, problems found, and the article-like node if there is one."""
    types: list = []
    issues: list = []
    article: dict = {}
    for block in tree.xpath('//script[@type="application/ld+json"]'):
        raw = (block.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            issues.append("JSON-LD block does not parse")
            continue
        nodes: list = []
        _flatten_schema(data, nodes)
        for node in nodes:
            kinds = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
            for kind in kinds:
                if not isinstance(kind, str):
                    continue
                types.append(kind)
                for prop in SCHEMA_REQUIRED.get(kind, ()):
                    if not node.get(prop):
                        issues.append(f"{kind}: required property {prop} is missing")
                for prop in SCHEMA_RECOMMENDED.get(kind, ()):
                    if not node.get(prop):
                        issues.append(f"{kind}: recommended property {prop} is missing")
                if kind in ("Article", "NewsArticle", "BlogPosting", "Review") and not article:
                    article = node
    return sorted(set(types)), sorted(set(issues)), article


def _name(value) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_name(item) for item in value)))
    if isinstance(value, dict):
        return clean(str(value.get("name") or ""))
    return clean(str(value or ""))


def parse_page(url: str, body: str, host: str) -> tuple[dict, list]:
    """Return the page's fields and its outgoing links."""
    try:
        tree = lxml_html.fromstring(body)
    except Exception:  # noqa: BLE001 - an unparseable page is a finding, not a crash
        return {"error": "html does not parse"}, []

    def meta(name: str, attr: str = "name") -> str:
        hit = tree.xpath(f'//meta[translate(@{attr},"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                         f'"abcdefghijklmnopqrstuvwxyz")="{name}"]/@content')
        return clean(hit[0]) if hit else ""

    titles = tree.xpath("//head/title/text()") or tree.xpath("//title/text()")
    canonical = tree.xpath('//link[translate(@rel,"CANOL","canol")="canonical"]/@href')
    schema_types, schema_issues, article = _schema(tree)
    hreflang = [
        {"lang": link.get("hreflang"), "href": normalize_url(link.get("href", ""), url)}
        for link in tree.xpath('//link[@rel="alternate"][@hreflang]')
    ]

    links: list = []
    for anchor in tree.xpath("//a[@href]"):
        target = normalize_url(anchor.get("href", ""), url)
        if not target:
            continue
        text = clean(anchor.text_content())
        if not text:
            alt = anchor.xpath(".//img/@alt")
            text = clean(alt[0]) if alt else ""
        links.append({
            "dst": target, "anchor": text[:200], "rel": (anchor.get("rel") or "").lower(),
            "zone": _zone(anchor), "internal": int(same_site(target, host)),
        })

    images = tree.xpath("//img")
    img_noalt = sum(1 for img in images if not (img.get("alt") or "").strip())

    for tag in DROP_TAGS:
        for element in tree.xpath(f"//{tag}"):
            element.drop_tree()
    outline = [
        [int(heading.tag[1]), clean(heading.text_content())[:160]]
        for heading in tree.xpath("//h1|//h2|//h3|//h4|//h5|//h6")
    ]
    roots = tree.xpath("//main") or tree.xpath("//article") or tree.xpath("//body")
    root = roots[0] if roots else tree
    tables = len(root.xpath(".//table"))
    lists = len(root.xpath(".//ul|.//ol"))
    paragraphs = [clean(p.text_content()) for p in root.xpath(".//p")]
    first_para = next((p for p in paragraphs if len(WORD.findall(p)) >= 8), "")
    for tag in CHROME_TAGS:
        for element in root.xpath(f".//{tag}"):
            element.drop_tree()
    main_text = clean(root.text_content())
    words = WORD.findall(main_text)

    fields = {
        "title": clean(titles[0]) if titles else "",
        "meta_desc": meta("description"),
        "robots_meta": meta("robots").lower(),
        "canonical": normalize_url(canonical[0], url) if canonical else "",
        "h1": json.dumps([text for level, text in outline if level == 1], ensure_ascii=False),
        "outline": json.dumps(outline[:120], ensure_ascii=False),
        "word_count": len(words),
        "text_hash": hashlib.sha1(main_text.encode("utf-8")).hexdigest() if main_text else "",
        "lang": (tree.get("lang") or "").lower(),
        "hreflang": json.dumps(hreflang, ensure_ascii=False),
        "og": json.dumps({key: meta(f"og:{key}", "property") for key in ("title", "description", "image", "type")},
                         ensure_ascii=False),
        "schema_types": json.dumps(schema_types),
        "schema_issues": json.dumps(schema_issues),
        "author": _name(article.get("author")) or meta("author"),
        "published": clean(str(article.get("datePublished") or "")) or meta("article:published_time", "property"),
        "modified": clean(str(article.get("dateModified") or "")) or meta("article:modified_time", "property"),
        "img_count": len(images),
        "img_noalt": img_noalt,
        "tables": tables,
        "lists": lists,
        "first_para_words": len(WORD.findall(first_para)),
        "main_text": main_text[:60000],
    }
    return fields, links
