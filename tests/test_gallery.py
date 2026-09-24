import re

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

XSS = '"><img src=x onerror=alert(1)>'

_PAGE = "<!doctype html><html><head></head><body>x</body></html>"

_OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


def _publish_and_ask(db, question: str = "ship it?", options=None) -> tuple:
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question=question,
        options=options or _OPTIONS,
    )
    return published, row


def test_the_list_escapes_a_hostile_title(db):
    store.publish(
        db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>"
    )
    page = (
        TestClient(create_gallery_app(db))
        .get("/", headers={"Remote-User": "jeremy"})
        .text
    )
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page or "&#" in page


def test_the_detail_page_frames_the_content_origin_and_inlines_nothing(db):
    published = store.publish(
        db,
        "ops-seat",
        "Board",
        "<!doctype html><html><head></head><body>secret-body</body></html>",
    )
    page = (
        TestClient(create_gallery_app(db))
        .get(f"/a/{published.id}", headers={"Remote-User": "jeremy"})
        .text
    )
    # A fresh single-use pass, never the page's permanent token (homelab-ij8.101).
    assert re.search(
        rf'src="https://{published.id}\.[^/"]+/p/[A-Za-z0-9_-]{{43}}/"', page
    )
    assert store.read_token(db, published.id) not in page
    assert "secret-body" not in page


@pytest.mark.parametrize("which", ["list", "detail"])
def test_the_gallery_sets_its_own_strict_headers(db, which):
    """Both routes must carry the same strict header set -- `/a/{id}` is the one page that
    ever holds the capability token, so it is checked exactly as strictly as `/`, not on the
    strength of `/` alone."""
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    path = "/" if which == "list" else f"/a/{published.id}"
    response = TestClient(create_gallery_app(db)).get(
        path, headers={"Remote-User": "jeremy"}
    )
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "frame-src https://*.tenfourty.com" in csp
    assert "frame-src https:;" not in csp
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"


def test_an_unknown_artifact_gets_a_404_not_a_frame(db):
    """An unchecked id renders a frame pointing at an invented subdomain."""
    response = TestClient(create_gallery_app(db)).get(
        "/a/aaaaaaaaaaaaaaaaaaaaaaaaaa", headers={"Remote-User": "jeremy"}
    )
    assert response.status_code == 404
    assert "<iframe" not in response.text


def test_the_detail_page_escapes_the_title_it_now_shows(db):
    published = store.publish(
        db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>"
    )
    page = (
        TestClient(create_gallery_app(db))
        .get(f"/a/{published.id}", headers={"Remote-User": "jeremy"})
        .text
    )
    assert "<img src=x" not in page


def test_the_detail_iframe_is_sandboxed_against_navigation_and_forms(db):
    """No allow-top-navigation*, no allow-forms, no allow-modals: an agent's page must never
    be able to navigate Jeremy's signed-in gallery tab, e.g. to a fake sign-in page. Asserting
    set equality (not just "contains") means adding a token back later fails this test too."""
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    page = (
        TestClient(create_gallery_app(db))
        .get(f"/a/{published.id}", headers={"Remote-User": "jeremy"})
        .text
    )
    match = re.search(r'<iframe[^>]*\bsandbox="([^"]*)"', page)
    assert match is not None, "detail page has no sandboxed iframe"
    assert set(match.group(1).split()) == {
        "allow-scripts",
        "allow-same-origin",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
    }


def _ids_in_order(page: str) -> list[str]:
    return re.findall(r'href="/a/([a-z2-7]{26})"', page)


def test_the_list_is_capped_at_two_hundred_rows(db):
    for i in range(201):
        store.publish(db, "ops-seat", f"page {i}", _PAGE)
    page = (
        TestClient(create_gallery_app(db))
        .get("/", headers={"Remote-User": "jeremy"})
        .text
    )
    assert len(_ids_in_order(page)) == 200


def test_the_list_orders_newest_first(db):
    older = store.publish(db, "ops-seat", "older", _PAGE)
    with db.connect() as conn:
        conn.execute(
            "update artifact set updated_at = now() - interval '1 hour' where id = %s",
            (older.id,),
        )
    newer = store.publish(db, "ops-seat", "newer", _PAGE)
    page = (
        TestClient(create_gallery_app(db))
        .get("/", headers={"Remote-User": "jeremy"})
        .text
    )
    order = _ids_in_order(page)
    assert order.index(newer.id) < order.index(older.id)


def test_the_list_includes_every_agents_pages(db):
    """The gallery is Jeremy's view of everything -- unlike the agent API, its query carries
    no `where agent = ...`."""
    one = store.publish(db, "ops-seat", "one", _PAGE)
    two = store.publish(db, "other-seat", "two", _PAGE)
    page = (
        TestClient(create_gallery_app(db))
        .get("/", headers={"Remote-User": "jeremy"})
        .text
    )
    ids = _ids_in_order(page)
    assert one.id in ids
    assert two.id in ids


def test_the_list_breaks_updated_at_ties_by_id(db):
    """Every row with the identical updated_at must still come back in one fixed order, not
    whatever a `select` with no full ORDER BY happens to return today. Ten rows, not two: with
    only two, insertion order coincidentally matches id-desc about half the time (measured: a
    mutant that removes `, id desc` was only caught 4 times in 12 runs), so a two-row sample
    gives almost no assurance the ORDER BY clause is doing anything. Ten rows landing in
    id-desc order by chance alone is astronomically unlikely."""
    ids = [store.publish(db, "ops-seat", f"page {i}", _PAGE).id for i in range(10)]
    with db.connect() as conn:
        conn.execute(
            "update artifact set updated_at = '2026-01-01T00:00:00Z' where id = any(%s)",
            (ids,),
        )
    page = (
        TestClient(create_gallery_app(db))
        .get("/", headers={"Remote-User": "jeremy"})
        .text
    )
    order = [i for i in _ids_in_order(page) if i in ids]
    assert order == sorted(ids, reverse=True)


def test_store_module_docstring_is_encodable():
    """A literal `\\ud800` escape in a non-raw docstring becomes a real lone surrogate in
    `__doc__`; harmless import on 3.12, but a bare `import artifacts.store` crashes on 3.14."""
    import artifacts.store as store_module

    store_module.__doc__.encode("utf-8")


# ---------------------------------------------------------------------------
# Decisions: the one place on the detail page Jeremy can act, not just look.
# ---------------------------------------------------------------------------


def test_the_detail_page_renders_a_form_per_decision(db):
    published, row = _publish_and_ask(db)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert f'action="/d/{row.id}/answer"' in page
    assert "(a) yes" in page
    assert "(b) no" in page


def test_the_decisions_block_marks_the_recommended_option(db):
    published, _row = _publish_and_ask(db)
    decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="proceed?",
        options=_OPTIONS,
        recommended="a",
    )
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    # Marked twice on purpose: in words, so it still reads correctly with no
    # stylesheet, and with the class that makes it the filled primary button.
    assert '(a) yes<span class="tag">recommended</span>' in page
    assert '(b) no<span class="tag">recommended</span>' not in page
    assert 'value="a" class="rec"' in page
    assert 'value="b" class="rec"' not in page


def test_a_page_with_no_decisions_shows_no_decisions_block(db):
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "Decisions" not in page
    # The ANSWER form specifically. "<form" alone also matches the rename and
    # archive controls, which are page housekeeping and have nothing to do with
    # decisions — the third time today a broad substring over a rendered page
    # has caught the page's own furniture.
    assert "/answer" not in page


def test_the_decisions_block_escapes_a_hostile_question(db):
    published, _row = _publish_and_ask(db, question="<script>alert(1)</script>")
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_answered_decision_still_offers_every_button(db):
    """He can change his mind -- re-answering is allowed, so the form must not
    disappear or drop a button just because a decision already has an answer."""
    published, row = _publish_and_ask(db)
    decisions.answer(db, row.id, letter="a", note="go ahead", by="jeremy")
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    # An answered decision now lives in the "Already decided" timeline: one quiet
    # collapsed line carrying the answer, opening to the full card.
    assert '<details class="settled">' in page
    assert '<span class="sa">(a) yes</span>' in page
    # The mark that says WHICH one is his, distinct from the recommendation:
    # the chosen button is solid and tagged, and no other button is.
    assert 'value="a" class="chosen"' in page
    assert "&check; your answer" in page
    assert page.count("&check; your answer") == 1
    assert "go ahead" in page
    assert f'action="/d/{row.id}/answer"' in page
    assert "(a) yes" in page
    assert "(b) no" in page


def test_answering_with_no_origin_is_refused(db):
    published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer", data={"letter": "a"}
    )
    assert response.status_code == 403
    assert (
        decisions.list_for_artifact(db, published.id, include_answered=True)[0].answer
        is None
    )


def test_answering_with_a_foreign_origin_is_refused(db):
    _published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_answering_with_cross_site_sec_fetch_site_is_refused(db):
    _published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403


def test_answering_with_the_right_signals_redirects_and_stores_the_answer(db):
    published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a", "note": "go ahead"},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/a/{published.id}"
    updated = decisions.list_for_artifact(db, published.id, include_answered=True)[0]
    assert updated.answer == "a"
    assert updated.answer_note == "go ahead"


def test_answering_works_with_no_sec_fetch_site_header_at_all(db):
    """Sec-Fetch-Site is checked only when present -- a client that never sends it is
    judged on Origin alone, not refused for silence on a header it never had."""
    _published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"origin": store.gallery_origin()},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_the_framed_page_and_its_tokenised_src_are_unchanged_by_decisions(db):
    published, _row = _publish_and_ask(db)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert re.search(
        rf'src="https://{published.id}\.[^/"]+/p/[A-Za-z0-9_-]{{43}}/"', page
    )


def test_firefoxs_null_origin_is_accepted_when_fetch_metadata_proves_same_origin(db):
    """The bug that refused Jeremy's first real answer, 19 Sep 2026.

    Firefox derives the `Origin` header from `Referrer-Policy`, and this app sends
    `no-referrer` (that is what keeps a page's capability token out of a `Referer`).
    So a genuine click on this app's own form arrives from Firefox carrying
    `Origin: null`, and an equality check on `Origin` refuses the one person the
    whole page exists for. `Sec-Fetch-Site: same-origin` is what actually proves the
    request came from this origin -- the browser sets it, page script cannot forge
    it, and no referrer policy touches it.
    """
    published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"origin": "null", "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        decisions.list_for_artifact(db, published.id, include_answered=True)[0].answer
        == "a"
    )


def test_a_missing_origin_is_accepted_only_when_fetch_metadata_proves_same_origin(db):
    """Absent `Origin` is the same case as `null` -- it is what the referrer policy
    leaves behind -- so it turns on `Sec-Fetch-Site` alone, and nothing else."""
    published, row = _publish_and_ask(db)
    client = TestClient(create_gallery_app(db))

    refused = client.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert refused.status_code == 403

    accepted = client.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert (
        decisions.list_for_artifact(db, published.id, include_answered=True)[0].answer
        == "a"
    )


def test_headers_that_disagree_are_refused(db):
    """`Sec-Fetch-Site` saying same-origin does not license ANY `Origin`: a value
    that is neither this origin nor the referrer-policy's `null` means the two
    headers disagree, and a disagreement is refused rather than resolved."""
    published, row = _publish_and_ask(db)
    response = TestClient(create_gallery_app(db)).post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers={"origin": "https://evil.example", "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 403
    assert (
        decisions.list_for_artifact(db, published.id, include_answered=True)[0].answer
        is None
    )


def test_without_fetch_metadata_a_missing_origin_is_still_refused(db):
    """The fallback path is unchanged: no `Sec-Fetch-Site` at all means there is no
    Fetch Metadata to judge on, and `Origin` must then match exactly -- so the
    `null`/absent leniency above can never be reached by simply omitting both."""
    published, row = _publish_and_ask(db)
    client = TestClient(create_gallery_app(db))
    for headers in ({}, {"origin": "null"}):
        response = client.post(
            f"/d/{row.id}/answer", data={"letter": "a"}, headers=headers
        )
        assert response.status_code == 403
    assert (
        decisions.list_for_artifact(db, published.id, include_answered=True)[0].answer
        is None
    )


def test_the_chosen_option_outranks_the_recommended_one(db):
    """The bug Jeremy hit on 19 Sep 2026: he had answered (b), but (a) was the
    recommendation and the recommendation was the filled button -- so the page
    showed his answer in words while the strongest visual mark sat on a different
    letter, and he read it as "I can't change selection".

    Selection is a fact and recommendation is a hint, so when they disagree the
    fact takes the solid style and the only tag.
    """
    published, row = _publish_and_ask(db)
    decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="which way?",
        options=_OPTIONS,
        recommended="a",
    )
    decisions.answer(db, row.id, letter="b", by="jeremy")
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text

    # The answered decision: (b) is his, and it is the solid one.
    assert 'value="b" class="chosen"' in page
    # The still-open decision keeps its recommendation as a hint, NOT as a
    # selection -- it must never carry the chosen class or the chosen tag.
    assert 'value="a" class="rec"' in page
    assert 'class="rec chosen"' not in page
    assert page.count("&check; your answer") == 1


# ---------------------------------------------------------------------------
# The viewer shim: one scrollbar, and a decision that appears where the page
# puts it -- gallery.js, its CSP, and the ids/attributes it acts on.
# ---------------------------------------------------------------------------


def test_gallery_js_is_served_from_self_with_no_dependency(db):
    response = TestClient(create_gallery_app(db)).get("/gallery.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    text = response.text
    # single-digit KB, per the design's own budget -- and a ratchet well inside
    # it, not some looser number that would let a regression through unnoticed.
    # Moved deliberately, each time with its reason: 4096 when the file only
    # sized the frame (3050 bytes), 5120 once it also drove the tile menus
    # (4530), and 7168 on 21 Sep when it began telling him a page had been
    # republished under him (7003) -- homelab-ij8.74, which he ruled on
    # himself. That last move paid for its bytes twice over by keeping the
    # PROSE out of the served file: the argument for the feature lives in the
    # Python comment above `_GALLERY_JS`, which no browser ever downloads.
    # Moving it is a decision to be argued for in a commit, which is the whole
    # point of having it; what it must never become is a number that follows
    # the file upwards.
    #
    # 7680 on 22 Sep 2026 (7459): the lift threshold stopped being a pixel
    # number of the script's own and became the stylesheet's own media query,
    # plus a net that puts a block back where it came from if some rule
    # un-lifts it anyway. Two of Jeremy's questions rendered as blank white
    # space because those two numbers had drifted apart; 291 bytes is what it
    # costs to make them one number and to make the failure visible if it ever
    # happens again. The argument for it lives in the Python comment above
    # `_GALLERY_JS`, which no browser downloads.
    #
    # 8192 on 23 Sep 2026 (8000): a page's frame is now opened with a pass that
    # works once (homelab-ij8.101, Jeremy's ruling that a private page needs his
    # login to read), and browsers re-request a frame's last URL on reload and
    # back/forward. 396 bytes ask the gallery for a fresh pass when the frame
    # says its pass is spent -- without them, every reload in Firefox would
    # leave a page reading "expired". The argument lives above `_GALLERY_JS`.
    assert len(text.encode()) < 8192
    assert "http://" not in text and "https://" not in text  # no CDN, nothing fetched
    assert "eval(" not in text
    assert "new Function(" not in text


def test_gallery_js_checks_both_source_and_origin_before_trusting_a_message(db):
    text = TestClient(create_gallery_app(db)).get("/gallery.js").text
    assert "event.source" in text and "event.origin" in text
    assert "iframe.contentWindow" in text
    # the content origin comes from the iframe's OWN src, never a value the
    # message itself carries
    assert "new URL(iframe.src)" in text


def test_gallery_js_clamps_height_to_the_documented_range(db):
    text = TestClient(create_gallery_app(db)).get("/gallery.js").text
    assert "200" in text
    # 100000, not 20000, since 22 Sep 2026: the operator's board is roughly
    # 25,000px of aggregated decisions, and REFUSING its height left the frame
    # at its 78vh floor -- a small scrolling window, with every lifted control
    # clipped outside the sheet and a reserved hole where each should have been.
    # It is clamped now rather than refused, and `fits` stops the lift when the
    # clamp bites, because a page coordinate past the clamp points at nothing.
    assert "100000" in text
    assert "fits" in text
    assert "isFinite" in text


def test_gallery_js_never_polls(db):
    """No timer of any kind, still. The "has this page been republished" watch
    added on 21 Sep (homelab-ij8.74) is a HELD request -- the server waits, so
    the browser does not have to wake up and ask, and a hidden tab stops asking
    until it is looked at again. A `setInterval` here would be the "poll hard
    enough to matter" the bead rules out, arriving by the back door."""
    text = TestClient(create_gallery_app(db)).get("/gallery.js").text
    assert "ResizeObserver" not in text  # that lives in the content shim, not here
    assert "setInterval" not in text
    assert "setTimeout" not in text
    assert "wait=25" in text, "it holds rather than polls"
    assert "visibilitychange" in text, "and resumes on an event, not a timer"


@pytest.mark.parametrize("which", ["list", "detail"])
def test_the_gallery_csp_allows_only_self_scripts(db, which):
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    path = "/" if which == "list" else f"/a/{published.id}"
    csp = (
        TestClient(create_gallery_app(db)).get(path).headers["content-security-policy"]
    )
    directives = {p.strip() for p in csp.split(";") if p.strip()}
    assert "script-src 'self'" in directives
    assert not any(
        d.startswith("script-src") and "unsafe-inline" in d for d in directives
    )


def test_the_detail_page_loads_gallery_js_and_inlines_no_script(db):
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert '<script src="/gallery.js" defer></script>' in page
    # every <script> tag on this page carries a src -- none is inline, which the CSP
    # (script-src 'self', no 'unsafe-inline') would refuse to run anyway.
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page) is None


def test_the_detail_page_ids_the_frame_and_sheet_gallery_js_looks_for(db):
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert '<div class="sheet" id="artifact-sheet"' in page
    assert 'data-version="1"' in page, (
        "and says which version is in front of him, which is what the watcher "
        "compares against (homelab-ij8.74)"
    )
    assert 'id="artifact-frame"' in page


def test_the_list_page_never_loads_gallery_js(db):
    """No iframe, nothing to size or place -- the list page has no reason to pay
    for the script at all. (The shared stylesheet's own comments happen to name
    gallery.js in prose, so this checks for the <script> tag specifically, not the
    bare substring.)"""
    store.publish(db, "ops-seat", "Board", _PAGE)
    page = TestClient(create_gallery_app(db)).get("/").text
    assert "<script" not in page


def test_each_decision_carries_the_id_gallery_js_matches_slots_against(db):
    published, row = _publish_and_ask(db)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert f'data-decision-id="{row.id}"' in page


def test_two_decisions_get_two_distinct_data_decision_ids(db):
    published, first = _publish_and_ask(db, question="ship it?")
    second = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="which way?",
        options=_OPTIONS,
    )
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert f'data-decision-id="{first.id}"' in page
    assert f'data-decision-id="{second.id}"' in page
    assert first.id != second.id


def test_the_decisions_block_escapes_a_hostile_id_bearing_attribute(db):
    """`row.id` is a fixed base32 shape from ids.py, never agent content -- escaped
    anyway, on the same principle as every other field this module renders."""
    published, row = _publish_and_ask(db, question=XSS)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert f'data-decision-id="{row.id}"' in page
    assert "<img src=x" not in page


def test_the_whole_answer_path_still_works_with_gallery_js_never_fetched(db):
    """The promise this task makes: the ANSWER PATH keeps working with scripts off.
    This test never requests /gallery.js at all -- exactly what a script-blocked
    browser does -- and still completes the full round trip: render, plain POST,
    redirect, and the answer showing as chosen on the next render."""
    published, row = _publish_and_ask(db)
    client = TestClient(create_gallery_app(db))

    first = client.get(f"/a/{published.id}").text
    assert f'action="/d/{row.id}/answer"' in first

    response = client.post(
        f"/d/{row.id}/answer",
        data={"letter": "a", "note": "go ahead"},
        headers={"origin": store.gallery_origin(), "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    second = client.get(f"/a/{published.id}").text
    assert 'value="a" class="chosen"' in second
    assert "go ahead" in second


def test_an_answer_nobody_collected_says_so_and_one_that_was_collected_does_not(db):
    """Two different facts, and only one of them was ever on the page.

    Jeremy answering is the end of his part; the agent collecting the answer is
    the end of the round trip, and those came apart tonight -- a deploy killed a
    held request and the agent that asked heard nothing more. An uncollected
    answer used to look exactly like one acted on an hour ago.

    So the mark appears only in the bad case. An answer that landed needs no
    announcement; one still sitting there is the whole reason for the feature,
    and it says plainly that it is waiting on the agent, not on him.
    """
    published, row = _publish_and_ask(db)
    decisions.answer(db, row.id, letter="a", by="jeremy")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert '<span class="unread">not picked up yet</span>' in page
    assert "waiting for them, not for you" in page.lower()

    decisions.acknowledge(db, "ops-seat", row.id)

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "not picked up yet" not in page
    assert "ops-seat picked this up" in page


def test_a_decision_still_waiting_on_him_carries_no_collection_mark(db):
    """The mark is about an answer's onward journey. A decision he has not
    answered has no answer to collect, and a "not picked up yet" beside one
    would read as a reproach for something he has not done yet."""
    published, _row = _publish_and_ask(db)
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "not picked up yet" not in page
    assert "picked this up" not in page


def test_an_uncollected_answer_says_whether_the_agent_is_even_running(db):
    """ "Nobody took your answer" is two situations, needing opposite responses:
    an agent that is not running (someone else has to collect it) and one that is
    running and not acting (a bug to go and look at). One comparison tells them
    apart, and a message that cannot is one he learns to skip."""
    from artifacts import events

    published, row = _publish_and_ask(db)
    decisions.answer(db, row.id, letter="a", by="jeremy")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "probably not running" in page

    events.note_read(db, "ops-seat")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "probably not running" not in page
    assert "has looked since you answered" in page


def test_a_seat_that_stopped_looking_before_he_answered_counts_as_not_running(db):
    """The comparison is "since the answer". A seat that was listening an hour
    ago and is gone now must not be described as still looking."""
    from artifacts import events

    events.note_read(db, "ops-seat")
    published, row = _publish_and_ask(db)
    decisions.answer(db, row.id, letter="a", by="jeremy")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "probably not running" in page


def test_settled_questions_sit_below_the_page_and_open_ones_above(db):
    """Jeremy, 20 Sep 2026: "for sure it feels odd to have the questions at the
    top of the page". He was looking at his own status board, whose first screen
    was four questions he had ANSWERED the day before -- the page itself started
    underneath them, and the one question still open was below all of it.

    What must be acted on goes in front of the page; what is settled is history
    and goes after it.
    """
    published, settled = _publish_and_ask(db, question="Settled already?")
    decisions.answer(db, settled.id, letter="a", by="jeremy")
    decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="Still open?",
        options=_OPTIONS,
    )

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    frame = page.index("artifact-frame")
    assert page.index("Still open?") < frame, "an open question belongs above the page"
    assert page.index("Settled already?") > frame, "a settled one belongs below it"


def test_the_settled_block_is_collapsed_and_says_how_many(db):
    published, first = _publish_and_ask(db, question="Q one?")
    decisions.answer(db, first.id, letter="a", by="jeremy")
    second = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=published.id,
        question="Q two?",
        options=_OPTIONS,
    )
    decisions.answer(db, second.id, letter="a", by="jeremy")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text
    assert "2 questions you have already settled" in page
    # <details> with no `open`: closed until he asks for it.
    assert '<details class="asks settled-list"><summary>' in page
    assert '<details class="asks settled-list" open' not in page


def test_the_page_has_a_main_landmark(db):
    """Found by diffing the two documents' STRUCTURE rather than their pixels:
    the reference has a <main> and this had none. Without one there is nothing
    to skip to, so a screen-reader user arrives at the top of the chrome on
    every page and walks the header again before reaching a single tile.

    The controls row stays outside it on purpose -- tabs and search are
    navigation, not the content they filter."""
    client = TestClient(create_gallery_app(db))
    page = client.get("/").text
    assert page.count("<main>") == 1
    assert page.count("</main>") == 1
    # The wall is inside it; the header and the filter tabs are not.
    before, inside = page.split("<main>", 1)
    assert "<h1>" in before, "the title is chrome"
    assert 'class="tabs"' in before, "so are the tabs"
    assert 'class="tiles"' in inside or 'class="empty"' in inside


def _unnamed_controls(html: str) -> list[str]:
    """Every <button> and <summary> must carry visible text or an aria-label."""
    import re

    out = []
    for m in re.finditer(r"<(button|summary)\b([^>]*)>(.*?)</\1>", html, re.DOTALL):
        attrs, inner = m.group(2), m.group(3)
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if not text and "aria-label" not in attrs:
            out.append(m.group(0)[:70])
    return out


def test_every_view_names_its_controls_not_just_the_wall(db):
    """The first version of this checked the grid alone, which is the view I
    happened to be looking at. An icon-only control with no label is announced
    as "button", and every view here has some -- so every view is checked."""
    keys.mint(db, agent="seat")
    published = store.publish(
        db,
        "seat",
        "Board",
        "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>",
    )
    client = TestClient(create_gallery_app(db))

    views = {
        "grid": client.get("/").text,
        "list": client.get("/?view=list").text,
        "page": client.get(f"/a/{published.id}").text,
    }
    store.set_deleted(db, published.id, True, agent=None)
    views["bin"] = client.get("/?filter=deleted").text

    bad = {name: _unnamed_controls(html) for name, html in views.items()}
    bad = {name: found for name, found in bad.items() if found}
    assert bad == {}, f"controls with no accessible name: {bad}"


def test_a_settled_card_says_on_itself_that_it_can_be_changed(db):
    """Jeremy, 22 Sep 2026: "at least the ability to change the answer if we
    press a button."

    It has worked since 19 September, and was proved again on the live service
    the same morning -- tapping a second letter replaces the answer and its
    note. What was missing was anything SAYING so where the hand is. The section
    carries "nothing here is locked", but that sits at the foot of the whole
    collapsed list, after every other entry, so the card you have actually
    opened reads as a record rather than a control.
    """
    published, settled = _publish_and_ask(db, question="Settled already?")
    decisions.answer(db, settled.id, letter="a", by="jeremy")

    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}").text

    body = page[page.index('<div class="settled-body">') :]
    card = body[: body.index("</details>")]
    assert "Tap a different letter to change this" in card, (
        "the affordance belongs inside the opened card, not only under the list"
    )
    assert card.index("Tap a different letter") < card.index("<button"), (
        "and above the buttons it describes"
    )
    # The section-level line stays: it is what tells him before he opens
    # anything that none of this is final.
    assert "nothing here is locked" in page
