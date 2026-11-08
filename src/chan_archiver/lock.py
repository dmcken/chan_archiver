"""Single-instance lock."""

from __future__ import annotations

import contextlib
import fcntl
import logging
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_NAME = ".chan.lock"


@contextlib.contextmanager
def instance_lock(base_dir: Path, wait: bool) -> Iterator[bool]:
    """
    Only one process should talk to 4chan at a time, otherwise overlapping
    cron jobs multiply the request rate. Yields False if the lock is busy
    and wait is False.
    """
    with open(base_dir / LOCK_NAME, "w") as f:
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
