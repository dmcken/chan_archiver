"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .archiver import Archiver
from .config import BASE_DIR_ENV, MONITOR_INTERVAL
from .lock import instance_lock
from .menu import interactive, print_threads

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chan-archiver", description="4chan thread archiver")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Base directory (threads.txt, boards.json, archive/, completed/). "
        f"Default: ${BASE_DIR_ENV} or the current directory",
    )
    parser.add_argument("--thumbs", action="store_true", help="Also download thumbnails")
    parser.add_argument(
        "--no-html", action="store_true", help="Skip thread HTML/CSS/JS (JSON and images only)"
    )
    parser.add_argument(
        "--wait", action="store_true", help="Wait if another instance is running instead of exiting"
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show progress (-v) or debug output (-vv). "
        "By default only warnings and errors are shown.",
    )
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only show warnings and errors (default, except in the interactive menu)",
    )

    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("add", help="Add thread(s) and archive them once")
    p.add_argument("urls", nargs="+")
    sub.add_parser("run", help="Archive every listed thread once")
    p = sub.add_parser("fetch", help="Find and archive new threads using boards.json")
    p.add_argument("boards", nargs="*", help="Limit to these boards")
    p = sub.add_parser("monitor", help="Keep archiving until every thread is gone")
    p.add_argument(
        "--interval",
        type=float,
        default=MONITOR_INTERVAL,
        help="Seconds between passes (default: %(default)s)",
    )
    sub.add_parser("list", help="List watched threads")
    p = sub.add_parser("remove", help="Stop watching thread(s)")
    p.add_argument("urls", nargs="+")
    sub.add_parser("clean", help="Move dead threads from archive/ to completed/")
    return parser


def _setup_logging(args: argparse.Namespace) -> None:
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args)

    archiver = Archiver(args.dir, thumbs=args.thumbs, save_html=not args.no_html)

    if args.command is None:
        interactive(archiver)
        return 0

    if args.command == "list":
        print_threads(archiver.threads.all())
        return 0

    if args.command == "remove":
        for url in args.urls:
            try:
                print(("Removed: " if archiver.threads.remove(url) else "Not listed: ") + url)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
        return 0

    # Network commands. 'add' always waits its turn so the URL isn't lost;
    # scheduled commands exit if a previous run is still going.
    wait = args.wait or args.command == "add"
    with instance_lock(archiver.base_dir, wait=wait) as acquired:
        if not acquired:
            log.info("Another instance is already running; exiting.")
            return 0
        if args.command == "add":
            for url in args.urls:
                try:
                    archiver.add(url)
                except ValueError as exc:
                    log.error("%s", exc)
        elif args.command == "run":
            archiver.run_all()
        elif args.command == "fetch":
            archiver.fetch(args.boards or None)
        elif args.command == "monitor":
            archiver.monitor(args.interval)
        elif args.command == "clean":
            archiver.clean()
    return 0


def run() -> None:
    """Console-script entry point."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
