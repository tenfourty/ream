"""Small edits by quoted text on update (23 Nov 2026): `edits` on PATCH /pages/{id}.

The agent sends the exact words to replace and the words that replace them; they apply in order
to the LIVE page, each old_str must occur exactly once, and the whole result is stored as one
version through `store.patch` or nothing is. The MCP cases are in `tests/test_mcp.py`.
"""

import pytest
from fastapi.testclient import TestClient

from ream import decisions, keys, quoteedit, sections, store
from ream.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>CI</title></head><body>"
    '<h1>Board</h1><p id="status">3 slices landed</p>'
    "<ul><li>build one</li><li>build two</li></ul>"
    "<p>done done</p>"
    "</body></html>"
)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_DISPLAY_TZ", "Europe/Paris")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(client, db):
    token, _ = keys.mint(db, agent="ci-seat")
    made = client.post(
        "/api/v1/pages", json={"title": "CI", "body": PAGE}, headers=auth(token)
    ).json()
    return made["id"], token


def edit(client, aid, token, edits, word="pages", **rest):
    return client.patch(
        f"/api/v1/{word}/{aid}", json={"edits": edits, **rest}, headers=auth(token)
    )


def pair(old, new):
    return {"old_str": old, "new_str": new}


def live(db, aid):
    return store.get_body(db, "ci-seat", aid)


def versions(db, aid) -> int:
    with db.connect() as conn:
        return conn.execute(
            "select count(*) from version where artifact_id = %s", (aid,)
        ).fetchone()[0]


def last_note(db, aid):
    with db.connect() as conn:
        return conn.execute(
            "select note, section from version where artifact_id = %s order by n desc",
            (aid,),
        ).fetchone()


def _ask(db, aid):
    return decisions.ask(
        db,
        agent="ci-seat",
        artifact_id=aid,
        question="Ship it?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )


def with_slot(db, aid):
    """An open question whose slot is on the page. Returns the decision id."""
    asked = _ask(db, aid)
    current = live(db, aid)
    slot = f'<div id="q" data-artifact-decision="{asked.id}">Q</div>'
    store.patch(
        db,
        "ci-seat",
        aid,
        current.body.replace("</body>", slot + "</body>"),
        current.revision,
    )
    return asked.id


# --- the pure function ------------------------------------------------------


def test_overlapping_occurrences_are_ambiguous():
    with pytest.raises(quoteedit.EditError) as caught:
        quoteedit.apply("aaa", [("aa", "b")])
    assert (caught.value.index, caught.value.matches) == (0, 2)


# --- the route --------------------------------------------------------------


def test_an_edit_replaces_the_quote_and_stores_one_version(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("3 slices landed", "4 slices landed")])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["unchanged"] is False
    assert live(db, aid).body == PAGE.replace("3 slices", "4 slices")
    assert last_note(db, aid) == ("edits: 1", None)
    assert versions(db, aid) == 2


def test_the_old_word_serves_it_too(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "build 1")], word="artifacts")
    assert r.status_code == 200, r.text
    assert "<li>build 1</li>" in live(db, aid).body


def test_a_callers_note_follows_the_count(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "b1")], note="renamed")
    assert r.status_code == 200, r.text
    assert last_note(db, aid)[0] == "edits: 1 -- renamed"


def test_a_later_edit_sees_the_text_the_earlier_one_made(client, db, page):
    aid, token = page
    r = edit(
        client,
        aid,
        token,
        [pair("build one", "build uno"), pair("build uno</li>", "build 1</li>")],
    )
    assert r.status_code == 200, r.text
    assert "<li>build 1</li>" in live(db, aid).body
    assert last_note(db, aid)[0] == "edits: 2"


def test_a_miss_names_the_failing_index_and_stores_nothing(client, db, page):
    aid, token = page
    r = edit(
        client,
        aid,
        token,
        [pair("build one", "b1"), pair("no such words", "x"), pair("build two", "b2")],
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "field": "edits",
        "index": 1,
        "matches": 0,
        "detail": "old_str occurs 0 times",
    }
    assert "no such words" not in r.text and "build" not in r.text
    assert live(db, aid).body == PAGE
    assert versions(db, aid) == 1


def test_an_ambiguous_quote_is_refused_with_its_count(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("<li>", "<li class='x'>")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["index"] == 0
    assert r.json()["detail"]["matches"] == 2
    assert r.json()["detail"]["detail"] == "old_str occurs more than once"
    assert live(db, aid).body == PAGE


def test_overlapping_matches_are_ambiguous(client, db):
    token, _ = keys.mint(db, agent="ci-seat")
    body = "<!doctype html><html><body><p>aaa</p></body></html>"
    aid = client.post(
        "/api/v1/pages", json={"title": "t", "body": body}, headers=auth(token)
    ).json()["id"]
    r = edit(client, aid, token, [pair("aa", "b")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["matches"] == 2
    assert live(db, aid).body == body


def test_an_empty_old_str_is_refused(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "b1"), pair("", "x")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "edits"
    assert r.json()["detail"]["index"] == 1
    assert live(db, aid).body == PAGE


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [pair("a", "b")] * 51,
        [pair("x" * (64 * 1024 + 1), "b")],
        [pair("build one", "y" * (64 * 1024 + 1))],
    ],
    ids=["none", "fifty-one", "long-old", "long-new"],
)
def test_the_limits_are_422_and_nothing_is_stored(client, db, page, edits):
    aid, token = page
    r = edit(client, aid, token, edits)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "edits"
    assert versions(db, aid) == 1


def test_the_limits_allow_fifty_edits_of_64_kib(client, db, page):
    aid, token = page
    big = "y" * (64 * 1024)
    r = edit(client, aid, token, [pair("build one", big)] + [pair(big, big)] * 49)
    assert r.status_code == 200, r.text
    assert big in live(db, aid).body


def test_a_secret_in_new_str_is_refused_and_not_echoed(client, db, page):
    aid, token = page
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    r = edit(client, aid, token, [pair("build one", f"key {secret}")])
    assert r.status_code == 422, r.text
    assert secret not in r.text
    assert live(db, aid).body == PAGE
    assert versions(db, aid) == 1


def test_a_result_that_is_not_a_page_is_refused(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair(PAGE, "just some words")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "body"
    assert live(db, aid).body == PAGE


def test_an_edit_that_would_drop_an_open_questions_slot_is_refused(client, db, page):
    aid, token = page
    decision = with_slot(db, aid)
    before = live(db, aid).body
    r = edit(
        client, aid, token, [pair('<div id="q" data-artifact-decision', "<div data-x")]
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["decision"] == decision
    assert r.json()["detail"]["field"] == "edits"
    assert live(db, aid).body == before
    # An edit that leaves the slot alone goes through while the question is open.
    ok = edit(client, aid, token, [pair("build one", "b1")])
    assert ok.status_code == 200, ok.text


def test_a_closed_questions_slot_may_go(client, db, page):
    aid, token = page
    decision = with_slot(db, aid)
    decisions.close(db, "ci-seat", decision)
    r = edit(
        client, aid, token, [pair('<div id="q" data-artifact-decision', "<div data-x")]
    )
    assert r.status_code == 200, r.text


def test_a_question_opened_during_the_update_keeps_its_slot(db, page):
    """No slots are read while no question is open; one that opens before the write is
    caught by the check inside the transaction, which sends the update round again."""
    aid, _ = page
    decision = with_slot(db, aid)
    decisions.close(db, "ci-seat", decision)
    real_patch = store.patch
    reopened = []

    def reopen_then_patch(*args, **kwargs):
        if not reopened:
            reopened.append(1)
            with db.connect() as conn:
                conn.execute(
                    "update decision set closed_at = null where id = %s", (decision,)
                )
        return real_patch(*args, **kwargs)

    store.patch = reopen_then_patch
    try:
        with pytest.raises(store.OpenDecisionDropped):
            store.patch_edits(
                db,
                "ci-seat",
                aid,
                [('<div id="q" data-artifact-decision', "<div data-x")],
            )
    finally:
        store.patch = real_patch
    assert f'data-artifact-decision="{decision}"' in live(db, aid).body


def test_no_page_is_parsed_while_no_question_is_open(client, db, page, monkeypatch):
    aid, token = page

    def parsed(_body):
        raise AssertionError("read the page's slots with no question open")

    monkeypatch.setattr(sections, "slots", parsed)
    assert edit(client, aid, token, [pair("build one", "b1")]).status_code == 200


def test_expected_revision_names_a_revision_or_a_version(client, db, page):
    aid, token = page
    first = live(db, aid)
    assert edit(client, aid, token, [pair("build one", "b1")]).status_code == 200
    stale = edit(
        client,
        aid,
        token,
        [pair("build two", "b2")],
        expected_revision=first.revision,
    )
    assert stale.status_code == 409, stale.text
    assert "changed" in stale.json()["detail"]
    assert "<li>build two</li>" in live(db, aid).body
    now = live(db, aid)
    assert (
        edit(
            client,
            aid,
            token,
            [pair("build two", "b2")],
            expected_revision=now.revision,
        ).status_code
        == 200
    )
    assert (
        edit(
            client,
            aid,
            token,
            [pair("b2", "b22")],
            expected_revision=str(now.version + 1),
        ).status_code
        == 200
    )


def test_without_a_revision_a_race_is_retried_on_the_new_page(client, db, page):
    aid, token = page
    real_patch = store.patch
    raced = []

    def patch_after_a_rival(*args, **kwargs):
        if not raced:
            raced.append(1)
            current = store.get_body(db, "ci-seat", aid)
            real_patch(
                db,
                "ci-seat",
                aid,
                current.body.replace("build two", "build 2"),
                current.revision,
            )
        return real_patch(*args, **kwargs)

    store.patch = patch_after_a_rival
    try:
        r = edit(client, aid, token, [pair("build one", "b1")])
    finally:
        store.patch = real_patch
    assert r.status_code == 200, r.text
    body = live(db, aid).body
    assert "<li>b1</li>" in body and "<li>build 2</li>" in body


def test_an_edit_that_changes_nothing_answers_unchanged(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "build one")])
    assert r.status_code == 200, r.text
    assert r.json()["unchanged"] is True
    assert r.json()["version"] == 1
    assert versions(db, aid) == 1


@pytest.mark.parametrize(
    "extra",
    [{"body": PAGE, "expected_revision": "a" * 16}, {"format": "html"}],
    ids=["body", "format"],
)
def test_edits_do_not_go_with_a_body_or_a_format(client, db, page, extra):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "b1")], **extra)
    assert r.status_code == 422, r.text
    assert live(db, aid).body == PAGE


def test_a_whole_page_update_still_needs_a_body_and_a_revision(client, db, page):
    aid, token = page
    url = f"/api/v1/pages/{aid}"
    assert client.patch(url, json={}, headers=auth(token)).status_code == 422
    r = client.patch(url, json={"body": PAGE + " "}, headers=auth(token))
    assert r.status_code == 422, r.text
    assert versions(db, aid) == 1
    ok = client.patch(
        url,
        json={"body": PAGE + " ", "expected_revision": live(db, aid).revision},
        headers=auth(token),
    )
    assert ok.status_code == 200, ok.text


def test_another_agents_page_is_a_404_like_an_unknown_one(client, db, page):
    aid, _ = page
    other, _ = keys.mint(db, agent="other-seat")
    theirs = edit(client, aid, other, [pair("build one", "b1")])
    unknown = edit(client, "a" * 26, other, [pair("build one", "b1")])
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json() == {"detail": "no such page"}
    assert live(db, aid).body == PAGE


def test_a_page_too_large_to_read_for_slots_is_edited_while_no_question_is_open(
    client, db
):
    token, _ = keys.mint(db, agent="ci-seat")
    filler = "<p>" + "filler " * 100 + "</p>"
    body = (
        "<!doctype html><html><body><p>one word</p>"
        + filler * (sections.SECTION_PAGE_MAX // len(filler) + 10)
        + "</body></html>"
    )
    aid = client.post(
        "/api/v1/pages", json={"title": "big", "body": body}, headers=auth(token)
    ).json()["id"]
    assert edit(client, aid, token, [pair("one word", "two words")]).status_code == 200
    _ask(db, aid)
    r = edit(client, aid, token, [pair("two words", "three words")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "edits"


def test_a_null_format_beside_edits_is_absent(client, db, page):
    aid, token = page
    r = edit(client, aid, token, [pair("build one", "b1")], format=None)
    assert r.status_code == 200, r.text
    assert "<li>b1</li>" in live(db, aid).body


def test_a_count_stops_at_the_second_match(client, db):
    token, _ = keys.mint(db, agent="ci-seat")
    body = "<!doctype html><html><body>" + "<p>x</p>" * 50 + "</body></html>"
    aid = client.post(
        "/api/v1/pages", json={"title": "t", "body": body}, headers=auth(token)
    ).json()["id"]
    r = edit(client, aid, token, [pair("<p>x</p>", "y")])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["matches"] == 2
