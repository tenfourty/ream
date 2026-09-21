"""Ways a page is wrong that the serving policy has no opinion about.

homelab-ij8.36. An agent on 19 Sep wrote its own greps before publishing --
title, forms, external refs, target=_blank on the gallery link -- and said
every agent would rediscover them. "A check that runs beats a warning that is
read." The CSP half already refused; these are the ones no directive covers.

Every case here is a page that RENDERS PERFECTLY and is still wrong, which is
the whole category: the author cannot look at its own page (homelab-ij8.35),
so nothing else catches it.
"""

import pathlib

import pytest

from artifacts import traps

TEMPLATES = pathlib.Path(traps.__file__).parent / "templates"


def page(head: str = "", body: str = "<p>Words.</p>") -> str:
    return (
        "<!doctype html>\n<html><head>\n"
        f"{head or '<title>The backups</title>'}\n"
        f"</head><body>\n{body}\n</body></html>\n"
    )


# --- a placeholder nobody can see ---------------------------------------------


@pytest.mark.parametrize(
    "title", ["PAGE TITLE", "page title", "Untitled", "Document", "TODO", "Replace me"]
)
def test_a_placeholder_title_is_refused(title):
    [found] = traps.check(page(head=f"<title>{title}</title>"))
    assert found.shape == "placeholder_title"
    assert found.line == 3


def test_a_placeholder_h1_is_caught_too():
    """The first board shipped `PAGE TITLE` in <title>; the same skeleton puts
    it in the <h1>, where it is the first thing on the page."""
    found = traps.check(page(body="<h1>PAGE TITLE</h1><p>Words.</p>"))
    assert [w.shape for w in found] == ["placeholder_title"]


def test_a_real_title_says_nothing():
    assert traps.check(page()) == []


def test_a_title_that_merely_contains_the_word_is_not_a_placeholder():
    """"Untitled" is a placeholder; "Untitled drafts and what to do with them"
    is a page about them. A check that fires on a correct page is how a
    warning becomes noise."""
    assert traps.check(page(head="<title>Untitled drafts, and what to do</title>")) == []


def test_markup_inside_the_title_does_not_hide_a_placeholder():
    assert [w.shape for w in traps.check(page(head="<title> PAGE  TITLE </title>"))] == [
        "placeholder_title"
    ]


# --- the link that goes nowhere -----------------------------------------------


GALLERY = "https://artifacts.tenfourty.site/a/abc"


def test_a_gallery_link_in_the_same_tab_is_refused():
    """The page is served in a sandboxed frame that forbids top-level
    navigation and the gallery refuses to be framed, so the link does nothing
    at all. Jeremy found this on a real page on 19 Sep."""
    [found] = traps.check(page(body=f'<a href="{GALLERY}">Back to the wall</a>'))
    assert found.shape == "gallery_link_same_tab"
    assert "_blank" in found.hint


def test_a_gallery_link_that_opens_a_new_tab_is_fine():
    assert (
        traps.check(
            page(body=f'<a href="{GALLERY}" target="_blank" rel="noopener">Wall</a>')
        )
        == []
    )


def test_a_link_somewhere_else_is_not_this_check():
    """Only the gallery's own host is affected -- an ordinary external link is
    the CSP's business, or nobody's."""
    assert traps.check(page(body='<a href="https://example.com/x">Elsewhere</a>')) == []


def test_every_gallery_link_is_reported_not_just_the_first():
    body = f'<a href="{GALLERY}">One</a>\n<a href="{GALLERY}">Two</a>'
    found = traps.check(page(body=body))
    assert [w.shape for w in found] == ["gallery_link_same_tab"] * 2
    assert found[0].line < found[1].line, "and each names its own line"


# --- the id that does not exist yet -------------------------------------------


def test_the_page_id_placeholder_is_caught():
    """The old skeleton's footer read "Page id <code>ID</code>", and the id
    does not exist until publish returns (homelab-ij8.31)."""
    [found] = traps.check(page(body="<footer>Page id <code>ID</code>.</footer>"))
    assert found.shape == "placeholder_page_id"


def test_the_word_id_in_prose_is_left_alone():
    """Narrow on purpose: "ID" is an ordinary word, and only that exact
    element is unambiguous."""
    assert traps.check(page(body="<p>The ID is shown in the gallery.</p>")) == []
    assert traps.check(page(body="<p>Ask for <code>ID_TOKEN</code>.</p>")) == []


# --- the same rule the CSP check learned --------------------------------------


def test_nothing_inside_a_comment_is_reported():
    """Every shipped template carries its guidance in comments, and a check
    that fires on a page used exactly as instructed teaches the reader to
    ignore it (the CSP half learned this the same day)."""
    body = f'<!-- <a href="{GALLERY}">like this, but with target</a> -->\n<p>Real.</p>'
    assert traps.check(page(head="<!-- <title>PAGE TITLE</title> -->", body=body)) == []


# --- against what actually ships ----------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_every_shipped_template_is_clean(name):
    """Published exactly as shipped. A template that trips the check on day one
    teaches every seat that the check is noise."""
    assert traps.check((TEMPLATES / name).read_text()) == []


def test_the_skeleton_is_caught_because_it_is_the_one_that_bit(tmp_path):
    """The bare `template.html` in the skill is the file the first board was
    built from, and it still carries PAGE TITLE in both places on purpose --
    it is a skeleton. This is the check that would have stopped it reaching
    Jeremy's browser tab."""
    skeleton = pathlib.Path(".claude/skills/lab-artifacts/template.html")
    if not skeleton.exists():  # pragma: no cover - the skill may not be checked out
        pytest.skip("the skill's skeleton is not in this tree")
    found = traps.check(skeleton.read_text())
    assert [w.shape for w in found] == ["placeholder_title", "placeholder_title"]
