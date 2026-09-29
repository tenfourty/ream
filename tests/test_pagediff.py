"""Tests for artifacts.pagediff: the new page with its changes marked."""

import random
import re
import time
from html.parser import HTMLParser

from artifacts.pagediff import render_changes

INS = '<ins class="ar-diff-ins">'
DEL = '<del class="ar-diff-del">'
CHIP = '<span class="ar-diff-chip">removed</span>'


def marks(doc):
    return (
        re.findall(r'<ins class="ar-diff-ins">(.*?)</ins>', doc, re.DOTALL),
        re.findall(r'<del class="ar-diff-del">(.*?)</del>', doc, re.DOTALL),
    )


class _Shape(HTMLParser):
    """Records the tag tree so two documents can be compared for shape."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.events.append(("s", tag))
        if tag not in ("img", "br", "meta", "link", "input", "hr"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        self.events.append(("e", tag))
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass


def shape(doc, drop=("ins", "del", "span")):
    p = _Shape()
    p.feed(doc)
    p.close()
    return [e for e in p.events if e[1] not in drop], p.stack


def test_changed_word_is_one_del_and_one_ins():
    doc, reason = render_changes("<p>the quick fox</p>", "<p>the slow fox</p>")
    assert reason is None
    assert marks(doc) == (["slow"], ["quick"])
    assert doc == f"<p>the {DEL}quick</del>{INS}slow</ins> fox</p>"


def test_unchanged_page_has_no_marks():
    doc, reason = render_changes("<p>same</p>", "<p>same</p>")
    assert (doc, reason) == ("<p>same</p>", None)


def test_whitespace_only_change_has_no_marks():
    old = "<p>a  b</p>\n<p>c</p>"
    new = "<p>a b</p>\n\n\n  <p>c</p>"
    doc, reason = render_changes(old, new)
    assert reason is None
    assert doc == new


def test_gt_inside_attribute_stays_one_tag():
    old = '<p title="a>b">hello</p>'
    new = '<p title="a>b">hello there</p>'
    doc, _ = render_changes(old, new)
    assert doc == f'<p title="a>b">hello {INS}there</ins></p>'
    doc, _ = render_changes('<p title="a>b">x</p>', '<p title="a>c">x</p>')
    assert marks(doc) == ([], [])
    assert doc == '<p title="a>c">x</p>'


def test_script_style_and_comments_are_opaque():
    old = "<style>a { color: red }</style><script>var x = 1 > 0;</script><!-- one --><p>t</p>"
    new = "<style>a { color: blue }</style><script>var x = 2 > 0;</script><!-- two --><p>t</p>"
    doc, reason = render_changes(old, new)
    assert reason is None
    assert marks(doc) == ([], [])
    assert doc == new


def test_authors_own_ins_and_del_pass_through():
    page = "<p>keep <ins>mine</ins> and <del>gone</del> too</p>"
    doc, _ = render_changes(page, page)
    assert doc == page
    doc, _ = render_changes(page, page.replace("too", "also"))
    assert "<ins>mine</ins>" in doc and "<del>gone</del>" in doc
    assert marks(doc) == (["also"], ["too"])


def test_removed_img_leaves_a_chip():
    doc, _ = render_changes("<p>a <img src=x.png> b</p>", "<p>a b</p>")
    assert CHIP in doc
    assert marks(doc) == ([], [])
    assert "<img" not in doc


def test_attribute_only_change_leaves_no_chip():
    doc, _ = render_changes('<p class="a">x</p>', '<p class="b">x</p>')
    assert doc == '<p class="b">x</p>'


def test_removed_table_row_with_text_is_struck_through():
    old = "<table><tr><td>one</td></tr><tr><td>two</td></tr></table>"
    new = "<table><tr><td>one</td></tr></table>"
    doc, _ = render_changes(old, new)
    assert marks(doc) == ([], ["two"])
    assert "<td>two</td>" not in doc
    assert CHIP not in doc


def test_removed_empty_row_leaves_a_chip():
    doc, _ = render_changes(
        "<table><tr><td>x</td></tr><tr></tr></table>",
        "<table><tr><td>x</td></tr></table>",
    )
    assert CHIP in doc


def test_deleted_text_sits_where_it_was_removed():
    doc, _ = render_changes("<p>a</p><p>gone</p><p>c</p>", "<p>a</p><p>c</p>")
    assert doc.index("a</p>") < doc.index("gone") < doc.index("c</p>")


def test_output_is_well_formed():
    old = "<div><h1>Title</h1><ul><li>a</li><li>b <b>bold</b></li></ul><img src=z><p>tail</p></div>"
    new = "<div><h1>New title</h1><ul><li>a</li><li>c <i>it</i></li><li>d</li></ul><p>tail!</p></div>"
    doc, _ = render_changes(old, new)
    events, stack = shape(doc)
    exp_events, exp_stack = shape(new)
    assert events == exp_events
    assert stack == exp_stack == []


def test_long_page_one_change_marks_exactly_one_word():
    words = [f"w{i % 50}" for i in range(20000)]
    old = "<p>" + " ".join(words) + "</p>"
    words2 = list(words)
    words2[10000] = "CHANGED"
    new = "<p>" + " ".join(words2) + "</p>"
    t = time.perf_counter()
    doc, reason = render_changes(old, new)
    took = time.perf_counter() - t
    assert reason is None
    assert marks(doc) == (["CHANGED"], ["w0"])
    assert took < 2


def test_long_page_with_repeated_tokens_and_scattered_edits_is_fast():
    rng = random.Random(1)
    words = [f"v{rng.randrange(400)}" for _ in range(20000)]
    old = "<p>" + " ".join(words) + "</p>"
    w2 = list(words)
    for i in (100, 9000, 19000):
        w2[i] = "DOG"
    w2[5000] = "CAT"
    new = "<p>" + " ".join(w2) + "</p>"
    t = time.perf_counter()
    doc, _ = render_changes(old, new)
    assert time.perf_counter() - t < 2
    assert sorted(marks(doc)[0]) == ["CAT", "DOG", "DOG", "DOG"]


def test_too_many_tokens_falls_back_with_reason():
    big = "<body>" + " ".join(["w"] * 31000) + "</body>"
    doc, reason = render_changes(big, big + " x")
    assert reason == "too large to mark up changes"
    assert (
        '<div class="ar-diff-banner">Showing the page as it now is: too large to mark up changes</div>'
        in doc
    )
    assert "ar-diff-ins" not in doc


def test_mostly_script_falls_back_with_reason():
    new = "<html><body><p>hi</p><script>" + "x=1;" * 500 + "</script></body></html>"
    doc, reason = render_changes("<html><body><p>hi</p></body></html>", new)
    assert reason == "this page draws itself with script"
    assert (
        '<div class="ar-diff-banner">Showing the page as it now is: this page draws itself with script</div>'
        in doc
    )


def test_banner_goes_after_body_tag():
    new = (
        '<html><body class="x"><p>hi</p><script>'
        + "x=1;" * 500
        + "</script></body></html>"
    )
    doc, _ = render_changes("<p>a</p>", new)
    m = re.search(r'<body class="x"><div class="ar-diff-banner">', doc)
    assert m, doc[:200]


def test_banner_without_body_goes_first():
    new = "<p>hi</p><script>" + "x=1;" * 500 + "</script>"
    doc, _ = render_changes("<p>a</p>", new)
    assert doc.startswith('<div class="ar-diff-banner">')


def test_degenerate_repeated_phrase_page_is_bounded():
    old = "<p>" + " ".join(["the cat sat"] * 7000) + "</p>"
    new = old.replace("cat", "dog", 3000)
    t = time.perf_counter()
    _, reason = render_changes(old, new)
    assert time.perf_counter() - t < 2
    assert reason in (None, "too large to mark up changes")
