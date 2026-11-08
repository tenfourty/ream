"""A comment appears where he wrote it, without reloading the page.

The route answers JSON to a fetch (Accept: application/json) and keeps its 303
for the plain form; the script intercepts the submits and falls back to the
plain form on any failure, so a comment is never lost.
"""

import json

import pytest
from fastapi.testclient import TestClient

from ream import comments, decisions, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
ANCHOR = {"kind": "range", "path": "body>p:nth-of-type(1)", "start": 0, "end": 1}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


@pytest.fixture()
def page(db):
    return store.publish(db, "seat", "Board", PAGE)


JSON_HDRS = {"Sec-Fetch-Site": "same-origin", "Accept": "application/json"}


def _post(client, page, data, headers=None):
    return client.post(
        f"/c/{page.id}",
        data=data,
        headers=JSON_HDRS if headers is None else headers,
        follow_redirects=False,
    )


def test_a_json_post_of_a_range_comment_returns_the_thread_and_the_marks(client, page):
    got = _post(
        client,
        page,
        {"body": "Move this up", "quote": "x", "anchor": json.dumps(ANCHOR)},
    )
    assert got.status_code == 201
    data = got.json()
    assert "Move this up" in data["html"]
    assert data["html"].startswith('<li class="thread')
    assert data["thread_root"]
    assert [m["id"] for m in data["marks"]] == [data["thread_root"]]
    assert data["marks"][0]["kind"] == "comment"
    assert data["marks"][0]["held"] is False


def test_a_json_reply_returns_the_whole_thread(client, db, page):
    root = comments.add(
        db, artifact_id=page.id, author_kind="agent", author="seat", body="Done."
    )
    got = _post(client, page, {"body": "Thanks", "thread_root": root.id})
    assert got.status_code == 201
    data = got.json()
    assert data["thread_root"] == root.id
    assert "Done." in data["html"] and "Thanks" in data["html"]
    assert data["html"].count('<li class="thread') == 1


def test_without_the_json_accept_it_is_still_a_303(client, page):
    got = _post(client, page, {"body": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert got.status_code == 303
    assert got.headers["location"] == f"/a/{page.id}#comments"


def test_a_cross_site_json_post_is_refused_like_a_form_post(client, db, page):
    got = _post(
        client,
        page,
        {"body": "x"},
        headers={"Sec-Fetch-Site": "cross-site", "Accept": "application/json"},
    )
    plain = _post(client, page, {"body": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert plain.status_code in (400, 403)
    assert got.status_code == plain.status_code
    assert comments.list_for_artifact(db, page.id) == []


def test_a_script_in_the_body_comes_back_escaped(client, page):
    got = _post(client, page, {"body": "<script>alert(1)</script>"})
    html = got.json()["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_json_errors_are_json_with_the_form_status_codes(client, page):
    empty = _post(client, page, {"body": "  "})
    assert empty.status_code == 422
    assert "detail" in empty.json()
    missing = client.post(
        "/c/nope", data={"body": "x"}, headers=JSON_HDRS, follow_redirects=False
    )
    assert missing.status_code == 404
    assert "detail" in missing.json()


def test_a_held_mark_posted_in_place_carries_held(client, db, page):
    decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    got = _post(
        client,
        page,
        {"body": "hmm", "quote": "x", "anchor": json.dumps(ANCHOR)},
    )
    data = got.json()
    assert got.status_code == 201
    assert data["marks"][0]["held"] is True
    assert data["marks"][0]["kind"] == ""
    assert "htag" in data["html"]


def test_the_served_script_posts_json_disables_the_form_and_falls_back(client):
    js = client.get("/comments.js").text
    assert "application/json" in js
    assert "disabled" in js
    assert "form.submit()" in js or ".submit()" in js
    assert 'addEventListener("submit"' in js


def test_a_failure_building_the_json_after_the_save_says_reload(
    client, db, page, monkeypatch, caplog
):
    def boom(*a, **k):
        raise RuntimeError("marks broke")

    monkeypatch.setattr(comments, "marks_for_page", boom)
    got = _post(client, page, {"body": "saved anyway"})
    assert got.status_code == 201
    assert got.json() == {"reload": True}
    assert [r.body for r in comments.list_for_artifact(db, page.id)] == ["saved anyway"]
    assert "in-place reply failed" in caplog.text


def test_each_thread_names_its_mark_so_a_hover_card_can_copy_it(client, db, page):
    """The hover card is a copy of the panel's own escaped thread markup, found
    by the mark id the page reports -- never built from what the page sends."""
    root = comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        body="Move this up",
        quote="x",
        anchor=ANCHOR,
    )
    html = client.get(f"/a/{page.id}").text
    assert f'<li class="thread" data-mark-id="{root.id}" id="thread-{root.id}">' in html
    got = _post(client, page, {"body": "Thanks", "thread_root": root.id})
    assert got.json()["html"].startswith(
        f'<li class="thread" data-mark-id="{root.id}" id="thread-{root.id}">'
    )
