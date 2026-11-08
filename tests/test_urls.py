import pytest

from chan_archiver.urls import normalize_url, parse_thread_url, thread_url


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
    assert parse_thread_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "https://boards.4chan.org/w/", "https://example.com/w/thread/1"],
)
def test_parse_thread_url_invalid(url):
    with pytest.raises(ValueError):
        parse_thread_url(url)


def test_thread_url():
    assert thread_url("w", 5) == "https://boards.4chan.org/w/thread/5"


def test_normalize_url():
    assert (
        normalize_url("http://boards.4channel.org/WG/thread/5/slug#p6")
        == "https://boards.4chan.org/wg/thread/5"
    )
