"""Thread archiving: archive/ and completed/ directory management."""

from __future__ import annotations

import contextlib
import email.utils
import json
import logging
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import page
from .api import FourChanAPI, ThreadStatus
from .client import HttpClient
from .config import IMAGE_HOST, MONITOR_INTERVAL, SITE_DIR_NAME, STATIC_HOST, default_base_dir
from .filters import Filters, load_filters
from .threadlist import ThreadList
from .urls import normalize_url, parse_thread_url, thread_url

log = logging.getLogger(__name__)

FINISHED = (ThreadStatus.DEAD, ThreadStatus.ARCHIVED)


@dataclass
class ArchiveResult:
    status: ThreadStatus
    new_files: int = 0
    failed_files: int = 0
    errors: list[str] = field(default_factory=list)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class Archiver:
    """
    Archives watched threads under a base directory:

        threads.txt                          Watched thread URLs
        boards.json                          Catalog filters for fetch()
        archive/4chan/<board>/<id>/          Threads being watched
        completed/4chan/<board>/<slug>-<id>/ Threads that have died

    Example:

        archiver = Archiver("/srv/4chan", thumbs=True)
        archiver.add("https://boards.4chan.org/w/thread/123456")
        archiver.run_all()
    """

    def __init__(
        self,
        base_dir: Path | str | None = None,
        *,
        thumbs: bool = False,
        save_html: bool = True,
        host_intervals: dict[str, float] | None = None,
        api: FourChanAPI | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else default_base_dir()
        self.archive_dir = self.base_dir / "archive" / SITE_DIR_NAME
        self.completed_dir = self.base_dir / "completed" / SITE_DIR_NAME
        self.threads = ThreadList(self.base_dir / "threads.txt")
        self.filters_file = self.base_dir / "boards.json"

        self.thumbs = thumbs
        self.save_html = save_html
        self.api = api or FourChanAPI(HttpClient(host_intervals))

    @property
    def http(self) -> HttpClient:
        return self.api.http

    def load_filters(self) -> Filters:
        return load_filters(self.filters_file)

    # -- Archiving a single thread -----------------------------------------

    def thread_dir(self, board: str, thread_id: int) -> Path:
        return self.archive_dir / board / str(thread_id)

    def _fetch_thread_json(
        self, board: str, thread_id: int, json_path: Path
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
        resp = self.http.request(self.api.thread_json_url(board, thread_id), headers=headers)
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
        except (ValueError, KeyError, IndexError, TypeError):
            log.warning("%s: invalid thread JSON", resp.url)
            return ThreadStatus.ERROR, None, False
        return None, data, True

    def _save_html(self, board: str, thread_id: int, thread_dir: Path) -> None:
        resp = self.http.request(thread_url(board, thread_id))
        if resp is None or resp.status_code != 200:
            log.warning("[/%s/%d] Could not fetch thread HTML", board, thread_id)
            return
        text = resp.text

        css_files, js_files = page.static_assets(text)
        for kind, names in (("css", css_files), ("js", js_files)):
            for name in names:
                dest = thread_dir / kind / name
                if not dest.exists():
                    self.http.download_file(f"{STATIC_HOST}/{kind}/{name}", dest)

        _write_atomic(thread_dir / f"{thread_id}.html", page.rewrite_html(text))

    def _download_media(
        self, board: str, posts: list[dict], thread_dir: Path, result: ArchiveResult
    ) -> None:
        for post in posts:
            if "tim" not in post or post.get("filedeleted"):
                continue
            tim, ext = post["tim"], post["ext"]

            image = thread_dir / "images" / f"{tim}{ext}"
            size = post.get("fsize")
            # A file of the wrong size is a partial or error-page download.
            if not image.exists() or (size is not None and image.stat().st_size != size):
                if self.http.download_file(
                    f"{IMAGE_HOST}/{board}/{tim}{ext}",
                    image,
                    expected_size=size,
                    expected_md5=post.get("md5"),
                ):
                    result.new_files += 1
                    log.info("    image %s", image.name)
                else:
                    result.failed_files += 1

            if self.thumbs:
                thumb = thread_dir / "thumbs" / f"{tim}s.jpg"
                if not thumb.exists() or thumb.stat().st_size == 0:
                    if self.http.download_file(f"{IMAGE_HOST}/{board}/{tim}s.jpg", thumb):
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
            _write_atomic(
                json_path, json.dumps(data, sort_keys=True, indent=2, separators=(",", ": "))
            )
            _write_atomic(
                thread_dir / "external_links.txt",
                "".join(f"{u}\n" for u in page.external_links(posts)),
            )
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
            log.error(
                "[/%s/%d] %s already exists; leaving archive in place", board, thread_id, dest
            )
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

        if result.status in FINISHED:
            if self.thread_dir(board, thread_id).exists():
                self.move_to_completed(board, thread_id)
            self.threads.remove(url)
        return result

    def add(self, url: str) -> ArchiveResult:
        """Watch a thread and archive it once."""
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
            boards = list(boards)
            for b in boards:
                if b not in filters:
                    log.warning("No filters for /%s/ in %s", b, self.filters_file.name)
            filters = {b: filters[b] for b in boards if b in filters}

        added = []
        for board, groups in filters.items():
            matches = self.api.find_threads(board, groups)
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
        completed/. URLs in skip were just checked and are not requested
        again.
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
                status = self.api.thread_status(board, thread_id)
                if status in FINISHED:
                    log.info("[/%s/%d] %s", board, thread_id, status.value)
                    self.move_to_completed(board, thread_id)
                    self.threads.remove(url)
