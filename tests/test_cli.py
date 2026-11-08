import pytest

from chan_archiver import Archiver, ThreadList, __version__
from chan_archiver.cli import main
from chan_archiver.config import default_base_dir
from chan_archiver.lock import instance_lock


def test_default_base_dir_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAN_ARCHIVER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_base_dir() == tmp_path
    assert Archiver().base_dir == tmp_path


def test_default_base_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAN_ARCHIVER_DIR", str(tmp_path))
    assert default_base_dir() == tmp_path


def test_instance_lock(tmp_path):
    with instance_lock(tmp_path, wait=False) as first:
        assert first is True
        # flock locks are per open file description, so a second open in the
        # same process contends just like another process would.
        with instance_lock(tmp_path, wait=False) as second:
            assert second is False


def test_list_and_remove(tmp_path, capsys):
    url = "https://boards.4chan.org/w/thread/1"
    (tmp_path / "threads.txt").write_text(f"{url}\n", encoding="utf-8")

    assert main(["--dir", str(tmp_path), "list"]) == 0
    assert url in capsys.readouterr().out

    assert main(["--dir", str(tmp_path), "remove", url, "junk"]) == 0
    captured = capsys.readouterr()
    assert "Removed:" in captured.out
    assert "ERROR" in captured.err
    assert ThreadList(tmp_path / "threads.txt").all() == []


def test_network_command_skipped_when_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(Archiver, "run_all", lambda self: pytest.fail("should not run"))
    with instance_lock(tmp_path, wait=False):
        assert main(["--dir", str(tmp_path), "run"]) == 0


def test_help_and_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "4chan thread archiver" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
