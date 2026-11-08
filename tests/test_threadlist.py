import pytest

from chan_archiver.threadlist import ThreadList


def test_add_remove(tmp_path):
    tl = ThreadList(tmp_path / "threads.txt")
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


def test_skips_comments_and_invalid_lines(tmp_path):
    path = tmp_path / "threads.txt"
    path.write_text(
        "# comment\n\nhttps://boards.4chan.org/w/thread/1\nbogus line\n",
        encoding="utf-8",
    )
    assert ThreadList(path).all() == ["https://boards.4chan.org/w/thread/1"]


def test_merges_legacy_files(tmp_path):
    path = tmp_path / "threads.txt"
    path.write_text("https://boards.4chan.org/w/thread/1\n", encoding="utf-8")
    (tmp_path / "toadd.txt").write_text(
        "https://boards.4chan.org/w/thread/1\nhttps://boards.4chan.org/w/thread/2\n",
        encoding="utf-8",
    )
    expected = [
        "https://boards.4chan.org/w/thread/1",
        "https://boards.4chan.org/w/thread/2",
    ]

    assert ThreadList(path).all() == expected
    assert not (tmp_path / "toadd.txt").exists()
    assert path.read_text(encoding="utf-8").splitlines() == expected
