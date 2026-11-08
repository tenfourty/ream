"""The tools say what is new, once per machine (a seat's request, 24 Sep 2026)."""

import io

from ream import news


def _run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("REAM_NEWS", raising=False)
    out = io.StringIO()
    news.announce(out)
    return out.getvalue()


def test_a_new_machine_sees_only_the_newest_then_nothing(tmp_path, monkeypatch):
    first = _run(tmp_path, monkeypatch)
    assert first.count("new in the artifacts tools:") == news.FIRST_RUN
    assert news.entries()[0][1] in first
    assert _run(tmp_path, monkeypatch) == "", "said once, not on every call"


def test_a_machine_that_saw_an_older_note_sees_everything_since(tmp_path, monkeypatch):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "news-seen").write_text("2026-09-01\n")
    assert _run(tmp_path, monkeypatch).count("new in") == len(news.NEWS)


def test_it_can_be_silenced_and_never_breaks_a_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "file"))
    (tmp_path / "file").write_text("not a directory")
    out = io.StringIO()
    news.announce(out)  # an unwritable state dir must not raise
    monkeypatch.setenv("REAM_NEWS", "0")
    silent = io.StringIO()
    news.announce(silent)
    assert silent.getvalue() == ""


def test_the_entries_are_newest_first():
    tags = [tag for tag, _ in news.NEWS]
    assert tags == sorted(tags, reverse=True)
