"""The watched-thread list (threads.txt)."""

from __future__ import annotations

import contextlib
import fcntl
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from .urls import normalize_url

log = logging.getLogger(__name__)


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
