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
import random
import re
from urllib.parse import urlsplit

import pytest
import scalecheck
from fastapi.testclient import TestClient

from ream import keys, renderfind, settings, textvars, traps
from ream.app import create_gallery_app
from ream.renderfind import Budget

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
    """ "Untitled" is a placeholder; "Untitled drafts and what to do with them"
    is a page about them. A check that fires on a correct page is how a
    warning becomes noise."""
    assert (
        traps.check(page(head="<title>Untitled drafts, and what to do</title>")) == []
    )


def test_markup_inside_the_title_does_not_hide_a_placeholder():
    assert [
        w.shape for w in traps.check(page(head="<title> PAGE  TITLE </title>"))
    ] == ["placeholder_title"]


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


def test_the_skeleton_is_caught_because_it_is_the_one_that_bit():
    """A bare skeleton still carries PAGE TITLE in both places on purpose -- it is
    a skeleton. The first board was built from one, and this is the check that
    would have stopped it reaching the reader's browser tab."""
    skeleton = page(
        head="<title>PAGE TITLE</title>", body="<h1>PAGE TITLE</h1>\n<p>Words.</p>"
    )
    found = traps.check(skeleton)
    assert [w.shape for w in found] == ["placeholder_title", "placeholder_title"]


# --- a page started from a template and not finished (the cove seat, 24 Sep) --


def _explainer() -> str:
    return (TEMPLATES / "explainer.html").read_text()


def test_a_leftover_replace_marker_is_caught_once_the_page_is_started():
    page = _explainer().replace(
        "How the nightly backup works", "Two galleries, one login"
    )
    found = [w for w in traps.check(page) if w.shape == "template_marker_left"]
    assert found, "an edited page still carrying markers must say so"
    # The author's line numbers: the marker is a comment, so it cannot be
    # counted against the comment-blanked text without drifting.
    first = next(
        n for n, line in enumerate(page.splitlines(), 1) if "<!-- REPLACE" in line
    )
    assert found[0].line == first


def test_the_example_title_left_in_an_edited_page_is_caught():
    page = _explainer().replace("Three copies, two of them", "Two of them")
    shapes = {w.shape for w in traps.check(page)}
    assert "template_example_title" in shapes


def test_a_finished_page_is_clean():
    page = re.sub(r"<!--\s*REPLACE.*?-->", "", _explainer(), flags=re.DOTALL)
    page = page.replace("How the nightly backup works", "Two galleries, one login")
    assert traps.check(page) == []


def test_a_lowercase_replace_in_prose_is_not_a_marker():
    page = _explainer().replace(
        "How the nightly backup works", "Two galleries, one login"
    )
    page = re.sub(r"<!--\s*REPLACE.*?-->", "", page, flags=re.DOTALL)
    page = page.replace("</main>", "<p>We replace the key weekly.</p></main>")
    assert traps.check(page) == []


# --- the page is read once: the same answers as the patterns it replaces ------

# The patterns the checks were written with. Each looks to the end of the page from
# every unclosed tag it meets, so the time grows with the square of the page. Held
# here, on small pages, so the one-pass reads are held to their answers.
_OLD_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_OLD_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.IGNORECASE | re.DOTALL)
_OLD_TAGS = re.compile(r"<[^>]*>")
_OLD_BLANK_TARGET = re.compile(r"\btarget\s*=\s*[\"']?_blank\b", re.IGNORECASE)


def _old_gallery_link(client):
    names = [urlsplit(textvars.gallery_origin(client)).hostname or ""]
    former = settings.current().former_host.strip().lower().removesuffix(".")
    if former:
        names.append(former)
    alternatives = "|".join(re.escape(n) for n in names)
    return re.compile(
        r"<a\b[^>]*\bhref\s*=\s*[\"']?https?://(?:" + alternatives + r")[^>]*>",
        re.IGNORECASE,
    )


def _old_element(text, name):
    found = (_OLD_TITLE if name is traps._TITLE else _OLD_H1).search(text)
    return (found.start(), found.group(1)) if found else None


def _old_plain(fragment):
    return " ".join(_OLD_TAGS.sub(" ", fragment).split()).strip()


def _old_links(text, client, budget):
    for found in _old_gallery_link(client).finditer(text):
        yield found.start(), _OLD_BLANK_TARGET.search(found.group(0)) is not None


FORMER = "old.example"
GALLERY_HOST = urlsplit(textvars.gallery_origin(False)).hostname


def _pieces():
    examples = sorted(traps._templates()[1])
    return [
        "<a", "<A", "<a ", "<ab", "<a-", "<a\n", "</a>", " href=", "HREF = '",
        f'href="https://{GALLERY_HOST}/x"', f"href=http://{GALLERY_HOST}",
        f"HREF = 'HTTPS://{GALLERY_HOST.upper()}/'", f'href="https://{FORMER}/y"',
        'href="https://other.test/z"', "https://", "target=_blank", "target = '_blank'",
        'target="_blank"', "target=_blankx", "target", "rel=noopener", " ", "\n", ">", ">",
        "<", '"', "'", "=", "<title>", "</title>", "</TITLE >", "<title ", "<titles>",
        "<h1>", "<h1 class=x>", "</h1>", "</H1  >", "<b>", "</b>", "Hello",
        "PAGE TITLE", "Untitled", "Document", "<code>ID</code>", "<code> id </code>",
        "<code>", "</code>", "<!-- REPLACE", "<!--", "-->", "<!---->", "<!-->",
        "<title>PAGE TITLE</title>", "<h1>TODO</h1>",
        *[f"<title>{e}</title>" for e in examples],
        *[f"<h1>{e}</h1>" for e in examples],
    ]  # fmt: skip


@pytest.fixture(params=[False, True], ids=["no former host", "a former host"])
def former_host(request, monkeypatch):
    if request.param:
        monkeypatch.setenv("REAM_FORMER_HOST", FORMER)
    return request.param


def test_the_one_pass_reads_find_what_the_patterns_found(former_host):
    rng = random.Random(20261111)
    pieces = _pieces()
    for _ in range(1500):
        text = "".join(rng.choices(pieces, k=rng.randint(1, 30)))
        for name in (traps._TITLE, traps._H1):
            assert traps._element(text, name) == _old_element(text, name), text
        assert traps._plain(text) == _old_plain(text), text
        for client in (False, True):
            assert list(traps._gallery_links(text, client, Budget(60))) == list(
                _old_links(text, client, None)
            ), text


def test_the_whole_check_gives_the_warnings_it_gave(former_host, monkeypatch):
    rng = random.Random(20261112)
    pieces = _pieces()
    pages = ["".join(rng.choices(pieces, k=rng.randint(1, 30))) for _ in range(1500)]
    got = [traps.check(text) for text in pages]
    monkeypatch.setattr(traps, "_element", _old_element)
    monkeypatch.setattr(traps, "_plain", _old_plain)
    monkeypatch.setattr(traps, "_gallery_links", _old_links)
    assert got == [traps.check(text) for text in pages]
    seen = {w.shape for warnings in got for w in warnings}
    assert seen >= {
        "placeholder_title",
        "template_example_title",
        "template_marker_left",
        "placeholder_page_id",
        "gallery_link_same_tab",
    }, "the random pages must reach every warning for this to mean anything"


# --- the cost of checking a page grows with its size, never with its square ----

LINK = f'<a href="https://{GALLERY_HOST}/x" '
OTHER = '<a href="https://other.test/x">'
# name: (what comes first, what is repeated, what comes last). The patterns the page was
# checked with looked to the end of the page from every one of these.
SHAPES = {
    "unclosed anchors": ("", "<a ", ""),
    "unclosed links to the gallery": ("", LINK, ""),
    "one anchor, then hrefs to the gallery": ("<a ", LINK[3:], ""),
    "a link to the gallery, then unclosed anchors": (LINK + ">", "<a ", ""),
    "a link to the gallery, then other links": (LINK + ">", OTHER, ""),
    "links to the gallery with no target": ("", LINK + ">", ""),
    "links to the gallery with a target": ("", LINK + "target=_blank>", ""),
    "a link to the gallery, then target again and again": (LINK, "target ", ">"),
    "links elsewhere": ("", OTHER, ""),
    "unclosed titles": ("", "<title>", ""),
    "titles with no >": ("", "<title ", ""),
    "unclosed h1": ("", "<h1>", ""),
    "h1 with no >": ("", "<h1 ", ""),
    "a title of unclosed tags": ("<title>", "<", "</title>"),
    "a title of tags": ("<title>", "<b>", "</title>"),
    "an h1 of unclosed tags": ("<h1>", "<", "</h1>"),
    "a title opened twice": ("<title <title>", "<title>", ""),
    "unclosed code tags": ("", "<code> ", ""),
    "code tags with a word": ("", "<code> ID ", ""),
    "comment openers": ("", "<!--", ""),
    "comments": ("", "<!-- x -->", ""),
    "whitespace after a comment opener": ("", "<!--" + " " * 20, ""),
}


def _checked(text):
    found = traps.check(text, budget=Budget(600))
    assert "trap_checks_not_run" not in [w.shape for w in found], "not read"


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.scaling
def test_eight_times_the_page_costs_about_eight_times_the_time(shape):
    """A check that grows with the square of the page takes 64 times as long on eight
    times the page; one that grows with the page, about 8 (the limit is 20,
    tests/scalecheck.py), on pages of 1 MiB and 8 MiB."""
    first, unit, last = SHAPES[shape]
    scalecheck.assert_linear(_checked, first, unit, last)


# The shapes the patterns were slow on.
_WAS_SLOW = [
    "unclosed anchors",
    "unclosed titles",
    "unclosed h1",
    "a title of unclosed tags",
    "an h1 of unclosed tags",
]


@pytest.mark.parametrize("shape", _WAS_SLOW)
def test_the_scaling_check_fails_the_old_patterns_on_the_pages_they_were_slow_on(
    shape, monkeypatch
):
    """Without this, a check that passes everything would look like a fast one."""
    monkeypatch.setattr(traps, "_element", _old_element)
    monkeypatch.setattr(traps, "_plain", _old_plain)
    monkeypatch.setattr(traps, "_gallery_links", _old_links)
    first, unit, last = SHAPES[shape]
    with pytest.raises(AssertionError, match="times as long"):
        scalecheck.assert_linear(_checked, first, unit, last)


# --- a page too large or too slow to check is not checked, and says so ----------

NOT_RUN = ["trap_checks_not_run"]


def kinds(found):
    return [w.shape for w in found]


def test_a_page_over_the_cap_is_not_read_at_all(monkeypatch):
    body = page(head="<title>PAGE TITLE</title>")
    assert kinds(traps.check(body)) == ["placeholder_title"]

    def reading(*args, **kwargs):
        raise AssertionError("the page was read")

    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", len(body) - 1)
    monkeypatch.setattr(traps, "outside_comments", reading)
    monkeypatch.setattr(traps, "_element", reading)
    found = traps.check(body)
    assert kinds(found) == NOT_RUN
    assert found[0].line == 1
    assert "did not run" in found[0].hint
    monkeypatch.undo()
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", len(body))
    assert kinds(traps.check(body)) == ["placeholder_title"]


def test_a_spent_budget_stops_the_checks_before_they_start(monkeypatch):
    body = page(head="<title>PAGE TITLE</title>", body="<code>ID</code>")
    spent = Budget(0)
    monkeypatch.setattr(traps, "outside_comments", lambda *_: 1 / 0)
    assert kinds(traps.check(body, budget=spent)) == NOT_RUN
    assert spent.spent


def test_a_budget_that_runs_out_midway_gives_the_one_warning_not_some_of_them():
    body = page(
        head="<title>PAGE TITLE</title>",
        body="<code>ID</code>" + f'<a href="{GALLERY}">x</a>' * 50,
    )
    assert len(traps.check(body)) > 3

    class Short(Budget):
        def step(self):
            self.steps = getattr(self, "steps", 0) + 1
            if self.steps > 5:
                self.spent = True
            super().step()

    found = traps.check(body, budget=Short())
    assert kinds(found) == NOT_RUN


def test_a_page_the_check_could_not_read_adds_no_other_warning(monkeypatch):
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", 10)
    body = page(
        head="<title>PAGE TITLE</title><!-- REPLACE -->", body=f'<a href="{GALLERY}">x'
    )
    assert kinds(traps.check(body)) == NOT_RUN
    assert [w["shape"] for w in traps.as_json(body)] == NOT_RUN


def test_the_budget_of_a_real_page_is_not_in_the_way():
    """The shipped templates, and a page of ordinary size, are inside both limits."""
    body = page(body=("<p>Words <a href='/x'>here</a>.</p>\n" * 40_000))
    assert len(body) > 1_000_000
    assert traps.check(body) == []


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def test_a_publish_that_could_not_be_checked_succeeds_and_says_so(
    client, db, monkeypatch
):
    token, _ = keys.mint(db, agent="ops-seat")
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", 10)
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": page(head="<title>PAGE TITLE</title>")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    assert "trap_checks_not_run" in [w["shape"] for w in response.json()["warnings"]]
