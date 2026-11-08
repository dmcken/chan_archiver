"""Thread HTML rewriting and link extraction."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

# Same substitutions BASC-Archiver applied to the saved page.
_IMG_URL_RE = re.compile(r"https?://i\.4cdn\.org/\w+/(\d+\.[A-Za-z0-9]+)")
_THUMB_URL_RE = re.compile(r"https?://i\.4cdn\.org/\w+/(\d+s\.[A-Za-z0-9]+)")
_CSS_URL_RE = re.compile(r"https?://s\.4cdn\.org/css/")
_JS_URL_RE = re.compile(r"https?://s\.4cdn\.org/js/")
_CSS_FILE_RE = re.compile(r"//s\.4cdn\.org/css/([\w.]+\.css)")
_JS_FILE_RE = re.compile(r"//s\.4cdn\.org/js/([\w.]+\.js)")

_WBR_RE = re.compile(r"<wbr\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_EXTERNAL_URL_RE = re.compile(r"(?:[a-z][\w+.-]*://|www\d{0,3}\.)[^\s<>\"']+", re.IGNORECASE)


def static_assets(page: str) -> tuple[list[str], list[str]]:
    """(css_files, js_files) referenced from s.4cdn.org, sorted and unique."""
    return sorted(set(_CSS_FILE_RE.findall(page))), sorted(set(_JS_FILE_RE.findall(page)))


def rewrite_html(page: str) -> str:
    """Point images, thumbnails, CSS and JS at the local copies."""
    page = page.replace('"//', '"https://')
    page = _IMG_URL_RE.sub(r"images/\1", page)
    page = _THUMB_URL_RE.sub(r"thumbs/\1", page)
    page = _CSS_URL_RE.sub("css/", page)
    return _JS_URL_RE.sub("js/", page)


def comment_text(comment: str) -> str:
    """Plain text of a post comment (API 'com' HTML)."""
    text = _WBR_RE.sub("", comment)
    text = _BR_RE.sub("\n", text)
    return html.unescape(_TAG_RE.sub("", text))


def external_links(posts: Iterable[dict]) -> list[str]:
    """URLs mentioned in post comments, in order of appearance."""
    links: list[str] = []
    for post in posts:
        comment = post.get("com")
        if comment:
            links.extend(_EXTERNAL_URL_RE.findall(comment_text(comment)))
    return links
