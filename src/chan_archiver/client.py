"""Throttled HTTP client with retries and verified downloads."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import (
    DEFAULT_BACKOFF,
    DEFAULT_INTERVAL,
    HOST_INTERVALS,
    MAX_ATTEMPTS,
    MAX_INTERVAL,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

log = logging.getLogger(__name__)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class HostThrottle:
    """Spaces out requests to a single host, slowing down after a 429."""

    def __init__(self, interval: float) -> None:
        self.base_interval = interval
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next = time.monotonic() + self.interval

    def penalize(self, retry_after: float | None) -> float:
        """Record a rate-limit response. Returns the wait before retrying."""
        with self._lock:
            self.interval = min(MAX_INTERVAL, max(self.interval * 2, self.base_interval))
            wait = max(retry_after if retry_after is not None else DEFAULT_BACKOFF, self.interval)
            self._next = max(self._next, time.monotonic() + wait)
            return wait

    def reward(self) -> None:
        with self._lock:
            self.interval = max(self.base_interval, self.interval * 0.9)


class HttpClient:
    """
    requests.Session wrapper. Every request goes through a per-host
    throttle; HTTP 429/5xx and network errors are retried with backoff.
    """

    def __init__(self, host_intervals: dict[str, float] | None = None) -> None:
        self._intervals = dict(HOST_INTERVALS)
        if host_intervals:
            self._intervals.update(host_intervals)
        self._throttles: dict[str, HostThrottle] = {}
        self._throttle_lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _throttle(self, url: str) -> HostThrottle:
        host = urlparse(url).hostname or ""
        with self._throttle_lock:
            if host not in self._throttles:
                self._throttles[host] = HostThrottle(self._intervals.get(host, DEFAULT_INTERVAL))
            return self._throttles[host]

    def request(
        self, url: str, *, headers: dict[str, str] | None = None, stream: bool = False
    ) -> requests.Response | None:
        """
        GET a URL through the throttle, retrying 429/5xx/network errors.

        Returns the final response (any status other than 429/5xx), or
        None if every attempt failed. The caller must close streamed
        responses.
        """
        throttle = self._throttle(url)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            throttle.wait()
            try:
                resp = self.session.get(
                    url, headers=headers, stream=stream, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                wait = min(MAX_INTERVAL, DEFAULT_BACKOFF * attempt)
                log.warning(
                    "%s: %s (attempt %d/%d, retrying in %.0fs)",
                    url,
                    exc,
                    attempt,
                    MAX_ATTEMPTS,
                    wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                resp.close()
                wait = throttle.penalize(retry_after)
                log.warning(
                    "%s: HTTP %d (attempt %d/%d, slowing down; next try in %.0fs)",
                    url,
                    resp.status_code,
                    attempt,
                    MAX_ATTEMPTS,
                    wait,
                )
                continue

            throttle.reward()
            return resp

        log.error("Giving up on %s after %d attempts", url, MAX_ATTEMPTS)
        return None

    def get_json(self, url: str) -> dict | list | None:
        """GET JSON. Returns None on 404/failure."""
        resp = self.request(url)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                log.debug("%s: HTTP %d", url, resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            log.error("%s: invalid JSON response", url)
            return None

    def download_file(
        self,
        url: str,
        dest: Path,
        *,
        expected_size: int | None = None,
        expected_md5: str | None = None,
    ) -> bool:
        """
        Download url to dest atomically. The file is only kept when the
        server returned 200 and size/MD5 (base64, as in the 4chan API) match.
        """
        resp = self.request(url, stream=True)
        if resp is None:
            return False
        tmp = dest.with_name(dest.name + ".part")
        try:
            if resp.status_code != 200:
                log.warning("Failed to download %s: HTTP %d", url, resp.status_code)
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.md5()
            size = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except (requests.RequestException, OSError) as exc:
            log.warning("Failed to download %s: %s", url, exc)
            tmp.unlink(missing_ok=True)
            return False
        finally:
            resp.close()

        if expected_size is not None and size != expected_size:
            log.warning(
                "%s: size mismatch (got %d, expected %d); discarded", url, size, expected_size
            )
            tmp.unlink(missing_ok=True)
            return False
        if expected_md5 is not None:
            actual = base64.b64encode(digest.digest()).decode()
            if actual != expected_md5:
                log.warning("%s: MD5 mismatch; discarded", url)
                tmp.unlink(missing_ok=True)
                return False

        tmp.replace(dest)
        return True
