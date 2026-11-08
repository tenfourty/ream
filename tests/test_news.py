"""The tools say what is new, once per machine (a seat's request, 24 Sep 2026)."""

import io

from ream import news, plugin_cli


def _run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("REAM_NEWS", raising=False)
    out = io.StringIO()
    news.announce(out)
    return out.getvalue()


def test_a_new_machine_sees_only_the_newest_then_nothing(tmp_path, monkeypatch):
    first = _run(tmp_path, monkeypatch)
    # 8 Nov 2026 (rename, step 2): the line the tools print said "new in the artifacts tools:";
    # it now says "new in Ream:". Only that prefix changed.
    assert first.count("new in Ream:") == news.FIRST_RUN
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


def _state(tmp_path):
    return tmp_path / "artifacts" / "news-seen"


def test_plugin_news_is_shown_and_never_moves_the_cores_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        news, "NEWS", (("2020-01-02", "core two"), ("2020-01-01", "core one"))
    )
    plugin_cli.reset()
    try:
        plugin_cli.register_client_defaults(
            "toy", news=[("2020-01-05", "plug-in five")]
        )
        _state(tmp_path).parent.mkdir(parents=True)
        _state(tmp_path).write_text("2020-01-01\n")  # an existing, old-format file
        out = _run(tmp_path, monkeypatch)
        assert "core two" in out and "plug-in five" in out and "core one" not in out
        assert _state(tmp_path).read_text().splitlines()[0] == "2020-01-02"
        assert _run(tmp_path, monkeypatch) == ""
        # a core entry later than the plug-in's is still shown
        monkeypatch.setattr(
            news, "NEWS", (("2020-01-03", "core three"), ("2020-01-02", "core two"))
        )
        assert "core three" in _run(tmp_path, monkeypatch)
    finally:
        plugin_cli.reset()


def test_a_plugin_entry_alone_leaves_the_cores_marker_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "NEWS", (("2020-01-02", "core two"),))
    plugin_cli.reset()
    try:
        _state(tmp_path).parent.mkdir(parents=True)
        _state(tmp_path).write_text("2020-01-02\n")
        plugin_cli.register_client_defaults(
            "toy", news=[("2020-01-05", "plug-in five")]
        )
        out = _run(tmp_path, monkeypatch)
        assert "plug-in five" in out and "core two" not in out
        assert _state(tmp_path).read_text().splitlines()[0] == "2020-01-02"
    finally:
        plugin_cli.reset()


def test_announce_strips_control_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "NEWS", (("2020-01-02", "core \x1b[31mred\rtwo"),))
    out = _run(tmp_path, monkeypatch)
    assert "\x1b" not in out and "\r" not in out and "red" in out
