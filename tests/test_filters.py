import json
import re

from conftest import REPO_ROOT

from chan_archiver.filters import load_filters, match_threads


def test_example_boards_json_is_valid():
    filters = load_filters(REPO_ROOT / "boards.json")
    assert filters
    for board, groups in filters.items():
        assert isinstance(groups, list) and groups, board
        for group in groups:
            assert isinstance(group, list) and group, (board, group)
            for pattern in group:
                re.compile(pattern)


def test_load_filters_drops_comment_keys(tmp_path):
    path = tmp_path / "boards.json"
    path.write_text(json.dumps({"_comment": "x", "w": [["rain"]]}), encoding="utf-8")
    assert load_filters(path) == {"w": [["rain"]]}


def test_load_filters_missing_file(tmp_path):
    assert load_filters(tmp_path / "boards.json") == {}


def test_match_threads():
    threads = [
        {"no": 1, "sub": "Black and White", "com": ""},
        {"no": 2, "sub": "Black", "com": ""},  # only half of a group
        {"no": 3, "sub": "", "com": "a <b>white</b> and black thread"},
        {"no": 4, "sub": "Rainy day"},
        {"no": 5},
    ]
    assert match_threads(threads, [["black", "white"], ["rain"]]) == [1, 3, 4]
