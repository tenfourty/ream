"""Where a pinned comment is now, read from its evidence (pinned comments,
slice 1: docs/agent/specs/2026-10-29-pinned-comments.md). Pure: a pin and a
page body in, a state out, no database.

The inputs are the shapes the gallery and an agent really send: a `range`
anchor from the browser (path, offsets, prefix, suffix, heading, ids) and the
`quote` anchor the server stores for an agent.
"""

import pytest

from artifacts import anchors

PAGE = (
    "<!doctype html><html><body>"
    '<h2 id="costs">Costs</h2>'
    "<p>Cost per seat is flat across plans.</p>"
    '<table id="plans"><tr><td>Pro plan is cheap.</td></tr></table>'
    "<h2>Hosting</h2>"
    "<p>Hosting is billed monthly in advance.</p>"
    "</body></html>"
)
QUOTE = "Cost per seat is flat across plans"


def _pin(**extra):
    return {
        "kind": "range",
        "path": "body>p:nth-of-type(1)",
        "start": 0,
        "end": 34,
        **extra,
    }


# --- the states ---------------------------------------------------------------


def test_a_quote_still_where_it_was_is_current():
    assert anchors.resolve(_pin(heading="Costs"), QUOTE, PAGE) == ("current", "")


def test_a_pin_with_no_quote_and_no_id_says_nothing():
    assert anchors.resolve(_pin(), "", PAGE) == ("", "")


def test_a_quote_edited_in_place_is_edited_not_outdated():
    body = PAGE.replace("is flat across plans", "is flat for every plan")
    pin = _pin(prefix="Cost per seat is ")  # text before the quote survives the edit
    assert anchors.resolve(pin, QUOTE, body) == ("edited", "")


def test_half_of_the_quote_surviving_reads_as_edited():
    body = PAGE.replace("across plans", "on every tier")
    assert anchors.resolve(_pin(), QUOTE, body)[0] == "edited"


def test_a_quote_that_is_gone_is_outdated():
    body = PAGE.replace("<p>Cost per seat is flat across plans.</p>", "")
    assert anchors.resolve(_pin(heading="Costs"), QUOTE, body) == ("outdated", "")


def test_emphasis_added_inside_the_quote_does_not_change_it():
    body = PAGE.replace("flat", "<em>flat</em>")
    assert anchors.resolve(_pin(), QUOTE, body) == ("current", "")


def test_a_page_with_more_text_than_is_searched_is_unknown(monkeypatch):
    monkeypatch.setattr(anchors.text, "visible", lambda body, limit=None: ("x", True))
    assert anchors.resolve(_pin(), QUOTE, PAGE) == ("", "")


# --- the order of evidence ----------------------------------------------------


def test_the_id_is_tried_first_and_a_changed_text_under_it_is_edited():
    body = PAGE.replace("Pro plan is cheap.", "Pro plan costs more now.")
    pin = _pin(el_id="plans")
    assert anchors.resolve(pin, "Pro plan is cheap.", body) == ("edited", "")


def test_the_id_holding_the_quote_is_current():
    assert anchors.resolve(_pin(el_id="plans"), "Pro plan is cheap.", PAGE) == (
        "current",
        "",
    )


def test_a_quote_moved_to_another_section_is_current_and_says_so():
    body = PAGE.replace("<p>Cost per seat is flat across plans.</p>", "").replace(
        "<p>Hosting is billed",
        "<p>Cost per seat is flat across plans.</p><p>Hosting is billed",
    )
    state, note = anchors.resolve(
        _pin(heading="Costs", heading_id="costs"), QUOTE, body
    )
    assert state == "current"
    assert note == 'moved: no longer under "Costs"'


def test_a_heading_found_by_its_id_survives_a_rename_of_its_text():
    body = PAGE.replace(">Costs<", ">What it costs<")
    pin = _pin(heading="Costs", heading_id="costs")
    assert anchors.resolve(pin, QUOTE, body) == ("current", "")


def test_a_vanished_id_falls_through_to_the_quote():
    body = PAGE.replace('id="plans"', "")
    assert anchors.resolve(_pin(el_id="plans"), "Pro plan is cheap.", body) == (
        "current",
        "",
    )


def test_a_repeated_id_is_not_trusted_and_falls_through_to_the_quote():
    body = PAGE.replace("<h2>Hosting</h2>", '<h2 id="plans">Hosting</h2>')
    assert anchors.resolve(_pin(el_id="plans"), "Pro plan is cheap.", body) == (
        "current",
        "",
    )


def test_an_id_alone_that_vanished_is_outdated():
    body = PAGE.replace('id="plans"', "")
    assert anchors.resolve({"kind": "element", "id": "plans"}, "", body) == (
        "outdated",
        "",
    )


def test_a_quote_never_moves_a_pin_onto_other_text():
    """Same heading, different words: the pin does not follow the heading."""
    body = PAGE.replace("Cost per seat is flat across plans.", "Seats are free.")
    assert anchors.resolve(_pin(heading="Costs"), QUOTE, body)[0] == "outdated"


def test_a_quote_anchor_an_agent_made_is_resolved_like_a_browser_one():
    pin = {"kind": "quote", "heading": "Hosting"}
    assert anchors.resolve(pin, "Hosting is billed monthly", PAGE) == ("current", "")
    gone = PAGE.replace("Hosting is billed monthly in advance.", "")
    assert anchors.resolve(pin, "Hosting is billed monthly", gone)[0] == "outdated"


def test_resolving_from_words_already_read_gives_the_same_answer():
    words, _ = anchors.text.visible(PAGE)
    got = anchors.resolve(_pin(), QUOTE, "", facts=(words, anchors._tight(words)))
    assert got == anchors.resolve(_pin(), QUOTE, PAGE)


# --- the heading a quote sits under -------------------------------------------


def test_the_nearest_heading_and_its_id_are_found():
    assert anchors.heading_for_quote(PAGE, QUOTE) == ("Costs", "costs")
    assert anchors.heading_for_quote(PAGE, "Hosting is billed monthly") == (
        "Hosting",
        "",
    )


def test_a_quote_above_every_heading_has_no_heading():
    body = "<p>Intro words here.</p>" + PAGE
    assert anchors.heading_for_quote(body, "Intro words here") == ("", "")


def test_a_named_section_is_the_heading_when_none_is_found_alone():
    body = PAGE + "<h2>Costs</h2><p>Cost per seat is flat across plans.</p>"
    assert anchors.heading_for_quote(body, QUOTE, "Costs")[0] == "Costs"
    assert anchors.heading_for_quote(body, QUOTE) == ("", "")


@pytest.mark.parametrize("state", anchors.STATES)
def test_the_states_are_the_three_named_ones(state):
    assert state in ("current", "edited", "outdated")


# --- one parse per page, however many pins ------------------------------------


def test_many_pins_on_one_page_parse_its_body_once(monkeypatch):
    calls = {"headings": 0, "locate": 0}
    real_headings, real_locate = anchors._headings, anchors.sections.locate_many

    def headings(body):
        calls["headings"] += 1
        return real_headings(body)

    def locate_many(body, ids):
        calls["locate"] += 1
        return real_locate(body, ids)

    monkeypatch.setattr(anchors, "_headings", headings)
    monkeypatch.setattr(anchors.sections, "locate_many", locate_many)
    index = anchors.PageIndex(PAGE, {"plans", "costs"})
    pins = [
        _pin(heading="Costs", heading_id="costs"),
        _pin(heading="Hosting"),
        _pin(el_id="plans"),
        _pin(el_id="plans", heading="Costs"),
        _pin(heading_id="costs"),
    ] * 20
    got = {anchors.resolve(p, QUOTE, index)[0] for p in pins}
    assert got <= set(anchors.STATES)
    assert calls == {"headings": 1, "locate": 1}


def test_an_index_answers_as_the_plain_body_does():
    for pin in (
        _pin(heading="Costs"),
        _pin(heading="Hosting"),
        _pin(el_id="plans"),
        _pin(heading_id="costs"),
    ):
        index = anchors.PageIndex(PAGE, {"plans"})
        assert anchors.resolve(pin, QUOTE, index) == anchors.resolve(pin, QUOTE, PAGE)


def test_an_id_longer_than_sections_address_is_refused():
    assert anchors.ID_RE.fullmatch("x" * 128)
    assert not anchors.ID_RE.fullmatch("x" * 129)


# --- an edited quote found approximately, inside its own section ---------------

SENTENCE = "Cost per seat is flat across all of the plans we offer."
FUZZ_PAGE = (
    "<html><body><h2>Costs</h2><p>" + SENTENCE + "</p>"
    "<h2>Hosting</h2><p>Hosting is billed monthly in advance, per cluster.</p>"
    "</body></html>"
)


def _fz(body, quote=SENTENCE, heading="Costs", **extra):
    pin = {"kind": "quote", "heading": heading, **extra}
    return anchors.resolve(pin, quote, body), anchors.current_quote(pin, quote, body)


def test_a_word_changed_in_the_quote_is_edited_and_shows_what_stands_there():
    body = FUZZ_PAGE.replace("flat", "fixed")
    got, now = _fz(body)
    assert got == ("edited", "")
    assert now == "Cost per seat is fixed across all of the plans we offer."


def test_a_slightly_reworded_sentence_is_edited_not_current():
    body = FUZZ_PAGE.replace(
        "across all of the plans we offer", "across every plan we offer"
    )
    got, now = _fz(body)
    assert got[0] == "edited"
    assert "every plan" in now


def test_a_quote_reworded_into_another_section_is_not_re_attached():
    reworded = "Rates per seat are fixed across every one of the plans we sell."
    body = FUZZ_PAGE.replace(SENTENCE, "").replace(
        "per cluster.", "per cluster. " + reworded
    )
    assert (
        anchors.resolve({"kind": "quote", "heading": "Costs"}, reworded, body)[0]
        == "current"
    )
    got, now = _fz(body, quote="Seat prices are fixed over each of the plans we sell.")
    assert got[0] == "outdated"
    assert now == ""


def test_a_very_short_quote_is_never_matched_approximately():
    body = FUZZ_PAGE.replace("Cost per seat is flat", "Cost per seats are flat")
    got, now = _fz(body, quote="Cost per seat")  # still a substring: current
    assert got[0] == "current"
    body = FUZZ_PAGE.replace(SENTENCE, "Cost per sea is.")
    got, now = _fz(body, quote="Cost per seas is")
    assert got[0] == "outdated" and now == ""


def test_a_quote_that_is_gone_from_its_section_is_outdated():
    body = FUZZ_PAGE.replace(SENTENCE, "Nothing here about pricing at all, sorry.")
    assert _fz(body) == (("outdated", ""), "")


def test_a_pin_with_no_heading_has_no_section_to_search():
    index = anchors.PageIndex(FUZZ_PAGE.replace("flat", "fixed"))
    assert index.fuzzy({"kind": "quote"}, SENTENCE) == ""
    assert index.fuzzy({"kind": "quote", "heading": "Costs"}, SENTENCE) != ""


def test_a_hostile_section_is_searched_in_bounded_time():
    import time

    hostile = "<html><body><h2>Costs</h2><p>" + "a b " * 60_000 + "</p></body></html>"
    quote = "alpha beta gamma delta epsilon zeta eta theta " * 4
    began = time.monotonic()
    got, _ = _fz(hostile, quote=quote.strip())
    assert time.monotonic() - began < 1.0
    assert got[0] == "outdated"
    spaced = "<html><body><h2>Costs</h2><p>" + "x " * 100_000 + "</p></body></html>"
    began = time.monotonic()
    _fz(spaced, quote="x " * 100 + "y")
    assert time.monotonic() - began < 1.0


def test_a_listing_over_many_pages_shares_one_budget_and_a_repeat_is_cached():
    import time

    anchors._FUZZY_CACHE.clear()
    pin = {"kind": "quote", "heading": "Costs"}
    quote = "x " * 100 + "y"

    def listing():
        budget = anchors.FuzzyBudget()
        began = time.monotonic()
        for n in range(60):
            body = f"<h2>Costs</h2><p>{'x ' * (1000 + n)}</p>"
            index = anchors.PageIndex(body, budget=budget)
            anchors.resolve(pin, quote, index)
            anchors.current_quote(pin, quote, index)
        return time.monotonic() - began

    assert listing() < 0.6
    assert listing() < 0.6  # a search cut short is never cached as "no match"


def test_a_finished_search_is_served_from_the_cache_next_time(monkeypatch):
    anchors._FUZZY_CACHE.clear()
    body = FUZZ_PAGE.replace("flat", "fixed")
    pin = {"kind": "quote", "heading": "Costs"}
    first = anchors.PageIndex(body).fuzzy(pin, SENTENCE)
    assert first and len(anchors._FUZZY_CACHE) == 1

    def refuse(*a, **k):
        raise AssertionError("searched again")

    monkeypatch.setattr(anchors, "_fuzzy_text", refuse)
    assert anchors.PageIndex(body).fuzzy(pin, SENTENCE) == first


def test_the_cache_is_bounded(monkeypatch):
    anchors._FUZZY_CACHE.clear()
    monkeypatch.setattr(anchors, "FUZZY_CACHE_MAX", 3)
    for n in range(10):
        index = anchors.PageIndex(f"<h2>Costs</h2><p>{SENTENCE} {n}</p>")
        index.fuzzy({"heading": "Costs"}, SENTENCE + " tail")
    assert len(anchors._FUZZY_CACHE) <= 3
