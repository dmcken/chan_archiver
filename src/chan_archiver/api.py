"""Read-only 4chan API client."""

from __future__ import annotations

from enum import Enum

from .client import HttpClient
from .config import API_HOST
from .filters import match_threads


class ThreadStatus(Enum):
    ALIVE = "alive"  # Thread is live; archived successfully
    ARCHIVED = "archived"  # 4chan archived (closed) it; no more changes
    DEAD = "dead"  # 404
    ERROR = "error"  # Could not determine / fetch failed


class FourChanAPI:
    """
    4chan JSON API.

    Example:

        api = FourChanAPI()
        for t in api.get_threads("b"):
            print(t["no"], t.get("sub", ""))
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    @staticmethod
    def thread_json_url(board: str, thread_id: int) -> str:
        return f"{API_HOST}/{board}/thread/{thread_id}.json"

    def get_boards(self) -> list[dict]:
        data = self.http.get_json(f"{API_HOST}/boards.json")
        return data.get("boards", []) if isinstance(data, dict) else []

    def get_catalog(self, board: str) -> list[dict]:
        """Raw catalog: a list of pages, each with a 'threads' list."""
        data = self.http.get_json(f"{API_HOST}/{board}/catalog.json")
        return data if isinstance(data, list) else []

    def get_threads(self, board: str) -> list[dict]:
        """All threads currently on a board (catalog OP entries)."""
        return [t for page in self.get_catalog(board) for t in page.get("threads", [])]

    def get_thread(self, board: str, thread_id: int) -> dict | None:
        """Thread JSON ({'posts': [...]}), or None if it 404'd / failed."""
        data = self.http.get_json(self.thread_json_url(board, thread_id))
        return data if isinstance(data, dict) else None

    def thread_status(self, board: str, thread_id: int) -> ThreadStatus:
        resp = self.http.request(self.thread_json_url(board, thread_id))
        if resp is None:
            return ThreadStatus.ERROR
        if resp.status_code == 404:
            return ThreadStatus.DEAD
        if resp.status_code != 200:
            return ThreadStatus.ERROR
        try:
            op = resp.json()["posts"][0]
        except (ValueError, KeyError, IndexError):
            return ThreadStatus.ERROR
        return ThreadStatus.ARCHIVED if op.get("archived") else ThreadStatus.ALIVE

    def thread_exists(self, board: str, thread_id: int) -> bool | None:
        """True if live/archived on 4chan, False if 404, None if unknown."""
        status = self.thread_status(board, thread_id)
        if status is ThreadStatus.ERROR:
            return None
        return status is not ThreadStatus.DEAD

    def find_threads(self, board: str, filters: list[list[str]]) -> list[int]:
        """Thread IDs on board matching a filter group (see filters.match_threads)."""
        return match_threads(self.get_threads(board), filters)
