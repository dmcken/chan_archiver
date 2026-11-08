#!/usr/bin/env python3
"""
4chan thread archiver.

Keeps a list of 4chan threads to watch (threads.txt) and archives each one
into archive/4chan/<board>/<thread_id>/. When a thread dies (404 or
archived by 4chan) its directory is moved to
completed/4chan/<board>/<semantic_url>-<thread_id>/ and the thread is
removed from threads.txt.

Each archived thread contains (same layout BASC-Archiver produced):

    <id>.json            Pretty-printed 4chan API JSON
    <id>.html            Thread HTML with links rewritten to local copies
    css/, js/            Stylesheets/scripts referenced by the HTML
    images/              Full-size files
    thumbs/              Thumbnails (only with --thumbs)
    external_links.txt   URLs found in post comments

All HTTP requests go through a per-host throttle. HTTP 429/5xx responses
are retried with backoff (honouring Retry-After), and downloads are written
to a temporary file and checked against the size/MD5 in the API before
they are kept. A "Too Many Requests" body is never saved as an image.

Usage:

    chan.py                       Interactive menu
    chan.py add URL [URL ...]     Add thread(s) and archive them once
    chan.py run                   Archive every listed thread once, then
                                  move dead archives to completed/
    chan.py fetch [BOARD ...]     Search catalogs using boards.json and
                                  add/archive new matching threads
    chan.py monitor               Keep running 'run' until the list is empty
    chan.py list                  Show listed threads
    chan.py remove URL [URL ...]  Stop watching thread(s)
    chan.py clean                 Move dead threads from archive/ to completed/

From Python:

    from chan import FourChan
    chan = FourChan()
    threads = chan.get_threads('b')
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import email.utils
import fcntl
import hashlib
import html
import json
import logging
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

USER_AGENT = "Mozilla/5.0 (compatible; chan-archiver/2.0)"

# Minimum seconds between requests to each host. The 4chan API rules ask
# for no more than one request per second.
HOST_INTERVALS = {
    "a.4cdn.org": 1.0,          # API (thread JSON, catalogs)
    "boards.4chan.org": 1.0,    # Thread HTML
    "i.4cdn.org": 0.5,          # Images / thumbnails
    "s.4cdn.org": 0.5,          # CSS / JS
}
DEFAULT_INTERVAL = 1.0

# After a 429 the interval for that host is doubled (up to this cap) and
# then slowly relaxes back towards the base interval on success.
MAX_INTERVAL = 30.0

# Attempts per request before giving up (the file is retried next run).
MAX_ATTEMPTS = 6

# Default wait when a 429/5xx has no Retry-After header.
DEFAULT_BACKOFF = 10.0

REQUEST_TIMEOUT = 30

# Seconds between passes in monitor mode.
MONITOR_INTERVAL = 90

API_HOST = "https://a.4cdn.org"
BOARDS_HOST = "https://boards.4chan.org"
IMAGE_HOST = "https://i.4cdn.org"
STATIC_HOST = "https://s.4cdn.org"

SITE_DIR_NAME = "4chan"

log = logging.getLogger("chan")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

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


# HTML rewriting (same substitutions BASC applied to the saved page).
_IMG_URL_RE = re.compile(r"https?://i\.4cdn\.org/\w+/(\d+\.[A-Za-z0-9]+)")
_THUMB_URL_RE = re.compile(r"https?://i\.4cdn\.org/\w+/(\d+s\.[A-Za-z0-9]+)")
_CSS_URL_RE = re.compile(r"https?://s\.4cdn\.org/css/")
_JS_URL_RE = re.compile(r"https?://s\.4cdn\.org/js/")
_CSS_FILE_RE = re.compile(r"//s\.4cdn\.org/css/([\w.]+\.css)")
_JS_FILE_RE = re.compile(r"//s\.4cdn\.org/js/([\w.]+\.js)")

_WBR_RE = re.compile(r"<wbr\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_EXTERNAL_URL_RE = re.compile(
    r"(?:[a-z][\w+.-]*://|www\d{0,3}\.)[^\s<>\"']+", re.IGNORECASE
)


def _parse_retry_after(value: str | None) -> float | None:
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


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

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
            wait = max(retry_after if retry_after is not None else DEFAULT_BACKOFF,
                       self.interval)
            self._next = max(self._next, time.monotonic() + wait)
            return wait

    def reward(self) -> None:
        with self._lock:
            self.interval = max(self.base_interval, self.interval * 0.9)


# ---------------------------------------------------------------------------
# Thread list (threads.txt)
# ---------------------------------------------------------------------------

class ThreadList:
    """threads.txt, safe to edit from several processes at once."""

    # Files written by the old add-thread.sh / run-all.py; merged on read.
    LEGACY_FILES = ("toadd.txt", "toadd.tmp")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_name(path.name + ".lock")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_file(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        urls = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                urls.append(normalize_url(line))
            except ValueError:
                log.warning("Ignoring invalid line in %s: %s", path.name, line)
        return urls

    def _write(self, urls: Iterable[str]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(f"{u}\n" for u in urls), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> list[str]:
        """Read threads.txt (merging legacy queue files). Lock must be held."""
        urls = self._read_file(self.path)
        merged = False
        for name in self.LEGACY_FILES:
            legacy = self.path.with_name(name)
            if legacy.exists():
                urls.extend(self._read_file(legacy))
                merged = True
        urls = list(dict.fromkeys(urls))  # de-duplicate, keep order
        if merged:
            self._write(urls)
            for name in self.LEGACY_FILES:
                self.path.with_name(name).unlink(missing_ok=True)
        return urls

    def all(self) -> list[str]:
        with self._locked():
            return self._load()

    def add(self, url: str) -> bool:
        url = normalize_url(url)
        with self._locked():
            urls = self._load()
            if url in urls:
                return False
            urls.append(url)
            self._write(urls)
            return True

    def remove(self, url: str) -> bool:
        url = normalize_url(url)
        with self._locked():
            urls = self._load()
            if url not in urls:
                return False
            self._write(u for u in urls if u != url)
            return True


# ---------------------------------------------------------------------------
# 4chan client / archiver
# ---------------------------------------------------------------------------

class ThreadStatus(Enum):
    ALIVE = "alive"         # Thread is live; archived successfully
    ARCHIVED = "archived"   # 4chan archived (closed) it; no more changes
    DEAD = "dead"           # 404
    ERROR = "error"         # Could not determine / fetch failed


@dataclass
class ArchiveResult:
    status: ThreadStatus
    new_files: int = 0
    failed_files: int = 0
    errors: list[str] = field(default_factory=list)


class FourChan:
    """
    4chan API client and thread archiver.

    Example:

        chan = FourChan()
        for t in chan.get_threads('b'):
            print(t['no'], t.get('sub', ''))
        chan.archive_thread('b', 123456789)
    """

    def __init__(
        self,
        base_dir: Path | str = BASE_DIR,
        *,
        thumbs: bool = False,
        save_html: bool = True,
        host_intervals: dict[str, float] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.archive_dir = self.base_dir / "archive" / SITE_DIR_NAME
        self.completed_dir = self.base_dir / "completed" / SITE_DIR_NAME
        self.threads = ThreadList(self.base_dir / "threads.txt")
        self.filters_file = self.base_dir / "boards.json"

        self.thumbs = thumbs
        self.save_html = save_html

        self._intervals = dict(HOST_INTERVALS)
        if host_intervals:
            self._intervals.update(host_intervals)
        self._throttles: dict[str, HostThrottle] = {}
        self._throttle_lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # -- HTTP --------------------------------------------------------------

    def _throttle(self, url: str) -> HostThrottle:
        host = urlparse(url).hostname or ""
        with self._throttle_lock:
            if host not in self._throttles:
                self._throttles[host] = HostThrottle(
                    self._intervals.get(host, DEFAULT_INTERVAL)
                )
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
                log.warning("%s: %s (attempt %d/%d, retrying in %.0fs)",
                            url, exc, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                resp.close()
                wait = throttle.penalize(retry_after)
                log.warning("%s: HTTP %d (attempt %d/%d, slowing down; next try in %.0fs)",
                            url, resp.status_code, attempt, MAX_ATTEMPTS, wait)
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
        server returned 200 and size/MD5 (if given) match.
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
            log.warning("%s: size mismatch (got %d, expected %d); discarded",
                        url, size, expected_size)
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

    # -- API ---------------------------------------------------------------

    def get_boards(self) -> list[dict]:
        data = self.get_json(f"{API_HOST}/boards.json")
        return data.get("boards", []) if isinstance(data, dict) else []

    def get_catalog(self, board: str) -> list[dict]:
        """Raw catalog: a list of pages, each with a 'threads' list."""
        data = self.get_json(f"{API_HOST}/{board}/catalog.json")
        return data if isinstance(data, list) else []

    def get_threads(self, board: str) -> list[dict]:
        """All threads currently on a board (catalog OP entries)."""
        return [t for page in self.get_catalog(board) for t in page.get("threads", [])]

    def get_thread(self, board: str, thread_id: int) -> dict | None:
        """Thread JSON ({'posts': [...]}), or None if it 404'd / failed."""
        data = self.get_json(f"{API_HOST}/{board}/thread/{thread_id}.json")
        return data if isinstance(data, dict) else None

    def thread_status(self, board: str, thread_id: int) -> ThreadStatus:
        resp = self.request(f"{API_HOST}/{board}/thread/{thread_id}.json")
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
        """
        Thread IDs on board whose subject or opening comment matches a
        filter group. Every regex in a group must match (each one may hit
        either the subject or the comment).
        """
        compiled = [[re.compile(r, re.IGNORECASE) for r in group] for group in filters]
        matches = []
        for t in self.get_threads(board):
            title = t.get("sub", "")
            comment = t.get("com", "")
            for group in compiled:
                if all(rx.search(title) or rx.search(comment) for rx in group):
                    matches.append(int(t["no"]))
                    break
        return matches

    def load_filters(self) -> dict[str, list[list[str]]]:
        if not self.filters_file.exists():
            log.error("Filter file not found: %s", self.filters_file)
            return {}
        data = json.loads(self.filters_file.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}

    # -- Archiving ---------------------------------------------------------

    def thread_dir(self, board: str, thread_id: int) -> Path:
        return self.archive_dir / board / str(thread_id)

    def _fetch_thread_json(self, board: str, thread_id: int, json_path: Path
                           ) -> tuple[ThreadStatus | None, dict | None, bool]:
        """
        Conditional GET of thread JSON. Returns (status, data, changed).
        status is DEAD/ERROR on failure, otherwise None.
        """
        headers = {}
        if json_path.exists():
            headers["If-Modified-Since"] = email.utils.formatdate(
                json_path.stat().st_mtime, usegmt=True
            )
        resp = self.request(f"{API_HOST}/{board}/thread/{thread_id}.json", headers=headers)
        if resp is None:
            return ThreadStatus.ERROR, None, False
        if resp.status_code == 404:
            return ThreadStatus.DEAD, None, False
        if resp.status_code == 304:
            try:
                return None, json.loads(json_path.read_text(encoding="utf-8")), False
            except (OSError, ValueError):
                # Local copy unreadable; fetch again unconditionally.
                json_path.unlink(missing_ok=True)
                return self._fetch_thread_json(board, thread_id, json_path)
        if resp.status_code != 200:
            log.warning("%s: unexpected HTTP %d", resp.url, resp.status_code)
            return ThreadStatus.ERROR, None, False
        try:
            data = resp.json()
            data["posts"][0]
        except (ValueError, KeyError, IndexError):
            log.warning("%s: invalid thread JSON", resp.url)
            return ThreadStatus.ERROR, None, False
        return None, data, True

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, sort_keys=True, indent=2, separators=(",", ": ")),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _write_external_links(self, thread_dir: Path, posts: list[dict]) -> None:
        links = []
        for post in posts:
            comment = post.get("com")
            if not comment:
                continue
            text = _WBR_RE.sub("", comment)
            text = _BR_RE.sub("\n", text)
            text = html.unescape(_TAG_RE.sub("", text))
            links.extend(_EXTERNAL_URL_RE.findall(text))
        (thread_dir / "external_links.txt").write_text(
            "".join(f"{u}\n" for u in links), encoding="utf-8"
        )

    def _save_html(self, board: str, thread_id: int, thread_dir: Path) -> None:
        resp = self.request(thread_url(board, thread_id))
        if resp is None or resp.status_code != 200:
            log.warning("[/%s/%d] Could not fetch thread HTML", board, thread_id)
            return
        page = resp.text

        for name in sorted(set(_CSS_FILE_RE.findall(page))):
            dest = thread_dir / "css" / name
            if not dest.exists():
                self.download_file(f"{STATIC_HOST}/css/{name}", dest)
        for name in sorted(set(_JS_FILE_RE.findall(page))):
            dest = thread_dir / "js" / name
            if not dest.exists():
                self.download_file(f"{STATIC_HOST}/js/{name}", dest)

        page = page.replace('"//', '"https://')
        page = _IMG_URL_RE.sub(r"images/\1", page)
        page = _THUMB_URL_RE.sub(r"thumbs/\1", page)
        page = _CSS_URL_RE.sub("css/", page)
        page = _JS_URL_RE.sub("js/", page)

        out = thread_dir / f"{thread_id}.html"
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(out)

    def _download_media(self, board: str, posts: list[dict], thread_dir: Path,
                        result: ArchiveResult) -> None:
        for post in posts:
            if "tim" not in post or post.get("filedeleted"):
                continue
            tim, ext = post["tim"], post["ext"]

            image = thread_dir / "images" / f"{tim}{ext}"
            size = post.get("fsize")
            # A file of the wrong size is a partial or error-page download.
            if not image.exists() or (size is not None and image.stat().st_size != size):
                if self.download_file(f"{IMAGE_HOST}/{board}/{tim}{ext}", image,
                                      expected_size=size, expected_md5=post.get("md5")):
                    result.new_files += 1
                    log.info("    image %s", image.name)
                else:
                    result.failed_files += 1

            if self.thumbs:
                thumb = thread_dir / "thumbs" / f"{tim}s.jpg"
                if not thumb.exists() or thumb.stat().st_size == 0:
                    if self.download_file(f"{IMAGE_HOST}/{board}/{tim}s.jpg", thumb):
                        result.new_files += 1
                    else:
                        result.failed_files += 1

    def archive_thread(self, board: str, thread_id: int) -> ArchiveResult:
        """
        Archive the current state of a thread once (like BASC --runonce).
        Unchanged threads (HTTP 304) skip the JSON/HTML rewrite but still
        get any missing or broken files fetched.
        """
        thread_dir = self.thread_dir(board, thread_id)
        json_path = thread_dir / f"{thread_id}.json"

        status, data, changed = self._fetch_thread_json(board, thread_id, json_path)
        if status is not None:
            return ArchiveResult(status)

        posts = data["posts"]
        thread_dir.mkdir(parents=True, exist_ok=True)

        if changed:
            log.info("[/%s/%d] %d posts", board, thread_id, len(posts))
            self._write_json(json_path, data)
            self._write_external_links(thread_dir, posts)
            if self.save_html:
                self._save_html(board, thread_id, thread_dir)
        else:
            log.info("[/%s/%d] no new posts", board, thread_id)

        result = ArchiveResult(
            ThreadStatus.ARCHIVED if posts[0].get("archived") else ThreadStatus.ALIVE
        )
        self._download_media(board, posts, thread_dir, result)
        return result

    def move_to_completed(self, board: str, thread_id: int) -> Path | None:
        """Move archive/4chan/b/123 -> completed/4chan/b/<semantic_url>-123."""
        src = self.thread_dir(board, thread_id)
        if not src.is_dir():
            return None

        name = str(thread_id)
        json_path = src / f"{thread_id}.json"
        try:
            op = json.loads(json_path.read_text(encoding="utf-8"))["posts"][0]
            if op.get("semantic_url"):
                name = f"{op['semantic_url']}-{thread_id}"
        except (OSError, ValueError, KeyError, IndexError):
            log.warning("[/%s/%d] No readable JSON; using bare thread id", board, thread_id)

        dest = self.completed_dir / board / name
        if dest.exists():
            log.error("[/%s/%d] %s already exists; leaving archive in place",
                      board, thread_id, dest)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        for leftover in dest.rglob("*.part"):
            leftover.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            src.parent.rmdir()  # Remove the board dir if now empty
        log.info("[/%s/%d] Moved to %s", board, thread_id, dest)
        return dest

    # -- High level operations ---------------------------------------------

    def process_thread(self, url: str) -> ArchiveResult:
        """Archive a listed thread once; retire it if it's finished."""
        board, thread_id = parse_thread_url(url)
        result = self.archive_thread(board, thread_id)

        if result.status is ThreadStatus.DEAD:
            log.info("[/%s/%d] 404'd", board, thread_id)
        elif result.status is ThreadStatus.ARCHIVED:
            log.info("[/%s/%d] Archived by 4chan (final copy saved)", board, thread_id)
        elif result.status is ThreadStatus.ERROR:
            log.warning("[/%s/%d] Could not fetch; will retry next run", board, thread_id)

        if result.status in (ThreadStatus.DEAD, ThreadStatus.ARCHIVED):
            if self.thread_dir(board, thread_id).exists():
                self.move_to_completed(board, thread_id)
            self.threads.remove(url)
        return result

    def add(self, url: str) -> ArchiveResult:
        url = normalize_url(url)
        if self.threads.add(url):
            log.info("Added %s", url)
        else:
            log.info("Already listed: %s", url)
        return self.process_thread(url)

    def run_all(self) -> None:
        """Archive every listed thread once, then clean up dead archives."""
        urls = self.threads.all()
        log.info("Thread count: %d", len(urls))
        for i, url in enumerate(urls, 1):
            log.info("(%d/%d) %s", i, len(urls), url)
            self.process_thread(url)
        self.clean(skip=set(urls))

    def fetch(self, boards: Iterable[str] | None = None) -> list[str]:
        """Search catalogs with boards.json filters and archive new threads."""
        filters = self.load_filters()
        if boards:
            missing = [b for b in boards if b not in filters]
            for b in missing:
                log.warning("No filters for /%s/ in %s", b, self.filters_file.name)
            filters = {b: filters[b] for b in boards if b in filters}

        added = []
        for board, groups in filters.items():
            matches = self.find_threads(board, groups)
            log.info("/%s/: %d matching thread(s)", board, len(matches))
            for thread_id in matches:
                url = thread_url(board, thread_id)
                if self.threads.add(url):
                    log.info("Adding: %s", url)
                    self.process_thread(url)
                    added.append(url)
                else:
                    log.debug("Already listed: %s", url)
        return added

    def monitor(self, interval: float = MONITOR_INTERVAL) -> None:
        """Run passes over the thread list until it is empty."""
        while True:
            start = time.monotonic()
            self.run_all()
            if not self.threads.all():
                log.info("No threads left to monitor.")
                return
            remaining = interval - (time.monotonic() - start)
            if remaining > 0:
                log.info("Next pass in %.0fs", remaining)
                time.sleep(remaining)

    def clean(self, skip: set[str] | None = None) -> None:
        """
        Check every directory under archive/ and move finished threads to
        completed/ (replaces clean_completed.py). URLs in skip were just
        checked and are not requested again.
        """
        skip = skip or set()
        if not self.archive_dir.is_dir():
            return
        for board_dir in sorted(p for p in self.archive_dir.iterdir() if p.is_dir()):
            for tdir in sorted(p for p in board_dir.iterdir() if p.is_dir()):
                if not tdir.name.isdigit():
                    continue
                board, thread_id = board_dir.name, int(tdir.name)
                url = thread_url(board, thread_id)
                if url in skip:
                    continue
                status = self.thread_status(board, thread_id)
                if status in (ThreadStatus.DEAD, ThreadStatus.ARCHIVED):
                    log.info("[/%s/%d] %s", board, thread_id, status.value)
                    self.move_to_completed(board, thread_id)
                    self.threads.remove(url)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def instance_lock(base_dir: Path, wait: bool) -> Iterator[bool]:
    """
    Only one process should talk to 4chan at a time, otherwise overlapping
    cron jobs multiply the request rate. Yields False if the lock is busy
    and wait is False.
    """
    path = base_dir / ".chan.lock"
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                yield False
                return
            log.info("Another instance is running; waiting for it to finish...")
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

MENU = """
========================================
         4chan Thread Archiver
========================================

1. Add thread + archive immediately
2. Run all listed threads once
3. Monitor all listed threads
4. Fetch new threads from catalogs
5. List threads
6. Remove thread from list
7. Move dead archives to completed/
8. Exit
"""


def _print_threads(urls: list[str]) -> None:
    print("\nListed threads\n--------------")
    if not urls:
        print("(none)")
    for i, url in enumerate(urls, 1):
        print(f"{i}. {url}")
    print()


def interactive(chan: FourChan) -> None:
    while True:
        print(MENU)
        try:
            choice = input("Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "1":
            raw = input("4chan thread URL: ").strip()
            if raw:
                try:
                    url = normalize_url(raw)
                except ValueError as exc:
                    print(f"ERROR: {exc}")
                    continue
                with instance_lock(chan.base_dir, wait=True):
                    chan.add(url)
        elif choice == "2":
            with instance_lock(chan.base_dir, wait=True):
                chan.run_all()
        elif choice == "3":
            with instance_lock(chan.base_dir, wait=True):
                try:
                    chan.monitor()
                except KeyboardInterrupt:
                    print("\nStopped monitoring.")
        elif choice == "4":
            with instance_lock(chan.base_dir, wait=True):
                chan.fetch()
        elif choice == "5":
            _print_threads(chan.threads.all())
        elif choice == "6":
            urls = chan.threads.all()
            _print_threads(urls)
            if not urls:
                continue
            value = input("Thread number or URL to remove: ").strip()
            url = None
            if value.isdigit() and 1 <= int(value) <= len(urls):
                url = urls[int(value) - 1]
            else:
                with contextlib.suppress(ValueError):
                    url = normalize_url(value)
            if url is None:
                print("Invalid selection.")
            elif input(f"Remove {url}? [y/N]: ").strip().lower() == "y":
                print("Removed." if chan.threads.remove(url) else "Not in list.")
        elif choice == "7":
            with instance_lock(chan.base_dir, wait=True):
                chan.clean()
        elif choice == "8":
            return
        else:
            print("Invalid choice.")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="4chan thread archiver")
    parser.add_argument("--dir", type=Path, default=BASE_DIR,
                        help="Base directory (threads.txt, archive/, completed/)")
    parser.add_argument("--thumbs", action="store_true", help="Also download thumbnails")
    parser.add_argument("--no-html", action="store_true",
                        help="Skip thread HTML/CSS/JS (JSON and images only)")
    parser.add_argument("--wait", action="store_true",
                        help="Wait if another instance is running instead of exiting")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="count", default=0,
                           help="Show progress (-v) or debug output (-vv). "
                                "By default only warnings and errors are shown.")
    verbosity.add_argument("-q", "--quiet", action="store_true",
                           help="Only show warnings and errors (default, "
                                "except in the interactive menu)")

    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("add", help="Add thread(s) and archive them once")
    p.add_argument("urls", nargs="+")
    sub.add_parser("run", help="Archive every listed thread once")
    p = sub.add_parser("fetch", help="Find and archive new threads using boards.json")
    p.add_argument("boards", nargs="*", help="Limit to these boards")
    p = sub.add_parser("monitor", help="Keep archiving until every thread is gone")
    p.add_argument("--interval", type=float, default=MONITOR_INTERVAL,
                   help="Seconds between passes (default: %(default)s)")
    sub.add_parser("list", help="List watched threads")
    p = sub.add_parser("remove", help="Stop watching thread(s)")
    p.add_argument("urls", nargs="+")
    sub.add_parser("clean", help="Move dead threads from archive/ to completed/")

    args = parser.parse_args(argv)

    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    elif args.command is None and not args.quiet:
        # The interactive menu would give no feedback at all if quiet.
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    chan = FourChan(args.dir, thumbs=args.thumbs, save_html=not args.no_html)

    if args.command is None:
        interactive(chan)
        return 0

    if args.command == "list":
        _print_threads(chan.threads.all())
        return 0

    if args.command == "remove":
        for url in args.urls:
            try:
                print(("Removed: " if chan.threads.remove(url) else "Not listed: ") + url)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
        return 0

    # Network commands. 'add' always waits its turn so the URL isn't lost;
    # scheduled commands exit if a previous run is still going.
    wait = args.wait or args.command == "add"
    with instance_lock(chan.base_dir, wait=wait) as acquired:
        if not acquired:
            log.info("Another instance is already running; exiting.")
            return 0
        if args.command == "add":
            for url in args.urls:
                try:
                    chan.add(url)
                except ValueError as exc:
                    log.error("%s", exc)
        elif args.command == "run":
            chan.run_all()
        elif args.command == "fetch":
            chan.fetch(args.boards or None)
        elif args.command == "monitor":
            chan.monitor(args.interval)
        elif args.command == "clean":
            chan.clean()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
