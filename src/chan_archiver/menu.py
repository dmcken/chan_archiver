"""Interactive menu (run with no subcommand)."""

from __future__ import annotations

import contextlib

from .archiver import Archiver
from .lock import instance_lock
from .urls import normalize_url

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


def print_threads(urls: list[str]) -> None:
    print("\nListed threads\n--------------")
    if not urls:
        print("(none)")
    for i, url in enumerate(urls, 1):
        print(f"{i}. {url}")
    print()


def _add(archiver: Archiver) -> None:
    raw = input("4chan thread URL: ").strip()
    if not raw:
        return
    try:
        url = normalize_url(raw)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return
    with instance_lock(archiver.base_dir, wait=True):
        archiver.add(url)


def _monitor(archiver: Archiver) -> None:
    with instance_lock(archiver.base_dir, wait=True):
        try:
            archiver.monitor()
        except KeyboardInterrupt:
            print("\nStopped monitoring.")


def _remove(archiver: Archiver) -> None:
    urls = archiver.threads.all()
    print_threads(urls)
    if not urls:
        return
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
        print("Removed." if archiver.threads.remove(url) else "Not in list.")


def _locked(method_name: str):
    def action(archiver: Archiver) -> None:
        with instance_lock(archiver.base_dir, wait=True):
            getattr(archiver, method_name)()

    return action


ACTIONS = {
    "1": _add,
    "2": _locked("run_all"),
    "3": _monitor,
    "4": _locked("fetch"),
    "5": lambda archiver: print_threads(archiver.threads.all()),
    "6": _remove,
    "7": _locked("clean"),
}


def interactive(archiver: Archiver) -> None:
    while True:
        print(MENU)
        try:
            choice = input("Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "8":
            return
        action = ACTIONS.get(choice)
        if action is None:
            print("Invalid choice.")
        else:
            action(archiver)
