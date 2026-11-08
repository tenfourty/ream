"""The `ream` dispatcher finds its verb as the first positional word."""

import pytest

from ream import cli


@pytest.mark.parametrize(
    ("args", "index"),
    [
        (["publish", "p.html"], 0),
        (["publish", "p.html", "--title", "list"], 0),
        (["--key-file", "list", "ask", "ART"], 2),
        (["--session=a/b", "list"], 1),
        (["--title", "list", "publish", "p.html"], None),
        (["nonsense", "list"], None),
        ([], None),
    ],
)
def test_the_verb_is_the_first_positional_word(args, index):
    assert cli._find_verb(args) == index


def test_a_stray_flag_before_the_verb_is_refused_not_misrouted(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--title", "list", "publish", "p.html"])
    assert stop.value.code == 2
    assert "no such verb" in capsys.readouterr().err
