"""Catalog filters (boards.json)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# {board: [[regex, ...], ...]}
Filters = dict[str, list[list[str]]]


def load_filters(path: Path) -> Filters:
    """
    Read boards.json. Each board maps to a list of groups of regexes; keys
    starting with "_" (e.g. "_comment") are ignored.
    """
    if not path.exists():
        log.error("Filter file not found: %s", path)
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def match_threads(threads: Iterable[dict], filters: list[list[str]]) -> list[int]:
    """
    IDs of catalog threads whose subject or opening comment matches a filter
    group. Every regex in a group must match (case-insensitive), each one
    against either the subject or the comment.
    """
    compiled = [[re.compile(r, re.IGNORECASE) for r in group] for group in filters]
    matches = []
    for t in threads:
        title = t.get("sub", "")
        comment = t.get("com", "")
        for group in compiled:
            if all(rx.search(title) or rx.search(comment) for rx in group):
                matches.append(int(t["no"]))
                break
    return matches
