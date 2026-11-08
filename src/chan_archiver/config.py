"""Constants and defaults."""

from __future__ import annotations

import os
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (compatible; chan-archiver/2.0)"

# Minimum seconds between requests to each host. The 4chan API rules ask
# for no more than one request per second.
HOST_INTERVALS = {
    "a.4cdn.org": 1.0,  # API (thread JSON, catalogs)
    "boards.4chan.org": 1.0,  # Thread HTML
    "i.4cdn.org": 0.5,  # Images / thumbnails
    "s.4cdn.org": 0.5,  # CSS / JS
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

BASE_DIR_ENV = "CHAN_ARCHIVER_DIR"


def default_base_dir() -> Path:
    """Base directory: $CHAN_ARCHIVER_DIR if set, else the current directory."""
    return Path(os.environ.get(BASE_DIR_ENV) or Path.cwd())
