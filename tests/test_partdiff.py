"""Which parts of a page changed between two versions (runcove-5t3oj.56, slice 2)."""

import time

import pytest

from ream import pagediff, partdiff


def page(*sections: str, head: str = "") -> str:
    return f"<!doctype html><html><head><title>t</title>{head}</head><body>{''.join(sections)}</body></html>"


def sec(id_: str, heading: str, text: str) -> str:
    return f'<section id="{id_}"><h2>{heading}</h2><p>{text}</p></section>'


def parts(old: str, new: str) -> list[tuple]:
    got = partdiff.compare(old, new)
    assert got["unknown_reason"] is None, got
    return [(p.get("id"), p["label"], p["change"]) for p in got["parts"]]


def test_a_part_with_an_id_is_matched_by_its_id_across_a_heading_rename():
    old = page(sec("running-now", "Running now", "a"), sec("results", "Results", "r"))
    new = page(sec("running-now", "In flight", "a"), sec("results", "Results", "r"))
    assert parts(old, new) == [("running-now", "In flight", "changed")]


def test_headings_match_by_text_when_there_is_no_id():
    old = page("<h2>Plan</h2><p>one</p><h2>Risks</h2><p>x</p><h2>Old</h2><p>gone</p>")
    new = page("<h2>Plan</h2><p>two</p><h2>Risks</h2><p>x</p><h2>New</h2><p>fresh</p>")
    assert parts(old, new) == [
        (None, "Plan", "changed"),
        (None, "New", "added"),
        (None, "Old", "removed"),
    ]


def test_duplicate_headings_each_carry_their_position():
    old = page("<h2>Notes</h2><p>a</p><h2>Notes</h2><p>b</p>")
    new = page("<h2>Notes</h2><p>a</p><h2>Notes</h2><p>c</p>")
    got = partdiff.compare(old, new)["parts"]
    assert [(p["label"], p["change"]) for p in got] == [("Notes", "changed")]
    assert len(got) == 1


def test_text_before_the_first_heading_is_the_top_of_the_page():
    old = page("<p>intro</p><h2>A</h2><p>x</p>")
    new = page("<p>intro, longer</p><h2>A</h2><p>x</p>")
    assert parts(old, new) == [(None, "Top of the page", "changed")]


def test_a_change_with_no_visible_text_is_not_a_changed_part():
    old = page('<h2 class="a">A</h2><p style="color:red">x</p>')
    new = page('<h2 class="b">A</h2><p style="color:blue">x</p>')
    got = partdiff.compare(old, new)
    assert got == {"parts": [], "more": 0, "unknown_reason": None}


def test_what_a_reader_sees_in_an_attribute_counts():
    old = page('<h2>A</h2><img src="a.png" alt="one"><a href="/x">go</a>')
    assert parts(old, old.replace("a.png", "b.png")) == [(None, "A", "changed")]
    assert parts(old, old.replace('alt="one"', 'alt="two"')) == [(None, "A", "changed")]
    assert parts(old, old.replace('"/x"', '"/y"')) == [(None, "A", "changed")]


def test_script_and_style_contents_are_left_out():
    old = page("<h2>A</h2><p>x</p><script>var a=1</script><style>p{}</style>")
    new = page("<h2>A</h2><p>x</p><script>var a=2</script><style>p{color:red}</style>")
    assert partdiff.compare(old, new)["parts"] == []


def test_a_page_drawn_by_its_script_says_so_never_an_empty_list():
    drawn = page("<div id=app></div><script>" + "x=1;" * 5000 + "</script>")
    got = partdiff.compare(drawn, drawn.replace("x=1", "x=2"))
    assert got["unknown_reason"] == pagediff.REASON_SCRIPT
    assert got["parts"] == []


def test_an_oversized_page_says_so():
    big = page("<h2>A</h2><p>" + "x" * (pagediff.MAX_BYTES + 1) + "</p>")
    got = partdiff.compare(big, big)
    assert got["unknown_reason"] == pagediff.REASON_LARGE and got["parts"] == []


def test_the_list_and_the_labels_are_capped():
    many_old = page("".join(f"<h2>H{n}</h2><p>a</p>" for n in range(60)))
    many_new = many_old.replace("<p>a</p>", "<p>b</p>")
    got = partdiff.compare(many_old, many_new)
    assert len(got["parts"]) == 50 and got["more"] == 10
    long = page("<h2>" + "w" * 200 + "</h2><p>a</p>")
    label = partdiff.compare(long, long.replace("<p>a", "<p>b"))["parts"][0]["label"]
    assert len(label) <= 80


def test_labels_are_clean_text():
    old = page('<section id="s"><h2>Fine</h2><p>a</p></section>')
    new = page(
        '<section id="s"><h2>&lt;img onerror=x&gt; \x1b[31mred</h2><p>a</p></section>'
    )
    label = partdiff.compare(old, new)["parts"][0]["label"]
    assert "\x1b" not in label
    assert label.startswith("<img onerror=x>")  # text, escaped when shown, never markup


def test_an_aria_label_or_the_id_names_a_part_with_no_heading():
    old = page('<div id="chart" aria-label="Sales chart"><p>1</p></div>')
    new = page('<div id="chart" aria-label="Sales chart"><p>2</p></div>')
    assert parts(old, new) == [("chart", "Sales chart", "changed")]


def test_identical_pages_have_no_changes():
    one = page(sec("a", "A", "x"))
    assert partdiff.compare(one, one)["parts"] == []


def test_a_span_is_one_diff_between_its_ends():
    v1 = page(sec("a", "A", "x"))
    v2 = page(sec("a", "A", "x"), sec("b", "B", "temp"))
    v3 = page(sec("a", "A", "x"))
    assert partdiff.compare(v1, v2)["parts"][0]["change"] == "added"
    assert partdiff.compare(v1, v3)["parts"] == []


def test_the_safe_entry_gives_the_same_answer_through_the_worker():
    old = page(sec("a", "A", "x"))
    new = page(sec("a", "A", "y"))
    partdiff._CACHE.clear()
    assert partdiff.changes_safely(old, new) == partdiff.compare(old, new)
    assert len(partdiff._CACHE) == 1  # a finished answer is kept
    assert partdiff.changes_safely(old, new) == partdiff.compare(old, new)  # cached


def _sleepy_compare(old, new):
    time.sleep(10)
    return partdiff.compare(old, new)


def test_a_diff_that_timed_out_is_answered_but_not_cached():
    """A pair that only ran out of time (a busy machine, a cold worker) is asked for
    again on the next call; one that finished is kept."""
    old = page(sec("a", "A", "x"))
    new = page(sec("a", "A", "z"))
    partdiff._CACHE.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(partdiff, "compare", _sleepy_compare)
        slow = partdiff.changes_safely(old, new, timeout=0.5)
    assert slow == {"parts": [], "more": 0, "unknown_reason": partdiff.REASON_SLOW}
    assert len(partdiff._CACHE) == 0
    real = partdiff.changes_safely(old, new)
    assert real == partdiff.compare(old, new)
    assert real["unknown_reason"] is None
    assert len(partdiff._CACHE) == 1
