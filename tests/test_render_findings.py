"""What the service's own render found wrong with a page: the check script's
line, the static missing-file check, the verdict, and where they are kept and
read back (runcove-5t3oj.56, slice 1)."""

import html.parser
import random
import re
import time
import tracemalloc
from html.parser import HTMLParser

import pytest
import scalecheck
from fastapi.testclient import TestClient

from ream import api as api_module
from ream import csp, htmlbound, keys, renderfind, store, thumbs
from ream.app import create_gallery_app

pytestmark = pytest.mark.background_thumbnails

NONCE = "0123456789abcdef"
GOOD = {"text": 40, "media": 1, "height": 600, "scroll": 900, "view": 900}


def line(nonce=NONCE, text=40, media=1, height=600, scroll=900, view=900):
    body = (
        f"ream-check {nonce} text={text} media={media} height={height}"
        f" scroll={scroll} view={view}"
    )
    return f'[1:1:0101/000000.1:INFO:CONSOLE:1] "{body}", source: file:///p.html (1)\n'


# --- the check line ----------------------------------------------------------


def test_the_right_nonce_is_accepted():
    assert renderfind.parse_check(line(), NONCE) == GOOD


def test_a_forged_line_with_another_nonce_is_ignored():
    assert renderfind.parse_check(line(nonce="f" * 16, text=0, media=0), NONCE) is None


def test_the_last_line_with_the_right_nonce_wins():
    both = line(text=0, media=0) + line(text=7)
    assert renderfind.parse_check(both, NONCE)["text"] == 7


def test_no_line_means_none_never_fine():
    assert renderfind.parse_check("", NONCE) is None
    assert renderfind.parse_check("noise\n", NONCE) is None


def test_a_malformed_or_oversized_line_is_dropped():
    bad = line().replace("text=40", "text=forty")
    assert renderfind.parse_check(bad, NONCE) is None
    huge = line(text=10**15)
    assert renderfind.parse_check(huge, NONCE) is None


def test_the_script_goes_first_in_the_head_and_carries_the_nonce():
    page = "<!doctype html><html><head><title>t</title></head><body>x</body></html>"
    out = renderfind.inject(page, NONCE)
    assert out.index("<head>") + len("<head>") == out.index("<script>")
    assert NONCE in out and out.endswith("</html>")
    assert out.replace(renderfind.script(NONCE), "") == page


def test_inject_without_a_head_still_places_it():
    out = renderfind.inject("<p>hi</p>", NONCE)
    assert NONCE in out and out.endswith("<p>hi</p>")
    out = renderfind.inject("<html><body>x</body></html>", NONCE)
    assert out.index("<script>") < out.index("<body>")


def test_a_head_inside_a_title_with_no_real_head_is_not_a_head():
    page = "<html><title><head></title><body>x</body></html>"
    out = renderfind.inject(page, NONCE)
    assert out.index("<script>") < out.index("<title>")
    assert out.replace(renderfind.script(NONCE), "") == page


def test_the_forged_head_cases_place_the_script_at_the_real_head():
    a = renderfind.inject("<html><head><title>&lt;<head></title></head></html>", NONCE)
    assert a.index("<script>") < a.index("<title>")
    b = renderfind.inject("<!-- <head> --><html><head></head></html>", NONCE)
    assert b.index("<script>") > b.index("-->")
    assert b.index("<script>") < b.index("</head>")


def test_a_doctype_and_comments_stay_before_the_script():
    out = renderfind.inject("<!doctype html><p>hi</p>", NONCE)
    assert out.startswith("<!doctype html>") and out.index("<script>") < out.index(
        "<p>"
    )


def test_an_error_listener_is_registered_in_capture_before_page_code():
    tag = renderfind.script(NONCE)
    assert "addEventListener('error'" in tag and ",true)" in tag


def test_error_lines_with_the_right_nonce_are_read():
    log = (
        f'[1:1:0101/000000.1:INFO:CONSOLE:1] "ream-error {NONCE} line=3: boom", source: x (1)\n'
        f'[1:1:0101/000000.1:INFO:CONSOLE:1] "ream-error {"f" * 16} line=4: forged", source: x (1)\n'
    )
    assert renderfind.parse_errors(log, NONCE) == ["line 3: boom"]


def test_live_data_is_read_from_scripts_not_from_text_or_comments():
    assert not renderfind.loads_live_data("<p>we fetch(x) data</p><!-- fetch( -->")
    assert renderfind.loads_live_data("<script>fetch('/x')</script>")


# --- the static missing-file check -------------------------------------------


def test_relative_and_root_relative_paths_are_flagged():
    page = (
        '<img src="logo.png"><script src="/app.js"></script>'
        '<link rel="stylesheet" href="css/site.css">'
    )
    assert renderfind.missing_files(page) == ["logo.png", "/app.js", "css/site.css"]


def test_what_is_served_or_not_a_file_is_not_flagged():
    page = (
        '<link rel="stylesheet" href="/_shared.css">'
        '<img src="data:image/png;base64,AAAA"><a href="#top">x</a>'
        '<a href="other.html">navigation is not a file</a>'
        '<img src="https://x.test/a.png"><img src="//x.test/a.png">'
        '<link rel="canonical" href="page.html"><img src="">'
    )
    assert renderfind.missing_files(page) == []


def test_comments_and_scripts_are_ignored():
    page = (
        '<!-- <img src="gone.png"> -->'
        "<script>var s = '<img src=\"inside.png\">';</script>"
    )
    assert renderfind.missing_files(page) == []


def test_live_data_is_recognised():
    assert renderfind.loads_live_data("<script>fetch('/api/v1/x')</script>")
    assert renderfind.loads_live_data("<script>await window.claude.use('db')</script>")
    assert not renderfind.loads_live_data("<p>fetch the milk</p>")


def test_a_script_with_a_src_counts_and_the_read_goes_on_after_its_block():
    page = '<script src="a.js">var s = "<img src=x.png>";</script><img src="b.png">'
    assert renderfind.missing_files(page) == ["a.js", "b.png"]


# The two patterns the page is no longer read with: each one looks to the end of
# the page from every unclosed tag. Kept here, on small pages only, so the
# single pass is held to the answers they gave.
_OLD_TAG = re.compile(
    r"<(img|script|source|video|audio|track|iframe|embed|link|object)\b([^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_OLD_SCRIPT_BLOCK = re.compile(
    r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL
)
_PIECES = [
    "<img", "<IMG", "<script", "<SCRIPT", "</script", "</script>", "</SCRIPT  >",
    "<link", "<object", "<source", "<a", "<scripts", "<script-x", "<imgx", ">", ">",
    " ", "\n", '"', "'", "=", "src=", "href=", "data=", "data-src=", "rel=",
    "rel=stylesheet", "x.png", "/a.js", "#top", "//cdn.test/x", "http://x.test/y",
    "<!--", "-->", "--!>", "<!--->", "fetch(", "<", "&", "text", "<p>",
]  # fmt: skip


def _old_tags(html):
    def keep_open(match):
        block = match.group(0)
        return block[: block.index(">") + 1] + "</script>"

    body = _OLD_SCRIPT_BLOCK.sub(keep_open, csp.outside_comments(html))
    return [(m.group(1).lower(), m.group(2)) for m in _OLD_TAG.finditer(body)]


def test_the_one_pass_read_finds_the_tags_the_patterns_found():
    rng = random.Random(20261111)
    for _ in range(6000):
        page = "".join(rng.choices(_PIECES, k=rng.randint(1, 40)))
        got = list(renderfind._tags(page, renderfind.Budget(60)))
        assert got == _old_tags(page), page


def _tokens(parser, page):
    """Every token `parser` reports for `page`, in order, and the text of each start tag."""
    seen = []

    def note(kind):
        def handler(*args):
            tag = kind.endswith("tag") and not kind.startswith("end")
            seen.append((kind, *args, parser.get_starttag_text() if tag else None))

        return handler

    for kind in (
        "starttag", "startendtag", "endtag", "data", "comment", "decl", "pi",
        "unknown_decl", "charref", "entityref",
    ):  # fmt: skip
        setattr(
            parser, kind if kind == "unknown_decl" else "handle_" + kind, note(kind)
        )
    parser.feed(page)
    parser.close()
    return seen


def test_a_start_tag_with_no_bracket_after_it_is_read_as_the_parser_reads_it():
    """`_Metered` skips the parser's own test for a tag with no ">" after it; the
    tokens must be the ones the parser gives without that."""
    pieces = [*_PIECES, "\x00", "/", "<img src='", '<a href="x" ', "<img <img ", "=>"]
    rng = random.Random(20261112)
    for _ in range(6000):
        page = "".join(rng.choices(pieces, k=rng.randint(1, 40)))
        got = _tokens(renderfind._Metered(renderfind.Budget(60)), page)
        assert got == _tokens(HTMLParser(convert_charrefs=False), page), page


@pytest.mark.skipif(
    not htmlbound._TAG_END_NEEDS_A_GT,
    reason="this Python's parser tests tags another way",
)
def test_a_start_tag_with_no_bracket_after_it_does_not_reach_the_parsers_long_match(
    monkeypatch,
):
    """The match that reads such a tag to the end of the page costs memory and time
    in kernel page faults, not in step with the page (the scaling tests below); it
    is not run at all when no ">" follows, and is when one does."""
    ran = []
    real = html.parser.locatetagend

    class Counting:
        def match(self, *args):
            ran.append(args)
            return real.match(*args)

    monkeypatch.setattr(html.parser, "locatetagend", Counting())
    for page in ("<img " * 1000, '<a href="x' * 1000, "<p>ok</p><img src="):
        assert renderfind.loads_live_data("fetch( " + page) is False
        assert renderfind.inject(page, NONCE).startswith(renderfind.script(NONCE))
    # the closed <p> and </p> of the third page, in each of the two reads, and no more
    assert len(ran) == 4
    assert all(">" in text[at:] for text, at, *_ in ran)
    ran.clear()
    assert renderfind.loads_live_data("fetch( <img <img >") is False
    assert len(ran) == 1


# --- a tag too long for the parser's match --------------------------------------

_TAG_PIECES = [
    "img", "a", "x y", " ", " ", "\n", "\t", "\f", "/", "=", "b=", "'", '"', "'x'",
    '"y"', "'>'", '">"', ">", ">", "=>", "/>", "<img ", "<", "\u00e9",
]  # fmt: skip


def _the_parsers_end_of_tag(text, name):
    """Where the parser's own match ends the tag whose name begins at `name`, as the
    parser takes it (after its ">"), or -1 when it is not whole."""
    end = html.parser.locatetagend.match(text, name).end()
    return end if text[end - 1] == ">" else -1


@pytest.mark.skipif(
    not htmlbound._TAG_END_NEEDS_A_GT,
    reason="this Python's parser tests tags another way",
)
@pytest.mark.parametrize("window", [1, 6, 30, htmlbound.TAG_WINDOW])
def test_the_end_of_a_tag_is_where_the_parser_puts_it_in_a_window_and_out_of_it(
    window, monkeypatch
):
    """Inside the window the parser's own match answers; outside it the tag is read an
    attribute at a time. Both give what the parser's match gives on the whole page
    (the window sizes below the tags' length make most of them take the second way)."""
    monkeypatch.setattr(htmlbound, "TAG_WINDOW", window)
    rng = random.Random(20261113 + window)
    whole = 0
    for _ in range(8000):
        text = "<i" + "".join(rng.choices(_TAG_PIECES, k=rng.randint(0, 24)))
        got = htmlbound.end_of_tag(text, 1, renderfind.Budget(60))
        assert got == _the_parsers_end_of_tag(text, 1), repr(text)
        whole += got > 0
    assert 1000 < whole < 7000, f"whole tags and open ones both, not {whole} of 8000"


@pytest.mark.parametrize("window", [8, htmlbound.TAG_WINDOW])
def test_a_page_is_read_in_tokens_as_the_parser_reads_it_whatever_the_window(
    window, monkeypatch
):
    monkeypatch.setattr(htmlbound, "TAG_WINDOW", window)
    pieces = [*_PIECES, *_TAG_PIECES, "</a ", "</a b='x>'>", "</a\n>", "<a b='>' "]
    pieces += ["</ a>", "</>", "<script>", "</script", "<!-- ", "-->"]
    rng = random.Random(20261114 + window)
    for _ in range(4000):
        page = "".join(rng.choices(pieces, k=rng.randint(1, 40)))
        got = _tokens(renderfind._Metered(renderfind.Budget(60)), page)
        assert got == _tokens(HTMLParser(convert_charrefs=False), page), page


def _long_value():
    return "A" * (3 * htmlbound.TAG_WINDOW)


def test_a_tag_longer_than_the_window_with_few_attributes_is_read():
    """The advice for an image the CSP will not load is to inline it as a data: address:
    one tag, and a megabyte of it. A page that does is a page like any other."""
    big = f'<img alt="a > b" src="data:image/png;base64,{_long_value()}">'
    page = f"<html><head>{big}</head><body><script>fetch('/x')</script></body></html>"
    assert renderfind.loads_live_data(page) is True
    quiet = page.replace("fetch", "fetch ")  # still a script; and one with no live data
    assert renderfind.loads_live_data(quiet.replace("fetch (", "fe(")) is False
    out = renderfind.inject(page, NONCE)
    assert out.startswith("<html><head>" + renderfind.script(NONCE) + "<img ")
    assert _tokens(renderfind._Metered(renderfind.Budget(60)), page) == _tokens(
        HTMLParser(convert_charrefs=False), page
    )


def test_a_tag_with_more_attributes_than_a_window_of_it_allows_is_not_read():
    """Over the window, a tag is read an attribute at a time and given up on at
    MAX_TAG_ATTRIBUTES; inside the window the parser's own match is trusted as it
    always was (its memory there is a few megabytes)."""
    inside = "fetch( <img" + " a" * (htmlbound.TAG_WINDOW // 4) + ">"
    outside = "fetch( <img" + " a" * htmlbound.TAG_WINDOW + ">"
    few = "fetch( <img" + " a" * (htmlbound.MAX_TAG_ATTRIBUTES // 2)
    few += ' alt="' + _long_value() + '">'
    assert renderfind.loads_live_data(inside) is False
    assert renderfind.loads_live_data(outside) is None
    assert renderfind.loads_live_data(few) is False
    assert renderfind.inject(outside, NONCE).startswith(renderfind.script(NONCE))


@pytest.mark.parametrize(
    "page",
    [
        "fetch( <img " * 30000 + ">",
        'fetch( <img a=">" ' + "x " * 100000 + ">",
        "fetch( </a " * 30000 + ">",
        "fetch( </a b='>' " + "x " * 100000 + ">",
    ],
    ids=["start tags", "a > in quotes first", "end tags", "an end tag, a > in quotes"],
)
def test_a_tag_that_runs_on_to_the_end_of_the_page_leaves_it_unread(page):
    """The ">" at the very end is the one that closes the first tag: the next one
    (inside quotes, or none) is not the measure, nor is the tag's first ">"."""
    began = time.perf_counter()
    assert renderfind.loads_live_data(page) is None
    assert renderfind.inject(page, NONCE).startswith(renderfind.script(NONCE))
    assert renderfind.static_scan(page)[1] is None
    assert time.perf_counter() - began < 5


# --- the cost of reading a page grows with its size, never with its square -----


FETCH = "fetch( "  # in the text, so the live-data read cannot skip the page

# name: (what comes first, what is repeated). Each pattern the page used to be
# read with looked to the end of the page from every one of these.
SHAPES = {
    "unclosed img tags": ("", "<img "),
    "unclosed script tags": ("", "<script>"),
    "a tag, then unclosed links": ("<a ", "<link "),
    "one long unclosed quote": ('<img src="', "a"),
    "many unclosed quotes": ("", '<img src="x'),
    "script closers with no >": ("<script>", "</script"),
    "src= again and again": ("<img ", "src=x "),
    "rel= again and again": ("<link ", "rel="),
    "comment openers": ("", "<!--"),
}
READS = {
    "missing_files": lambda page, budget: renderfind.missing_files(page, budget),
    "loads_live_data": lambda page, budget: renderfind.loads_live_data(page, budget),
    "inject": lambda page, budget: renderfind.inject(page, NONCE, budget),
}
# What each read has to see first for its work to be done at all, in the page's
# own characters (a page over the cap is not read, so nothing can be added to one).
STARTS = {"loads_live_data": FETCH}


# The patterns that were slow were the ones in `missing_files`, so it gets every
# shape; the two tokenizer reads get the shapes that make a tokenizer work.
_TOKENIZED = {
    "unclosed img tags",
    "unclosed script tags",
    "a tag, then unclosed links",
    "many unclosed quotes",
    "comment openers",
}
CASES = [
    (read, shape)
    for read in READS
    for shape in SHAPES
    if read == "missing_files" or shape in _TOKENIZED
]


@pytest.mark.parametrize(("read", "shape"), CASES)
@pytest.mark.scaling
def test_eight_times_the_page_costs_about_eight_times_the_time(read, shape):
    """A read that grows with the square of the page takes 64 times as long on
    eight times the page; one that grows with the page, about 8 (the limit is 20,
    tests/scalecheck.py), on pages of 1 MiB and 8 MiB."""
    first, unit = SHAPES[shape]

    def reads(text):
        done = READS[read](text, renderfind.Budget(600))
        assert done is not None, "the page was not read"

    scalecheck.assert_linear(reads, STARTS.get(read, "") + first, unit)


# A tag that runs on to a ">" at the very end of the page, which the parser's match
# reads in one go (3 to 4 seconds and 570 MB on a page of 8 MiB, where no budget can
# stop it): the reads stop after the window, whatever the page's size.
UNREAD = {
    "unclosed img tags, one > at the end": ("fetch( ", "<img ", ">"),
    "the same, after a > in quotes": ('fetch( <img a=">" ', "x ", ">"),
    "unclosed end tags, one > at the end": ("fetch( ", "</a ", ">"),
}
UNREAD_READS = ["loads_live_data", "inject"]


@pytest.mark.parametrize("read", UNREAD_READS)
@pytest.mark.parametrize("shape", sorted(UNREAD))
@pytest.mark.scaling
def test_a_page_that_runs_on_to_the_end_costs_the_same_on_eight_times_the_page(
    read, shape
):
    first, unit, last = UNREAD[shape]

    def reads(text):
        READS[read](text, renderfind.Budget(600))

    _, _, large = scalecheck.assert_linear(reads, first, unit, last, probe=False)
    assert large < renderfind.SCAN_SECONDS / 2, f"{large:.2f} s on the largest page"


def _peak_memory(read, page):
    """(what the read gave, the most memory in use at once while it ran, in bytes)."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        done = READS[read](page, renderfind.Budget(600))
        return done, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("read", UNREAD_READS)
@pytest.mark.parametrize("shape", sorted(UNREAD))
def test_a_page_that_runs_on_to_the_end_is_given_up_on_in_a_few_copies_of_it(
    read, shape
):
    """The most the reads keep is a copy or two of the page and the state of the
    parser's match in its window (a few megabytes): four pages' worth is the bound,
    where the match on the whole page took 70 times the size of the page."""
    first, unit, last = UNREAD[shape]
    page = scalecheck.page(first, unit, renderfind.MAX_SCAN_CHARS, last)
    done, peak = _peak_memory(read, page)
    if read == "loads_live_data":
        assert done is None
    assert peak < 4 * len(page), f"{peak / 2**20:.0f} MiB for {len(page) / 2**20:.0f}"


@pytest.mark.skipif(
    not htmlbound._TAG_END_NEEDS_A_GT,
    reason="this Python's parser tests tags another way",
)
def test_the_memory_meter_sees_what_the_parsers_long_match_keeps():
    """tracemalloc counts the regular expression's state (it is allocated through the
    interpreter's own allocator); without this, a meter that saw nothing would pass
    the page the match takes 570 MB on."""
    page = scalecheck.page("<img ", "<img ", 2**20, ">")
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        html.parser.locatetagend.match(page, 1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak > 8 * len(page), f"{peak / 2**20:.0f} MiB for {len(page) / 2**20:.0f}"


def test_the_big_page_is_inside_the_cap_so_it_is_read():
    assert 8 * scalecheck.MOST <= renderfind.MAX_SCAN_CHARS


@pytest.mark.parametrize("probe", [True, False])
def test_the_scaling_check_fails_a_read_that_grows_with_the_square(probe):
    """Without this, a check that passes everything would look like a fast read."""
    quadratic = re.compile(r"<a[^>]*>")
    with pytest.raises(AssertionError, match="times as long"):
        scalecheck.assert_linear(quadratic.search, "", "<a ", start=4096, probe=probe)


def test_pathological_pages_are_read_at_once_and_still_answered_sanely():
    gone = '<img src="gone.png">'
    for page, missing in (
        ("<img " * 200_000 + 'src="gone.png">', ["gone.png"]),
        ("<script>" * 200_000 + gone, ["gone.png"]),
        ("<a " + "<link " * 200_000, []),
        ('<img src="' + "a" * 1_000_000, []),
    ):
        began = time.perf_counter()
        assert renderfind.missing_files(page) == missing
        assert renderfind.loads_live_data(page) is False
        assert renderfind.inject(page, NONCE).startswith(renderfind.script(NONCE))
        assert time.perf_counter() - began < 5


# --- a page too large or too slow to read is not read, and is never clean -------


class _Counting(renderfind.Budget):
    steps = 0

    def step(self):
        self.steps += 1
        super().step()


def test_the_cap_is_inside_the_publish_limit():
    assert renderfind.MAX_SCAN_CHARS <= store.MAX_BODY_BYTES


def test_a_page_over_the_cap_is_not_read_at_all(monkeypatch):
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", 10)
    read = []

    def reading(*args, **kwargs):
        read.append(args)
        raise AssertionError("the page was read")

    monkeypatch.setattr(renderfind.csp, "outside_comments", reading)
    monkeypatch.setattr(renderfind, "_Scripts", reading)
    monkeypatch.setattr(renderfind, "_Opener", reading)
    page = "<img src=a>"  # 11 characters
    assert renderfind.missing_files(page) is None
    assert renderfind.loads_live_data(page) is None
    assert renderfind.static_scan(page) == (None, None)
    assert renderfind.inject(page, NONCE) == renderfind.script(NONCE) + page
    assert read == []
    monkeypatch.undo()
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", len(page))
    assert renderfind.missing_files(page) == ["a"]


def test_a_spent_budget_stops_every_read_before_it_starts():
    page = '<script>fetch("/x")</script><img src="a.png">'
    spent = renderfind.Budget(0)
    assert renderfind.missing_files(page, spent) is None
    assert spent.spent
    assert renderfind.loads_live_data(page, renderfind.Budget(0)) is None
    out = renderfind.inject(page, NONCE, renderfind.Budget(0))
    assert out == renderfind.script(NONCE) + page


def test_a_parser_that_fails_is_not_read_and_never_no_live_data(monkeypatch):
    """A failure that is not the budget running out leaves the page unread: None, the
    answer that makes the verdict unchecked, and never False, which says there is no
    live data in a page that was not read to its end."""
    page = '<script>fetch("/x")</script>'

    def fails(self, data):
        raise ValueError("the parser gave up")

    assert renderfind.loads_live_data(page) is True
    monkeypatch.setattr(renderfind._Scripts, "feed", fails)
    assert renderfind.loads_live_data(page) is None
    assert renderfind.static_scan(page) == ([], None)
    verdict, found = renderfind.assess([], {**GOOD, "text": 0, "media": 0}, [], None)
    assert verdict == "unchecked"
    assert "unchecked" in kinds(found)


def test_a_failure_while_looking_for_missing_files_is_not_read_and_never_none_missing(
    monkeypatch,
):
    """As for the live-data read: any failure that is not the budget running out leaves
    the page unread, None, and never the empty list that says nothing is missing."""
    page = '<img src="logo.png">'

    def fails(*_args):
        raise ValueError("the reader gave up")

    assert renderfind.missing_files(page) == ["logo.png"]
    monkeypatch.setattr(renderfind, "_tags", fails)
    assert renderfind.missing_files(page) is None
    assert renderfind.static_scan(page)[0] is None


def test_the_budget_reads_the_clock_every_few_tokens_and_stays_spent(monkeypatch):
    now = [100.0]

    class Clock:
        monotonic = staticmethod(lambda: now[0])

    monkeypatch.setattr(renderfind, "time", Clock)
    budget = renderfind.Budget(1.0)
    budget.check()
    now[0] = 102.0
    for _ in range(63):
        budget.step()  # between two looks at the clock
    for _ in range(3):
        with pytest.raises(renderfind.TooComplex):
            budget.step()
    now[0] = 0.0
    with pytest.raises(renderfind.TooComplex):
        budget.check()  # spent stays spent


def test_the_two_reads_of_a_request_draw_on_one_budget(monkeypatch):
    page = '<script>fetch("/x")</script><img src="a.png">'
    made = []

    class Spy(renderfind.Budget):
        def __init__(self, *args):
            super().__init__(*args)
            made.append(self)

    monkeypatch.setattr(renderfind, "Budget", Spy)
    assert renderfind.static_scan(page) == (["a.png"], True)
    assert len(made) == 1

    class OneLook(renderfind.Budget):
        looks = 0

        def check(self):
            self.looks += 1
            if self.looks > 1:
                self.spent = True
            super().check()

    monkeypatch.setattr(renderfind, "Budget", OneLook)
    assert renderfind.static_scan(page) == (["a.png"], None)


def test_the_scans_stop_when_they_have_what_they_need():
    rest = "<p>x</p>" * 2000
    budget = _Counting()
    renderfind.inject("<html><head>" + rest, NONCE, budget)
    assert budget.steps <= 3
    budget = _Counting()
    assert renderfind.loads_live_data('<script>fetch("/x")</script>' + rest, budget)
    assert budget.steps <= 3


# --- the verdict -------------------------------------------------------------


def kinds(findings):
    return [f["kind"] for f in findings]


def test_a_clean_render_is_ok():
    assert renderfind.assess([], GOOD, [], False) == ("ok", [])


def test_a_script_error_is_broken():
    verdict, found = renderfind.assess(["line 3: Uncaught Error: x"], GOOD, [], False)
    assert verdict == "broken" and kinds(found) == ["script_error"]
    assert found[0]["detail"] == "line 3: Uncaught Error: x"


def test_a_blank_page_is_broken_and_so_is_one_with_no_height():
    blank = {**GOOD, "text": 0, "media": 0}
    assert renderfind.assess([], blank, [], False)[0] == "broken"
    flat = {**GOOD, "height": 0}
    verdict, found = renderfind.assess([], flat, [], False)
    assert verdict == "broken" and kinds(found) == ["blank"]


def test_a_missing_file_is_broken():
    verdict, found = renderfind.assess([], GOOD, ["logo.png"], False)
    assert verdict == "broken" and "logo.png" in found[0]["detail"]


def test_too_wide_is_for_the_agent_only_and_does_not_break():
    verdict, found = renderfind.assess([], {**GOOD, "scroll": 2000}, [], False)
    assert verdict == "ok" and kinds(found) == ["too_wide"]


def test_no_check_line_is_unchecked_never_ok():
    verdict, found = renderfind.assess([], None, [], False)
    assert verdict == "unchecked" and kinds(found) == ["unchecked"]


def test_a_missing_check_does_not_hide_a_script_error():
    verdict, found = renderfind.assess(["line 1: Uncaught x"], None, [], False)
    assert verdict == "broken" and "script_error" in kinds(found)


def test_a_live_page_blank_or_error_goes_to_the_agent_only():
    blank = {**GOOD, "text": 0, "media": 0}
    verdict, found = renderfind.assess(["line 1: Uncaught x"], blank, [], True)
    assert verdict == "unchecked"
    assert set(kinds(found)) == {"blank", "script_error"}
    assert all("live data" in f["detail"] for f in found)
    # a missing file is still certain on a live page
    assert renderfind.assess([], GOOD, ["a.png"], True)[0] == "broken"


def test_a_page_that_was_not_read_is_never_ok_and_says_so():
    said = {"kind": "unchecked", "detail": renderfind.UNREAD_NOTE}
    for missing, live in (([], None), (None, False), (None, None)):
        verdict, found = renderfind.assess([], GOOD, missing, live)
        assert verdict == "unchecked" and found == [said]
    # a missing file that was found is certain, read or not
    verdict, found = renderfind.assess([], GOOD, ["a.png"], None)
    assert verdict == "broken" and found[0] == said
    # a blank page or a script error on a page not read goes to the agent alone
    blank = {**GOOD, "text": 0, "media": 0}
    verdict, found = renderfind.assess(["line 1: Uncaught x"], blank, None, None)
    assert verdict == "unchecked"
    assert kinds(found) == ["unchecked", "blank", "script_error"]
    assert not any(renderfind.LIVE_NOTE in f["detail"] for f in found)
    # and the finding is not pushed out by a long list of others
    errors = [f"line {n}: Uncaught x" for n in range(30)]
    assert renderfind.assess(errors, GOOD, None, None)[1][0] == said


def test_findings_are_capped_and_cleaned():
    errors = [f"line {n}: Uncaught \x1b[31m{'x' * 500}" for n in range(30)]
    _, found = renderfind.assess(errors, GOOD, [], False)
    assert len(found) == 10
    assert all(len(f["detail"]) <= 300 and "\x1b" not in f["detail"] for f in found)


# --- the render itself, with the browser faked --------------------------------


def test_render_report_still_unpacks_as_two_and_carries_the_check(monkeypatch):
    report = thumbs.Report(b"png", [], GOOD)
    png, errors = report
    assert (png, errors) == (b"png", []) and report.check == GOOD
    assert report == (b"png", [])


def test_a_render_with_no_browser_is_not_a_result(monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/nonexistent/browser")
    assert thumbs.render_report("<p>x</p>") == (None, None)


# --- stored, read back, and on the route -------------------------------------


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"


def test_the_route_carries_verdict_and_findings_and_keeps_the_old_fields(
    db, api, monkeypatch
):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    page = store.publish(db, "seat", "Board", PAGE)
    monkeypatch.setattr(api_module.thumbs, "available", lambda: True)
    before = api.get(f"/api/v1/pages/{page.id}/render", headers=auth).json()
    assert before == {
        "rendered": False,
        "errors": [],
        "renderer": True,
        "verdict": "unchecked",
        "findings": [],
    }
    found = [{"kind": "missing_file", "detail": "logo.png"}]
    store.save_render_errors(db, page.id, 1, [], "broken", found)
    after = api.get(f"/api/v1/pages/{page.id}/render", headers=auth).json()
    assert after["rendered"] is True and after["errors"] == []
    assert after["verdict"] == "broken" and after["findings"] == found
    row = store.render_report(db, page.id)
    assert row["verdict"] == "broken"


def test_an_old_render_without_findings_reads_as_unchecked(db):
    page = store.publish(db, "seat", "Board", PAGE)
    store.save_render_errors(db, page.id, 1, [])
    row = store.render_report(db, page.id)
    assert row["rendered"] is True and row["verdict"] == "unchecked"
    assert row["findings"] == []


def test_the_background_render_stores_the_verdict(db, monkeypatch):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    page = store.publish(
        db, "seat", "Board", PAGE.replace("<p>x</p>", '<img src="logo.png">')
    )
    html = store.get_body(db, "seat", page.id, as_reader=True).body
    monkeypatch.setattr(
        thumbs, "render_report", lambda h, *a: thumbs.Report(None, [], GOOD)
    )

    class Inline:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(thumbs.threading, "Thread", Inline)
    thumbs._failures.clear()
    thumbs.render_in_background(db, page.id, 1, html)
    row = store.render_report(db, page.id)
    assert row["verdict"] == "broken"
    assert row["findings"][0]["kind"] == "missing_file"


def test_the_tool_prints_every_finding_and_says_unchecked_not_fine(monkeypatch, capsys):
    from ream import client

    answer = {
        "rendered": True,
        "renderer": True,
        "errors": [],
        "verdict": "broken",
        "findings": [
            {"kind": "missing_file", "detail": "points to logo.png"},
            {"kind": "unchecked", "detail": "the check did not report"},
            {"kind": "script_error", "detail": "line 1: Uncaught x"},
        ],
    }
    monkeypatch.setattr(client, "_request", lambda *a, **k: answer)
    client._report_render("abc", "k")
    err = capsys.readouterr().err
    assert "missing file: points to logo.png" in err
    assert "not checked: the check did not report" in err
    assert "script_error" not in err


def test_a_preview_of_a_page_too_large_to_read_is_unchecked_never_ok(
    db, api, monkeypatch
):
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(thumbs, "available", lambda: True)
    monkeypatch.setattr(
        thumbs, "render_report", lambda *a: thumbs.Report(b"png", [], GOOD)
    )
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", 10)
    got = api.post("/api/v1/preview", json={"body": PAGE}, headers=auth).json()
    assert got["rendered"] is True and got["verdict"] == "unchecked"
    assert got["findings"] == [{"kind": "unchecked", "detail": renderfind.UNREAD_NOTE}]


def test_the_background_render_of_a_page_too_large_to_read_is_unchecked(
    db, monkeypatch
):
    monkeypatch.setenv("REAM_BROWSER", "/bin/true")
    monkeypatch.setattr(renderfind, "MAX_SCAN_CHARS", 10)
    page = store.publish(
        db, "seat", "Board", PAGE.replace("<p>x</p>", '<img src="logo.png">')
    )
    html = store.get_body(db, "seat", page.id, as_reader=True).body
    monkeypatch.setattr(
        thumbs, "render_report", lambda h, *a: thumbs.Report(None, [], GOOD)
    )

    class Inline:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(thumbs.threading, "Thread", Inline)
    thumbs._failures.clear()
    thumbs.render_in_background(db, page.id, 1, html)
    row = store.render_report(db, page.id)
    assert row["verdict"] == "unchecked"
    assert row["findings"] == [{"kind": "unchecked", "detail": renderfind.UNREAD_NOTE}]
