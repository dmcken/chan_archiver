"""Offline tests for chan.py (no network access)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import chan

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.4chan.org/w/thread/123456", ("w", 123456)),
        ("http://boards.4chan.org/wg/thread/42/some-semantic-slug", ("wg", 42)),
        ("https://boards.4channel.org/g/thread/7#p8", ("g", 7)),
        ("https://4chan.org/W/res/99.html", ("w", 99)),
        ("  https://boards.4chan.org/w/thread/1  ", ("w", 1)),
    ],
)
def test_parse_thread_url(url, expected):
    assert chan.parse_thread_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "https://boards.4chan.org/w/", "https://example.com/w/thread/1"],
)
def test_parse_thread_url_invalid(url):
    with pytest.raises(ValueError):
        chan.parse_thread_url(url)


def test_normalize_url():
    assert (
        chan.normalize_url("http://boards.4channel.org/WG/thread/5/slug#p6")
        == "https://boards.4chan.org/wg/thread/5"
    )


# ---------------------------------------------------------------------------
# Retry-After / throttling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("5", 5.0), ("-3", 0.0), ("garbage", None)],
)
def test_parse_retry_after(value, expected):
    assert chan._parse_retry_after(value) == expected


def test_parse_retry_after_http_date_in_past():
    assert chan._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_throttle_penalize_and_reward():
    t = chan.HostThrottle(1.0)
    wait = t.penalize(None)
    assert t.interval == 2.0
    assert wait == max(chan.DEFAULT_BACKOFF, 2.0)

    assert t.penalize(60.0) == 60.0
    assert t.interval == 4.0

    for _ in range(20):
        t.penalize(0)
    assert t.interval == chan.MAX_INTERVAL

    for _ in range(200):
        t.reward()
    assert t.interval == t.base_interval


# ---------------------------------------------------------------------------
# ThreadList
# ---------------------------------------------------------------------------


def test_thread_list_add_remove(tmp_path):
    tl = chan.ThreadList(tmp_path / "threads.txt")
    assert tl.all() == []

    assert tl.add("https://boards.4chan.org/w/thread/1/slug") is True
    assert tl.add("http://boards.4channel.org/w/thread/1") is False  # duplicate
    assert tl.add("https://boards.4chan.org/wg/thread/2") is True
    assert tl.all() == [
        "https://boards.4chan.org/w/thread/1",
        "https://boards.4chan.org/wg/thread/2",
    ]

    assert tl.remove("https://boards.4chan.org/w/thread/1") is True
    assert tl.remove("https://boards.4chan.org/w/thread/1") is False
    assert tl.all() == ["https://boards.4chan.org/wg/thread/2"]

    with pytest.raises(ValueError):
        tl.add("nonsense")


def test_thread_list_skips_comments_and_invalid_lines(tmp_path):
    path = tmp_path / "threads.txt"
    path.write_text(
        "# comment\n\nhttps://boards.4chan.org/w/thread/1\nbogus line\n",
        encoding="utf-8",
    )
    assert chan.ThreadList(path).all() == ["https://boards.4chan.org/w/thread/1"]


def test_thread_list_merges_legacy_files(tmp_path):
    path = tmp_path / "threads.txt"
    path.write_text("https://boards.4chan.org/w/thread/1\n", encoding="utf-8")
    (tmp_path / "toadd.txt").write_text(
        "https://boards.4chan.org/w/thread/1\nhttps://boards.4chan.org/w/thread/2\n",
        encoding="utf-8",
    )

    assert chan.ThreadList(path).all() == [
        "https://boards.4chan.org/w/thread/1",
        "https://boards.4chan.org/w/thread/2",
    ]
    assert not (tmp_path / "toadd.txt").exists()
    assert path.read_text(encoding="utf-8").splitlines() == [
        "https://boards.4chan.org/w/thread/1",
        "https://boards.4chan.org/w/thread/2",
    ]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_example_boards_json_is_valid():
    data = json.loads((REPO_ROOT / "boards.json").read_text(encoding="utf-8"))
    for board, groups in data.items():
        if board.startswith("_"):
            continue
        assert isinstance(groups, list) and groups, board
        for group in groups:
            assert isinstance(group, list) and group, (board, group)
            for pattern in group:
                re.compile(pattern)


def test_load_filters_drops_comment_keys(tmp_path):
    (tmp_path / "boards.json").write_text(
        json.dumps({"_comment": "x", "w": [["rain"]]}), encoding="utf-8"
    )
    assert chan.FourChan(tmp_path).load_filters() == {"w": [["rain"]]}


def test_load_filters_missing_file(tmp_path):
    assert chan.FourChan(tmp_path).load_filters() == {}


def test_find_threads(tmp_path, monkeypatch):
    threads = [
        {"no": 1, "sub": "Black and White", "com": ""},
        {"no": 2, "sub": "Black", "com": ""},  # only half of a group
        {"no": 3, "sub": "", "com": "a <b>white</b> and black thread"},
        {"no": 4, "sub": "Rainy day"},
        {"no": 5},
    ]
    fc = chan.FourChan(tmp_path)
    monkeypatch.setattr(fc, "get_threads", lambda board: threads)
    assert fc.find_threads("w", [["black", "white"], ["rain"]]) == [1, 3, 4]


# ---------------------------------------------------------------------------
# Base directory / CLI
# ---------------------------------------------------------------------------


def test_default_base_dir_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAN_ARCHIVER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert chan.default_base_dir() == tmp_path
    assert chan.FourChan().base_dir == tmp_path


def test_default_base_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAN_ARCHIVER_DIR", str(tmp_path))
    assert chan.default_base_dir() == tmp_path


def test_instance_lock(tmp_path):
    with chan.instance_lock(tmp_path, wait=False) as first:
        assert first is True
        # flock locks are per open file description, so a second open in the
        # same process contends just like another process would.
        with chan.instance_lock(tmp_path, wait=False) as second:
            assert second is False


def test_cli_list_and_remove(tmp_path, capsys):
    (tmp_path / "threads.txt").write_text(
        "https://boards.4chan.org/w/thread/1\n", encoding="utf-8"
    )
    assert chan.main(["--dir", str(tmp_path), "list"]) == 0
    assert "https://boards.4chan.org/w/thread/1" in capsys.readouterr().out

    assert chan.main(["--dir", str(tmp_path), "remove", "https://boards.4chan.org/w/thread/1"]) == 0
    assert "Removed:" in capsys.readouterr().out
    assert chan.ThreadList(tmp_path / "threads.txt").all() == []


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        chan.main(["--help"])
    assert exc.value.code == 0
    assert "4chan thread archiver" in capsys.readouterr().out
