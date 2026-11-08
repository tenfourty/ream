"""The `{{name}}` mechanism (design 3.4): an unknown name raises; forms follow the settings."""

import pytest

from ream import news, onboarding, settings, templates, textvars


def test_forms_follow_the_pronoun():
    text = "{{Owner_they}} answer{{owner_s}}; ask {{owner_them}}; {{owner_name}}'s"
    assert textvars.render(text) == "He answers; ask him; Jeremy's"
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        assert textvars.render(text) == "She answers; ask her; Ann's"
    with settings.override(owner_name="the owner", owner_pronoun="they"):
        assert textvars.render(text) == "They answer; ask them; the owner's"


def test_an_unknown_name_raises():
    with pytest.raises(textvars.UnknownVariable):
        textvars.render("{{owner_nmae}}")


def test_extra_names_win_and_are_stringified():
    assert textvars.render("{{base}} {{n}}", base="b", n=3) == "b 3"


@pytest.mark.parametrize(
    "owner",
    [{}, {"owner_name": "Ann", "owner_pronoun": "she"}, {"owner_pronoun": "they"}],
)
def test_every_served_text_renders_under_any_owner(owner):
    with settings.override(**owner):
        onboarding.agents_txt("https://x.example")
        onboarding.skill_md("https://x.example")
        news.entries()
        for name in templates.names():
            assert "{{" not in templates.body(name)
        templates.shared_css()
        templates.index()


def _all_help(parser):
    import argparse

    out = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out.append(_all_help(sub))
    return "\n".join(out)


def test_cli_help_and_messages_follow_the_owner(monkeypatch, capsys):
    from ream import client, connect_cli, decide

    monkeypatch.setenv("COLUMNS", "80")
    for parser in (client.build_parser(), decide.build_parser()):
        ours = _all_help(parser)
        assert "{{" not in ours
        assert "Jeremy" in ours
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        for parser in (client.build_parser(), decide.build_parser()):
            text = _all_help(parser)
            assert "{{" not in text
            assert "Jeremy" not in text
            assert " he " not in text
        assert "ask Ann a lettered question" in decide.build_parser().format_help()
        assert "{{" not in _all_help(client.build_parser())
        assert decide._w("waiting on {{owner_them}}") == "waiting on her"
        assert "Ask Ann to approve" in textvars.words(
            "Ask {{owner_name}} to approve this machine."
        )
    with settings.override(owner_name="Sam", owner_pronoun="they"):
        text = _all_help(decide.build_parser())
        assert "Jeremy" not in text and " they answers " not in text
        assert "block until they answer," in " ".join(text.split())
    # connect_cli's --help
    with settings.override(owner_name="Ann", owner_pronoun="she"):
        with pytest.raises(SystemExit):
            connect_cli.main(["--help"])
        shown = " ".join(capsys.readouterr().out.split())
        assert "Ann's artifacts space; she approves a code" in shown
