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

From Python:

    from chan_archiver import Archiver, FourChanAPI

    threads = FourChanAPI().get_threads("b")
    Archiver("/srv/4chan").add("https://boards.4chan.org/b/thread/123")
"""

from importlib.metadata import PackageNotFoundError, version

from .api import FourChanAPI, ThreadStatus
from .archiver import Archiver, ArchiveResult
from .threadlist import ThreadList
from .urls import normalize_url, parse_thread_url, thread_url

try:
    __version__ = version("chan-archiver")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0+unknown"

__all__ = [
    "ArchiveResult",
    "Archiver",
    "FourChanAPI",
    "ThreadList",
    "ThreadStatus",
    "__version__",
    "normalize_url",
    "parse_thread_url",
    "thread_url",
]
