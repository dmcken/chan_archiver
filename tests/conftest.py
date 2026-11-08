"""Shared fixtures: a fake requests session so nothing touches the network."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from chan_archiver import Archiver
from chan_archiver.client import HttpClient
from chan_archiver.config import HOST_INTERVALS

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: bytes | str | dict | list = b"",
        headers: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}
        self.url = url
        self.closed = False

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size: int = 1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True


Route = FakeResponse | list[FakeResponse] | Callable[..., FakeResponse]


class FakeSession:
    """
    Maps URL -> response. A list of responses is consumed one per request
    (the last one repeats). Unknown URLs return 404.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404, url=url)
        if callable(route):
            resp = route(url, **kwargs)
        elif isinstance(route, list):
            resp = route.pop(0) if len(route) > 1 else route[0]
        else:
            resp = route
        resp.url = url
        return resp

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


@pytest.fixture
def no_sleep(monkeypatch):
    """Make retries/backoff instant; returns the list of requested sleeps."""
    sleeps: list[float] = []
    monkeypatch.setattr("chan_archiver.client.time.sleep", sleeps.append)
    return sleeps


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def http(session, no_sleep) -> HttpClient:
    client = HttpClient({host: 0.0 for host in HOST_INTERVALS})
    client.session = session
    return client


@pytest.fixture
def archiver(tmp_path, http) -> Archiver:
    arch = Archiver(tmp_path)
    arch.api.http = http
    return arch
