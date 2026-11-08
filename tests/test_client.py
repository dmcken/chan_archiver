import pytest
from conftest import FakeResponse, md5_b64

from chan_archiver.client import HostThrottle, parse_retry_after
from chan_archiver.config import DEFAULT_BACKOFF, MAX_ATTEMPTS, MAX_INTERVAL

URL = "https://i.4cdn.org/w/1.jpg"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("5", 5.0), ("-3", 0.0), ("garbage", None)],
)
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date_in_past():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_throttle_penalize_and_reward():
    t = HostThrottle(1.0)
    assert t.penalize(None) == max(DEFAULT_BACKOFF, 2.0)
    assert t.interval == 2.0

    assert t.penalize(60.0) == 60.0
    assert t.interval == 4.0

    for _ in range(20):
        t.penalize(0)
    assert t.interval == MAX_INTERVAL

    for _ in range(200):
        t.reward()
    assert t.interval == t.base_interval


def test_request_retries_429_then_succeeds(http, session):
    session.routes[URL] = [
        FakeResponse(429, headers={"Retry-After": "3"}),
        FakeResponse(503),
        FakeResponse(200, b"ok"),
    ]
    resp = http.request(URL)
    assert resp.status_code == 200
    assert len(session.calls) == 3


def test_request_gives_up(http, session):
    session.routes[URL] = FakeResponse(429)
    assert http.request(URL) is None
    assert len(session.calls) == MAX_ATTEMPTS


def test_request_returns_404_without_retry(http, session):
    assert http.request(URL).status_code == 404
    assert len(session.calls) == 1


def test_get_json(http, session):
    session.routes[URL] = FakeResponse(200, {"a": 1})
    assert http.get_json(URL) == {"a": 1}
    session.routes[URL] = FakeResponse(200, b"not json")
    assert http.get_json(URL) is None


def test_download_file_verifies_size_and_md5(http, session, tmp_path):
    data = b"x" * 5000
    session.routes[URL] = FakeResponse(200, data)
    dest = tmp_path / "images" / "1.jpg"

    assert http.download_file(URL, dest, expected_size=5000, expected_md5=md5_b64(data))
    assert dest.read_bytes() == data
    assert not dest.with_name("1.jpg.part").exists()


@pytest.mark.parametrize(
    "kwargs",
    [{"expected_size": 1}, {"expected_md5": md5_b64(b"something else")}],
)
def test_download_file_discards_mismatch(http, session, tmp_path, kwargs):
    session.routes[URL] = FakeResponse(200, b"Too Many Requests")
    dest = tmp_path / "1.jpg"
    assert not http.download_file(URL, dest, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_download_file_http_error(http, tmp_path):
    assert not http.download_file(URL, tmp_path / "1.jpg")
    assert list(tmp_path.iterdir()) == []
