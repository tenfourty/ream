"""The readers of a page's markup give up in bounded TIME, as `tests/test_htmlbound.py`
holds them to bounded memory: a page of 8 MiB of tags with 4000 attributes each, or of
end tags, took `forms.parse` and `sections.locate` 5 to 20 seconds, and `text.visible`
and the headings of `anchors` 3 to 6, with every tag short enough to be read as it is.
Each now draws on a `htmlbound.Budget` (`PARSE_SECONDS`) and raises `TooComplex` when
it is spent, which each module already turns into its own refusal."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ream import anchors, api, forms, htmlbound, sections, store, text

MIB = 2**20
# The budget, one tag's work after it runs out, and the machine's own noise.
BOUND = htmlbound.PARSE_SECONDS * 2 + 1


def _attributes(chars: int) -> str:
    """Tags of 4000 attributes each (under MAX_TAG_ATTRIBUTES, so each is read)."""
    unit = "<i" + " a" * 4000 + ">"
    return unit * (chars // len(unit))


def _end_tags(chars: int) -> str:
    return "</a>" * (chars // 4)


PAGES = {"4000 attributes": _attributes, "end tags": _end_tags}
GIVES_UP = (htmlbound.TooComplex, forms.FormRefused)


def _forms(page: str):
    return forms.parse("<form><input name=a></form>" + page)


# (the read, the page's length it is given, what it gives for a page it gave up on).
# The anchors' heading read stops at the page's cap: it may finish inside the budget.
READS = {
    "forms": (_forms, 8 * MIB, forms.FormRefused),
    "sections.locate": (
        lambda p: sections.locate(p, "x"),
        8 * MIB,
        sections.TooComplexError,
    ),
    "sections.locate_many": (
        lambda p: sections.locate_many(p, {"x"}),
        8 * MIB,
        sections.TooComplexError,
    ),
    "text.visible": (text.visible, text.INPUT_LIMIT, ("", True)),
    "anchors._headings": (anchors._headings, anchors.SECTION_PAGE_MAX, None),
}


def _run(read, page):
    """(seconds, what the read gave or raised)."""
    began = time.perf_counter()
    try:
        gave = read(page)
    except Exception as exc:  # noqa: BLE001 - which one is the test's to say
        gave = exc
    return time.perf_counter() - began, gave


@pytest.mark.parametrize("shape", sorted(PAGES))
@pytest.mark.parametrize("name", sorted(READS))
@pytest.mark.scaling
def test_every_reader_gives_up_on_a_page_that_would_take_it_many_seconds(name, shape):
    read, chars, gives_up = READS[name]
    page = PAGES[shape](chars)
    took, gave = _run(read, page)
    assert took < BOUND, f"{name} took {took:.1f} s on {chars} characters of {shape}"
    if gives_up is None:
        return
    if isinstance(gives_up, tuple):
        assert gave == gives_up, f"{name} gave {gave!r}"
    else:
        assert isinstance(gave, gives_up), f"{name} gave {gave!r} in {took:.1f} s"


@pytest.mark.scaling
def test_the_time_spent_is_the_budget_not_the_page():
    """What a refusal costs is the budget, whatever the page: eight times the page
    does not take eight times as long."""
    page = _end_tags(8 * MIB)
    took, gave = _run(READS["sections.locate"][0], page)
    assert isinstance(gave, sections.TooComplexError)
    assert took < BOUND
    assert took > htmlbound.PARSE_SECONDS / 2, "the page was not read at all"


# --- each module's reader draws on a Budget, with a short one so that no timing is needed

SHORT = 0.02


@pytest.fixture()
def short_budgets(monkeypatch):
    for module in (forms, sections, text, anchors):
        monkeypatch.setattr(module, "Budget", lambda: htmlbound.Budget(SHORT))


def _ordinary(chars: int) -> str:
    unit = '<div class="c"><p>some <b>text</b> &amp; more</p><a href="/x">y</a></div>\n'
    return unit * (chars // len(unit))


@pytest.mark.parametrize(
    ("name", "read", "chars", "gives_up"),
    [
        ("forms", _forms, 2 * MIB, forms.FormRefused),
        (
            "sections.locate",
            lambda p: sections.locate(p, "x"),
            2 * MIB,
            sections.TooComplexError,
        ),
        (
            "sections.locate_many",
            lambda p: sections.locate_many(p, {"x"}),
            2 * MIB,
            sections.TooComplexError,
        ),
        (
            "sections.check_balanced",
            lambda p: sections.check_balanced(p),
            2 * MIB,
            sections.TooComplexError,
        ),
        ("text.visible", text.visible, text.INPUT_LIMIT, ("", True)),
        ("anchors._headings", anchors._headings, 2 * MIB, htmlbound.TooComplex),
    ],
)
def test_each_reader_stops_when_its_budget_is_spent(
    short_budgets, name, read, chars, gives_up
):
    page = _ordinary(chars)
    began = time.perf_counter()
    try:
        gave = read(page)
    except Exception as exc:  # noqa: BLE001
        gave = exc
    took = time.perf_counter() - began
    assert took < 2.0, f"{name} went on for {took:.1f} s"
    if isinstance(gives_up, tuple):
        assert gave == gives_up
    else:
        assert isinstance(gave, gives_up), f"{name} gave {gave!r}"


def test_a_reader_with_time_left_reads_an_ordinary_page_to_the_end():
    page = _ordinary(64 * 1024)
    assert sections.locate_many(page, {"x"}) == {}
    assert text.visible(page)[1] is False
    assert anchors._headings(page + "<h2>t</h2>") is not None
    assert forms.parse(page).forms == []


def test_a_budget_starts_when_it_is_first_used_and_is_spent_for_good():
    budget = htmlbound.Budget(0.05)
    time.sleep(0.1)  # made, then waited for a scan slot: not spent
    budget.check()
    time.sleep(0.1)
    with pytest.raises(htmlbound.TooComplex):
        budget.check()
    with pytest.raises(htmlbound.TooComplex):
        budget.step()  # spent stays spent
    with pytest.raises(htmlbound.TooComplex):
        htmlbound.Budget(0).check()


def test_the_bounded_parser_steps_its_budget_on_every_token_kind():
    """An end tag, a stray "<", a comment and a declaration each cost the parser
    work, and each is counted (the parser calls `updatepos` after every one)."""

    class Counting:
        steps = 0

        def check(self):
            pass

        def step(self):
            self.steps += 1

    budget = Counting()
    parser = htmlbound.BoundedParser(meter=budget)
    parser.feed("</a></a>< <!-- c --><!x><?p>text<b>")
    parser.close()
    assert budget.steps >= 6, budget.steps


# --- the callers that read a whole body with no size check of their own

HUGE = 8 * MIB


@pytest.mark.scaling
def test_an_element_anchor_on_a_page_it_cannot_read_in_time_is_a_refusal():
    """`api._element_anchor` locates the element in the whole live body: past the
    budget it is the 422 of every section refusal, and not a 500 or a minute."""
    page = _end_tags(HUGE)
    live = SimpleNamespace(body=page)
    saved = store.get_body
    store.get_body = lambda *a, **k: live  # type: ignore[assignment]
    try:
        began = time.perf_counter()
        with pytest.raises(HTTPException) as refused:
            api._element_anchor(None, "aid", {"kind": "element", "id": "x"}, "agent")
        took = time.perf_counter() - began
    finally:
        store.get_body = saved  # type: ignore[assignment]
    assert refused.value.status_code == 422
    assert refused.value.detail["field"] == "anchor"
    assert took < BOUND, f"{took:.1f} s"


@pytest.mark.scaling
def test_the_page_index_gives_up_on_a_page_it_cannot_locate_ids_in_in_time():
    """`anchors.PageIndex.id_span` (one pass for the ids a listing announced, or one
    for an id it did not): `TooComplex` in bounded time, which the listing treats as
    an anchor it cannot place."""
    page = _end_tags(HUGE)
    for ids in ({"x"}, set()):
        index = anchors.PageIndex(page, ids)
        began = time.perf_counter()
        with pytest.raises(htmlbound.TooComplex):
            index.id_span("x")
        assert time.perf_counter() - began < BOUND
