"""Page review step 1, Task 4: a selection offers three actions, not one, and
its anchor carries context either side (homelab-ij8.108). String-level checks
only -- the shim and the gallery script are run for real in
`tests/test_shim_js.py`/`tests/test_viewer_js.py` and their Task 6 successors."""

from artifacts import content, gallery


def test_the_selection_carries_context_either_side():
    source = content._viewer_report()
    assert "prefix:" in source and "suffix:" in source


def test_the_gallery_offers_three_actions_on_a_selection():
    js = gallery._COMMENTS_JS
    for label in ("Comment", "Remove this", "Looks good"):
        assert f'"{label}"' in js
    assert 'field("kind"' in js


def test_the_selection_bar_actually_hides():
    """Review fix, round 1: `.selbar{display:flex}` is author-origin CSS, so it
    always beats the user-agent stylesheet's `[hidden]{display:none}` --
    origin, not specificity, decides that fight. Without an explicit
    `[hidden]` rule of its own, `bar.hidden=true` is a no-op in a real
    browser and the bar never disappears; the fake-DOM node harness in
    tests/js/comments.test.mjs only reads the `hidden` property and applies
    no stylesheet at all, so it cannot catch this -- a string check here is
    the only thing that can."""
    assert ".selbar[hidden]{display:none}" in gallery._CSS
