"""Read a recipe link someone sent: a recipe site, YouTube, TikTok, Instagram or anything else.

What can be read differs by site, and FridgeBud says which case it was:
- Recipe sites usually publish the full recipe as structured data. That is copied as-is.
- Video links give a title and a caption/description. FridgeBud cannot watch a video.
- Some sites (often Instagram) refuse to show anything without a login.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_BYTES = 3 * 1024 * 1024
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


class LinkError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    site: str
    title: str = ""
    text: str = ""              # caption / description / page text worth showing a model
    recipe: dict | None = None  # structured recipe published by the site, if any


def first_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,!?") if m else None


_SLUG_NOISE = {"recipe", "recipes", "best", "easy", "my", "favorite", "favourite", "the", "a", "ever", "simple", "quick",
               "homemade", "classic", "perfect", "how", "to", "make", "html", "php", "www"}


def dish_from_url(url: str) -> str:
    """A guess at the dish from the link itself: '/20144/banana-banana-bread/' -> 'banana bread'."""
    if any(social in site_of(url) for social in ("instagram.", "tiktok.", "youtube.", "youtu.be", "facebook.", "pinterest.")):
        return ""  # their links are ids, not dish names
    path = urllib.parse.urlparse(url).path
    for segment in reversed([s for s in path.split("/") if s]):
        if not re.search(r"[-_]", segment):
            continue
        words = []
        for w in re.split(r"[-_.]+", segment.lower()):
            if w.isalpha() and w not in _SLUG_NOISE and (not words or words[-1] != w):
                words.append(w)
        if len(words) >= 2:
            return " ".join(words[:4])
    return ""


def site_of(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _check_public(url: str) -> None:
    """Only fetch public web addresses, never something on the local network."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LinkError("not a web link")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise LinkError("that address doesn't exist") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise LinkError("not a public web address")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, timeout: float = 15) -> tuple[str, str]:
    _check_public(url)
    opener = urllib.request.build_opener(_SafeRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as err:
        raise LinkError(f"the site answered {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LinkError("the site didn't answer") from None


def _meta(page: str, key: str) -> str:
    for pattern in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\']',
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']'):
        m = re.search(pattern, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _find_recipe(node) -> dict | None:
    """Find a schema.org Recipe inside JSON-LD, however the site nested it."""
    if isinstance(node, list):
        for child in node:
            found = _find_recipe(child)
            if found:
                return found
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
            return node
        return _find_recipe(node.get("@graph")) if "@graph" in node else None
    return None


def _minutes(iso: str) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(iso or "").strip())
    if not m or not any(m.groups()):
        return None
    days, hours, mins = (int(g or 0) for g in m.groups())
    return days * 1440 + hours * 60 + mins


def _steps(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out += [s.strip() for s in re.split(r"\n+", html.unescape(re.sub(r"<[^>]+>", " ", node))) if s.strip()]
    elif isinstance(node, list):
        for child in node:
            out += _steps(child)
    elif isinstance(node, dict):
        if node.get("@type") == "HowToSection":
            out += _steps(node.get("itemListElement"))
        elif node.get("text"):
            out += _steps(node["text"])
    return out


def structured_recipe(page: str) -> dict | None:
    """The site's own published recipe (schema.org JSON-LD), copied without any AI."""
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            found = _find_recipe(json.loads(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        if found and found.get("recipeIngredient"):
            servings = found.get("recipeYield")
            if isinstance(servings, list):
                servings = servings[0] if servings else ""
            return {
                "title": html.unescape(str(found.get("name") or "")),
                "servings": str(servings or ""),
                "minutes": _minutes(found.get("totalTime")) or _minutes(found.get("cookTime")),
                "ingredients": [{"name": html.unescape(str(i)).strip()} for i in found["recipeIngredient"]],
                "steps": _steps(found.get("recipeInstructions")),
            }
    return None


def _youtube_description(page: str) -> str:
    m = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', page)
    if not m:
        return ""
    try:
        return json.loads(f'"{m.group(1)}"')
    except json.JSONDecodeError:
        return ""


def read_page(url: str, page: str) -> Page:
    site = site_of(url)
    title = _meta(page, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else ""
    description = _meta(page, "og:description") or _meta(page, "description")
    if "youtube." in site or site == "youtu.be":
        description = _youtube_description(page) or description
    text = "\n".join(p for p in (title, description) if p)
    return Page(url, site, title, text[:6000], structured_recipe(page))


def fetch(url: str) -> Page:
    """Download a link and pull out whatever recipe information it exposes."""
    site = site_of(url)
    if "tiktok.com" in site:
        # TikTok's public embed endpoint returns the caption without loading the page.
        try:
            _, body = _get("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe=""))
            data = json.loads(body)
            caption = str(data.get("title") or "")
            if caption:
                return Page(url, "tiktok.com", caption[:120], f"{caption}\n(by {data.get('author_name', '')})")
        except (LinkError, json.JSONDecodeError):
            pass
    final_url, page = _get(url)
    return read_page(final_url, page)
