"""4chan thread URL parsing."""

from __future__ import annotations

import re

from .config import BOARDS_HOST

THREAD_URL_RE = re.compile(
    r"https?://(?:boards\.)?4chan(?:nel)?\.org/([0-9A-Za-z_]+)/(?:thread|res)/(\d+)",
    re.IGNORECASE,
)


def parse_thread_url(url: str) -> tuple[str, int]:
    """Return (board, thread_id) for a 4chan thread URL."""
    match = THREAD_URL_RE.search(url.strip())
    if not match:
        raise ValueError(f"Not a valid 4chan thread URL: {url!r}")
    return match.group(1).lower(), int(match.group(2))


def thread_url(board: str, thread_id: int) -> str:
    return f"{BOARDS_HOST}/{board}/thread/{thread_id}"


def normalize_url(url: str) -> str:
    return thread_url(*parse_thread_url(url))
