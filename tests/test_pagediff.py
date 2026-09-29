"""Tests for artifacts.pagediff: the new page with its changes marked."""

import random
import re
import time
from html.parser import HTMLParser

from artifacts.pagediff import render_changes

INS = '<ins class="ar-diff-ins">'
DEL = '<del class="ar-diff-del">'
CHIP = '<span class="ar-diff-chip">removed</span>'
CHANGED = '<span class="ar-diff-chip">changed</span>'


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
    assert doc == f'<p title="a>c">{CHANGED}x</p>'


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


def test_attribute_only_change_leaves_a_changed_chip():
    doc, _ = render_changes('<p class="a">x</p>', '<p class="b">x</p>')
    assert doc == f'<p class="b">{CHANGED}x</p>'


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


# --- review fixes -------------------------------------------------------------

OURS = {"ins", "del"}


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, tuple(attrs)))

    handle_startendtag = handle_starttag


def start_tags(doc):
    p = _Tags()
    p.feed(doc)
    p.close()
    return p.tags


def foreign_tags(doc, new):
    """Start tags in ``doc`` that the new page does not have, bar our own marks."""
    allowed = set(start_tags(new))
    return [
        t
        for t in start_tags(doc)
        if t not in allowed
        and t[0] not in OURS
        and not (t[0] == "span" and t[1] == (("class", "ar-diff-chip"),))
    ]


def test_dangling_tag_in_old_page_is_not_revived():
    old = '<p>keep</p><img src=x onerror=alert(1)// title="'
    new = '<p>keep</p><p class="z">b</p>'
    doc, _ = render_changes(old, new)
    assert not [t for t in start_tags(doc) if t[0] == "img"], doc
    assert foreign_tags(doc, new) == []


def test_deleted_text_is_escaped_and_entities_survive():
    doc, _ = render_changes("<p>a &amp; 1 < 2 b</p>", "<p>b</p>")
    assert "&amp;" in doc and "&amp;amp;" not in doc
    assert "< 2" not in doc and "&lt;" in doc


def test_fuzzed_pairs_never_create_a_tag_the_new_page_lacks():
    frags = [
        "<img src=x onerror=",
        'title="',
        "<scr",
        "ipt>",
        '"',
        ">",
        "<!--",
        "-->",
        "<p>",
        "</p>",
        "word",
        "alert(1)",
        " ",
        "<",
        "'",
        "<b class=",
        "</b>",
        "<table><tr><td>",
        "</td></tr></table>",
    ]
    rng = random.Random(7)
    for _ in range(400):
        old = "".join(rng.choice(frags) for _ in range(rng.randrange(2, 14)))
        new = "".join(rng.choice(frags) for _ in range(rng.randrange(2, 14)))
        doc, _ = render_changes(old, new)
        assert foreign_tags(doc, new) == [], (old, new, doc)


def _fast(old, new, limit=1.0):
    t = time.perf_counter()
    render_changes(old, new)
    took = time.perf_counter() - t
    assert took < limit, took
    return took


def test_unclosed_constructs_tokenize_in_linear_time():
    for unit in ("<a ", "<script ", "<!-- ", "<![CDATA[ ", "<title ", '<p title="'):
        blob = unit * 20000
        _fast(blob, blob + "x")
        _fast("<p>a</p>", blob)


def test_input_over_the_page_cap_falls_back():
    big = "<body>" + "x" * (3 * 1024 * 1024 + 1) + "</body>"
    t = time.perf_counter()
    _, reason = render_changes("<p>a</p>", big)
    assert time.perf_counter() - t < 1
    assert reason == "too large to mark up changes"


def test_recursive_matching_is_budgeted():
    words = [f"w{i % 230}" for i in range(29900)]
    old = "<p>" + " ".join(words) + "</p>"
    new = "<p>" + " ".join(reversed(words)) + "</p>"
    t = time.perf_counter()
    _, reason = render_changes(old, new)
    assert time.perf_counter() - t < 3
    assert reason == "too large to mark up changes"


def test_title_and_textarea_are_never_marked_inside():
    old = "<title>Old name</title><textarea>one two</textarea><p>x</p>"
    new = "<title>New name</title><textarea>one three</textarea><p>x</p>"
    doc, _ = render_changes(old, new)
    for tag in ("title", "textarea"):
        inner = re.search(rf"<{tag}>(.*?)</{tag}>", doc, re.DOTALL).group(1)
        assert "<ins" not in inner and "<del" not in inner, doc
    assert doc == new


def test_other_raw_text_elements_and_cdata_are_opaque():
    for open_, close in (
        ("<xmp>", "</xmp>"),
        ("<noembed>", "</noembed>"),
        ("<noframes>", "</noframes>"),
        ("<iframe>", "</iframe>"),
        ("<![CDATA[", "]]>"),
    ):
        old = f"<p>x</p>{open_}alpha beta{close}"
        new = f"<p>x</p>{open_}alpha gamma{close}"
        doc, _ = render_changes(old, new)
        assert doc == new, doc
    doc, _ = render_changes("<plaintext>a b", "<plaintext>a c")
    assert doc == "<plaintext>a c"


def _before_bad_neighbour(doc):
    pat = r'(</del>|<span class="ar-diff-chip">removed</span>)\s*(</table>|<tr\b|</tr>|</select>|<tbody\b|</tbody>)'
    return re.search(pat, doc)


def test_deleted_row_lands_inside_a_cell():
    old = "<table><tr><td>a</td></tr><tr><td>gone</td></tr></table>"
    new = "<table><tr><td>a</td></tr></table>"
    doc, _ = render_changes(old, new)
    assert not _before_bad_neighbour(doc), doc
    assert f"<td>a{DEL}gone</del></td>" in doc


def test_deleted_middle_row_lands_in_the_previous_cell():
    old = "<table><tr><td>a</td></tr><tr><td>gone</td></tr><tr><td>c</td></tr></table>"
    new = "<table><tr><td>a</td></tr><tr><td>c</td></tr></table>"
    doc, _ = render_changes(old, new)
    assert not _before_bad_neighbour(doc), doc
    assert marks(doc) == ([], ["gone"])


def test_deletion_in_table_with_no_earlier_cell_is_a_chip_outside():
    old = "<p>x</p><table><tr><td>gone</td></tr></table>"
    new = "<p>x</p><table></table>"
    doc, _ = render_changes(old, new)
    assert not _before_bad_neighbour(doc), doc
    assert doc == f"<p>x</p>{CHIP}<table></table>"


def test_nothing_is_appended_inside_an_unterminated_last_token():
    for tail in ("<b class=", '<a title="x>', "<!-- open", "<script>a>b", "<title>t"):
        new = "<p>keep</p><p>two</p>" + tail
        doc, _ = render_changes("<p>keep</p><p>one</p><p>more words</p>", new)
        assert doc.endswith(tail), doc
        doc, _ = render_changes('<p>keep</p><b class="q">', "<p>keep</p>" + tail)
        assert doc.endswith(tail), doc


def test_select_never_gets_marks_inside():
    old = "<select><option>Red</option><option>Blue</option></select>"
    new = "<select><option>Red</option><option>Green</option></select>"
    doc, _ = render_changes(old, new)
    inner = doc[doc.index("<select>") : doc.index("</select>")]
    assert "<ins" not in inner and "<del" not in inner and "ar-diff" not in inner
    assert not _before_bad_neighbour(doc), doc


def test_deletion_directly_in_a_list_moves_into_the_previous_item():
    old = "<ul><li>a</li><li>gone</li></ul>"
    new = "<ul><li>a</li></ul>"
    doc, _ = render_changes(old, new)
    assert f"<li>a{DEL}gone</del></li>" in doc


def test_href_change_leaves_a_changed_chip():
    old = '<p><a href="x">go</a></p>'
    new = '<p><a href="javascript:alert(1)">go</a></p>'
    doc, _ = render_changes(old, new)
    assert doc == f'<p><a href="javascript:alert(1)">{CHANGED}go</a></p>'


def test_void_tag_change_puts_the_chip_after_it():
    doc, _ = render_changes(
        '<p>a <img src="1.png"> b</p>', '<p>a <img src="2.png"> b</p>'
    )
    assert doc == f'<p>a <img src="2.png">{CHANGED} b</p>'


def test_whitespace_only_attribute_change_has_no_chip():
    doc, _ = render_changes('<p  class="a">x</p>', '<p class="a">x</p>')
    assert doc == '<p class="a">x</p>'


def test_deleted_text_in_pre_keeps_its_whitespace():
    old = "<pre>keep\n  line one\n    line two</pre>"
    new = "<pre>keep</pre>"
    doc, _ = render_changes(old, new)
    assert marks(doc)[1] == ["line one\n    line two"]


def test_empty_comment_forms_do_not_swallow_the_page():
    for empty in ("<!-->", "<!--->"):
        old = f"<p>a</p>{empty}<p>one</p><!-- x -->"
        new = f"<p>a</p>{empty}<p>two</p><!-- x -->"
        doc, _ = render_changes(old, new)
        assert marks(doc) == (["two"], ["one"]), doc
