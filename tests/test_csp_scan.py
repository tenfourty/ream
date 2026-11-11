"""The CSP advisory reads a page once: its time grows with the page, never with its
square, and a page it cannot read in the time allowed is not passed as clean.

The patterns the checks were first written as are kept here, as the answer the new
reading must give: each looked to the end of the page from every opening tag it found
(about a minute for 128 KiB of "<link "), and each is compared with the new reading on
random pages.
"""

import random
import re
import time

import pytest
import scalecheck
from fastapi.testclient import TestClient

from ream import csp, keys, renderfind
from ream.app import create_gallery_app
from ream.renderfind import Budget, TooComplex

# --- the patterns the page was first checked with -------------------------------

_R = r"(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//"
_I = re.IGNORECASE
_OLD = {
    "remote_script": re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']?" + _R, _I),
    "remote_stylesheet": re.compile(r"<link\b[^>]*\bhref\s*=\s*[\"']?" + _R, _I),
    "remote_css_url": re.compile(r"url\(\s*[\"']?" + _R, _I),
    "css_import": re.compile(r"@import\b[^;]*" + _R, _I),
    "remote_image": re.compile(
        r"<(?:img|source)\b[^>]*\bsrc(?:set)?\s*=\s*[\"']?" + _R, _I
    ),
    "media_element": re.compile(r"<(?:video|audio)\b", _I),
    "iframe": re.compile(r"<iframe\b", _I),
    "form_element": re.compile(r"<form\b(?![^>]{0,2000}\bdata-artifact-form\b)", _I),
    "base_element": re.compile(r"<base\b", _I),
    "web_worker": re.compile(r"new\s+(?:Shared)?Worker\s*\(", _I),
    "remote_connect": re.compile(
        r"(?:fetch|EventSource|WebSocket|sendBeacon)\s*\(\s*[\"'`]"
        + _R
        + r"|\.open\s*\(\s*[\"'][A-Za-z]+[\"']\s*,\s*[\"'`]"
        + _R,
        _I,
    ),
    "window_claude": re.compile(r"\bwindow\s*\.\s*claude\b|\bclaude\s*\.\s*use\s*\("),
}
_OLD_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:--!?>|\Z))", re.DOTALL)


def _old_outside_comments(body):
    return _OLD_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), body)


def _old_check(body):
    """(shape, line) of each warning, as `csp.check` was first written."""
    body = _old_outside_comments(body)
    newlines = csp.newline_offsets(body)
    found = []
    for shape in (name for name, _finder, _hint in csp._SHAPES):
        lines = []
        for examined, match in enumerate(_OLD[shape].finditer(body), start=1):
            line = csp.line_of(match.start(), newlines)
            if line not in lines:
                lines.append(line)
            if len(lines) >= 5 or examined >= 500:
                break
        found.extend((shape, line) for line in lines)
        if len(found) >= 40:
            break
    return found[:40]


def _finder(shape):
    return next(finder for name, finder, _hint in csp._SHAPES if name == shape)


# --- the same answers ---------------------------------------------------------

# Pieces of a page: the openings and attributes the shapes look for, in both cases,
# their near misses, comments, and the letters that ignore case in unexpected ways.
OPENINGS = ["<link ", "<LINK\n", "<script ", "<SCRIPT\t", "<img ", "<source ", "<IMG\n"]
OPENINGS += ["@import ", "@IMPORT\n", "<form ", "<FORM ", "<scriptx "]
PIECES = [
    'href="https://a.css"',
    "href=//x",
    "HREF = 'http://y'",
    "src=//",
    'SRC="//x.js"',
    "srcset=//i",
    "src = https://",
    "srcſet=//",
    "ſrc=//q",
    "href=x",
    "src=x",
    "data-src=//z",
    "xsrc=//",
    "rel=stylesheet",
    "url(//a)",
    "url('https://a')",
    "//",
    "https://",
    "a:b//",
    "x",
    "data-artifact-form",
    "data-artifact-form=x",
    "xdata-artifact-form",
    "data-artifact-forms",
    "@import url(//a)",
    '"',
    "'",
    ";",
    ";",
    ">",
    ">",
    " ",
    " ",
    "\n",
    "\n",
    "<!--",
    "-->",
    "<!-->",
    "<!--->",
    "--!>",
    "--",
    "!",
    "fetch('//a')",
    'FETCH ( "https://a" )',
    ".open('GET','//x')",
    '.OPEN ( "get" , `//x` )',
    "EventSource(",
    "sendBeacon(",
    "url(",
    "new Worker(",
    "NEW  SharedWorker (",
    "window . claude",
    "window.claude",
    "claude.use (",
    "xwindow",
    "<video",
    "<iframe",
    "<base",
    "İ",
    "ı",
    "ſ",
    "K",
    "é",
    "-",
    "http:",
    "a",
]


def _random_page(rng, pieces):
    return "".join(
        rng.choice(OPENINGS) if rng.random() < 0.3 else rng.choice(pieces)
        for _ in range(rng.randint(1, 45))
    )


def test_the_comments_blanked_are_the_ones_the_pattern_blanked():
    rng = random.Random(20261111)
    for _ in range(6000):
        body = _random_page(rng, PIECES)
        assert csp.outside_comments(body) == _old_outside_comments(body), repr(body)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_each_shape_finds_what_its_pattern_found(seed):
    """Every match of every shape, not just the first few the check reports, on random
    pages made of the very things the shapes look for."""
    rng = random.Random(seed)
    matched = {shape: 0 for shape in _OLD}
    for _ in range(3000):
        body = _random_page(rng, PIECES)
        visible = _old_outside_comments(body)
        page = csp._Page(visible, csp._fold(visible))
        for shape, pattern in _OLD.items():
            want = [m.start() for m in pattern.finditer(visible)]
            got = list(_finder(shape)(page, Budget(60)))
            assert got == want, (shape, repr(body))
            matched[shape] += bool(want)
    assert all(matched.values()), (
        f"a shape that never matched proves nothing: {matched}"
    )


@pytest.mark.parametrize("seed", [4, 5])
def test_the_whole_check_gives_the_warnings_it_gave(seed):
    rng = random.Random(seed)
    for _ in range(3000):
        body = _random_page(rng, PIECES)
        got = [(w.shape, w.line) for w in csp.check(body)]
        assert got == _old_check(body), repr(body)


def test_a_line_is_counted_as_it_was_with_long_pages():
    body = "\n".join(["<p>x</p>"] * 700) + "\n<form>\n" + "\n<video>" * 3
    assert [(w.shape, w.line) for w in csp.check(body)] == _old_check(body)


def test_the_lower_case_page_is_the_page_with_its_case_ignored():
    """`_fold` stands in for `re.IGNORECASE`: for every character there is, the letter
    it folds to is the one the flag makes it equal to, and nothing the patterns care
    about (a word character, a space, a punctuation mark) changes."""
    letters = {chr(c): re.compile(chr(c), re.IGNORECASE) for c in range(97, 123)}
    any_letter = re.compile("[a-z]", re.IGNORECASE)
    word, space = re.compile(r"\w"), re.compile(r"\s")
    for code in range(0x110000):
        ch = chr(code)
        folded = csp._fold(ch)
        assert len(folded) == 1, hex(code)
        if any_letter.fullmatch(ch):
            assert folded == next(a for a, p in letters.items() if p.fullmatch(ch))
        else:
            assert not "a" <= folded <= "z", hex(code)
            if ch.isascii():
                assert folded == ch
        assert bool(word.fullmatch(ch)) == bool(word.fullmatch(folded)), hex(code)
        assert bool(space.fullmatch(ch)) == bool(space.fullmatch(folded)), hex(code)


# --- the cost of checking a page grows with its size, never with its square ----

# name: (what comes first, what is repeated, what comes last). The patterns looked to
# the end of the page from each opening in the shapes marked (was slow).
SHAPES = {
    "unclosed scripts (was slow)": ("<html>", "<script ", ""),
    "scripts with a src (was slow)": ("<html>", "<script src=a ", ""),
    "scripts with a src, then >": ("<html>", "<script src=a ", ">"),
    "unclosed links (was slow)": ("<html>", "<link ", ""),
    "links with an href (was slow)": ("<html>", "<link href=a ", ""),
    "unclosed images (was slow)": ("<html>", "<img ", ""),
    "unclosed sources (was slow)": ("<html>", "<source ", ""),
    "images with a srcset (was slow)": ("<html>", "<img srcset=a ", ""),
    "imports (was slow)": ("<html>", "@import ", ""),
    "an import of one long word (was slow)": ("<html>@import ", "a", ""),
    "an import, then a long word (was slow)": ("<html>@import //x", "a", ""),
    "forms": ("<html>", "<form ", ""),
    "forms with data-artifact-form": ("<html>", "<form data-artifact-form ", ""),
    "forms, then a >": ("<html>", "<form ", ">"),
    "css urls": ("<html>", "url( ", ""),
    "workers": ("<html>", "new ", ""),
    "fetches": ("<html>", "fetch( ", ""),
    "fetches of a word": ("<html>", "fetch ", ""),
    "opens": ("<html>", ".open('a', ", ""),
    "windows": ("<html>", "window ", ""),
    "claudes": ("<html>", "claude . ", ""),
    "comment openers": ("<html>", "<!--", ""),
    "abrupt comments": ("<html>", "<!-->", ""),
    "comments": ("<html>", "<!--x-->", ""),
    "one comment of dashes": ("<html><!--", "-", ""),
    "newlines": ("<html>", "\n", ""),
    "tags": ("<html>", "<a ", ""),
}


def _read(text):
    # Over the page's own budget on purpose: it is the reading that is timed.
    return csp._check(text, Budget(600))


@pytest.mark.scaling
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_eight_times_the_page_costs_about_eight_times_the_time(shape):
    """A reading that grows with the square of the page takes 64 times as long on
    eight times the page; one that grows with the page, about 8 (the limit is 20,
    tests/scalecheck.py), on pages of 1 MiB and 8 MiB."""
    first, unit, last = SHAPES[shape]
    scalecheck.assert_linear(_read, first, unit, last)


@pytest.mark.scaling
@pytest.mark.parametrize(
    "shape",
    [
        "unclosed scripts (was slow)",
        "unclosed links (was slow)",
        "imports (was slow)",
        "an import of one long word (was slow)",
    ],
)
def test_the_scaling_check_fails_the_old_patterns_on_the_pages_they_were_slow_on(
    shape, monkeypatch
):
    """Without this, a check that passes everything would look like a fast one."""

    def old(pattern):
        return lambda page, _budget: (m.start() for m in pattern.finditer(page.visible))

    monkeypatch.setattr(
        csp,
        "_SHAPES",
        tuple((name, old(_OLD[name]), hint) for name, _finder, hint in csp._SHAPES),
    )
    first, unit, last = SHAPES[shape]
    with pytest.raises(AssertionError, match="times as long"):
        scalecheck.assert_linear(_read, first, unit, last)


def _ordinary_page(chars):
    """A page of the usual things, to the size asked: a table of links, text with
    code, comments, cards with images, an inline script and a form."""
    rng = random.Random(7)
    head = (
        "<!doctype html>\n<html lang=en><head><meta charset=utf-8><title>Board</title>\n"
        "<style>body{font:14px system-ui}td{background:url(data:image/png;base64,AA)}"
        "</style>\n<link rel=stylesheet href=local.css>\n"
        '<script>var n=0;function go(){fetch("/api/x")}</script></head><body>\n'
        '<h1>Board</h1><form data-artifact-form="q"><input name=a></form>\n'
    )
    parts = [head]
    size = len(parts[0])
    n = 0
    while size < chars - 400:
        n += 1
        words = " ".join(
            rng.choice(["alpha", "beta", "pass", "fail", "host"]) * 2 for _ in range(12)
        )
        row = (
            f'<tr><td>{n}</td><td><a href="https://example.com/{n}" target=_blank>{words}</a></td></tr>\n',
            f"<p>{words} &amp; <code>x = {n}</code></p>\n",
            f"<!-- note {n}: {words} -->\n",
            f'<div class=card><h3>Item {n}</h3><img src="img{n % 9}.png"></div>\n',
        )[n % 4]
        parts.append(row)
        size += len(row)
    parts.append("</body></html>\n")
    return "".join(parts)


@pytest.mark.scaling
def test_an_ordinary_page_of_the_largest_size_is_read_well_inside_its_budget():
    from ream import store

    page = _ordinary_page(store.MAX_BODY_BYTES)
    assert len(page) <= store.MAX_BODY_BYTES
    budget = Budget()
    began = time.perf_counter()
    found = csp.check(page, budget)
    took = time.perf_counter() - began
    assert found == []
    assert not budget.spent
    assert took < renderfind.SCAN_SECONDS / 2, (
        f"{took:.2f} s of {renderfind.SCAN_SECONDS}"
    )


# --- a page too large or too slow to check is not checked, and says so ----------

NOT_RUN = ["csp_checks_not_run"]
FORM_PAGE = "<!doctype html><html><body><form></form>\n<video></video></body></html>"


def kinds(found):
    return [w.shape for w in found]


def test_a_page_over_the_cap_is_not_read_at_all(monkeypatch):
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", len(FORM_PAGE) - 1)
    assert kinds(csp.check(FORM_PAGE)) == NOT_RUN
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", len(FORM_PAGE))
    assert kinds(csp.check(FORM_PAGE)) == ["media_element", "form_element"]


def test_a_spent_budget_stops_the_check_before_it_starts():
    spent = Budget(0)
    assert kinds(csp.check(FORM_PAGE, spent)) == NOT_RUN
    assert [w["shape"] for w in csp.as_json(FORM_PAGE, Budget(0))] == NOT_RUN


def test_a_budget_that_runs_out_midway_gives_the_one_warning_not_some_of_them():
    class Stops(Budget):
        def __init__(self):
            super().__init__()
            self.left = 3

        def step(self):
            self.left -= 1
            if self.left < 0:
                raise TooComplex

    page = '<script src="https://a/x.js"></script>\n' * 4 + FORM_PAGE
    assert kinds(csp.check(page, Stops())) == NOT_RUN
    assert "remote_script" in kinds(csp.check(page))


def test_the_warning_says_the_checks_did_not_run():
    (warning,) = csp.check(FORM_PAGE, Budget(0))
    assert (warning.line, warning.hint) == (1, csp.NOT_RUN)
    assert "did not run" in warning.hint
    assert FORM_PAGE not in warning.hint


def test_the_budget_of_a_real_page_is_not_in_the_way():
    budget = Budget()
    csp.check(FORM_PAGE * 100, budget)
    assert not budget.spent


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
        json={"title": "Board", "body": FORM_PAGE},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    shapes = [w["shape"] for w in response.json()["warnings"]]
    assert "csp_checks_not_run" in shapes
    assert "form_element" not in shapes, "never some of the findings"
