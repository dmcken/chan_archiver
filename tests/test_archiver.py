import json

from conftest import FakeResponse, md5_b64

from chan_archiver import ThreadStatus

API = "https://a.4cdn.org"
URL = "https://boards.4chan.org/w/thread/100"
JSON_URL = f"{API}/w/thread/100.json"
IMAGE = b"\x89PNG fake image"
HTML = '<link href="//s.4cdn.org/css/a.css"><img src="//i.4cdn.org/w/555.png">'


def thread_json(archived=False):
    op = {
        "no": 100,
        "sub": "Rain",
        "com": "pics from https://example.com/rain",
        "semantic_url": "rain-thread",
        "tim": 555,
        "ext": ".png",
        "fsize": len(IMAGE),
        "md5": md5_b64(IMAGE),
    }
    if archived:
        op["archived"] = 1
    return {"posts": [op, {"no": 101, "com": "nice"}]}


def serve_thread(session, archived=False):
    session.routes[JSON_URL] = FakeResponse(200, thread_json(archived))
    session.routes[URL] = FakeResponse(200, HTML)
    session.routes["https://s.4cdn.org/css/a.css"] = FakeResponse(200, "body{}")
    session.routes["https://i.4cdn.org/w/555.png"] = FakeResponse(200, IMAGE)


def test_archive_thread(archiver, session):
    serve_thread(session)
    result = archiver.archive_thread("w", 100)

    assert result.status is ThreadStatus.ALIVE
    assert (result.new_files, result.failed_files) == (1, 0)

    tdir = archiver.thread_dir("w", 100)
    assert json.loads((tdir / "100.json").read_text()) == thread_json()
    assert (tdir / "images" / "555.png").read_bytes() == IMAGE
    assert (tdir / "css" / "a.css").read_text() == "body{}"
    assert (tdir / "100.html").read_text() == '<link href="css/a.css"><img src="images/555.png">'
    assert (tdir / "external_links.txt").read_text() == "https://example.com/rain\n"


def test_archive_thread_not_modified(archiver, session):
    serve_thread(session)
    archiver.archive_thread("w", 100)
    (archiver.thread_dir("w", 100) / "images" / "555.png").unlink()

    session.calls.clear()
    session.routes[JSON_URL] = FakeResponse(304)
    result = archiver.archive_thread("w", 100)

    assert result.status is ThreadStatus.ALIVE
    assert result.new_files == 1  # missing image re-fetched
    assert "If-Modified-Since" in session.calls[0][1]["headers"]
    assert URL not in session.urls()  # HTML not re-fetched


def test_archive_thread_dead(archiver):
    assert archiver.archive_thread("w", 100).status is ThreadStatus.DEAD
    assert not archiver.thread_dir("w", 100).exists()


def test_add_archived_thread_moves_to_completed(archiver, session):
    serve_thread(session, archived=True)
    result = archiver.add(URL)

    assert result.status is ThreadStatus.ARCHIVED
    assert archiver.threads.all() == []
    assert not archiver.thread_dir("w", 100).exists()
    done = archiver.completed_dir / "w" / "rain-thread-100"
    assert (done / "images" / "555.png").read_bytes() == IMAGE


def test_run_all_retires_dead_threads(archiver, session):
    serve_thread(session)
    archiver.add(URL)
    assert archiver.threads.all() == [URL]

    session.routes[JSON_URL] = FakeResponse(404)
    archiver.run_all()

    assert archiver.threads.all() == []
    assert (archiver.completed_dir / "w" / "rain-thread-100" / "100.json").exists()


def test_clean_moves_unlisted_dead_archives(archiver, session):
    serve_thread(session)
    archiver.archive_thread("w", 100)  # archived but never listed
    session.routes[JSON_URL] = FakeResponse(404)

    archiver.clean()
    assert (archiver.completed_dir / "w" / "rain-thread-100").is_dir()


def test_fetch(archiver, session):
    (archiver.base_dir / "boards.json").write_text(json.dumps({"w": [["rain"]], "wg": [["x"]]}))
    session.routes[f"{API}/w/catalog.json"] = FakeResponse(
        200, [{"threads": [{"no": 100, "sub": "Rain"}, {"no": 200, "sub": "Snow"}]}]
    )
    serve_thread(session)

    assert archiver.fetch(["w"]) == [URL]
    assert archiver.threads.all() == [URL]
    assert f"{API}/wg/catalog.json" not in session.urls()
    assert archiver.fetch(["w"]) == []  # already listed
