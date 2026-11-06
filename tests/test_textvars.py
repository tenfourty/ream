"""The `{{name}}` mechanism (design 3.4): an unknown name raises; forms follow the settings."""

import pytest

from artifacts import news, onboarding, settings, templates, textvars


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
