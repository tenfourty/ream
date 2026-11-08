"""Every agent in his space can read every page, unless the page is closed to
just Jeremy and its publisher (homelab-ij8.105, his ruling of 23 Sep 2026:
1a 2a 3a 4b 5a)."""

import re

import pytest
from fastapi.testclient import TestClient

from ream import decisions, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


@pytest.fixture()
def page(db):
    return store.publish(db, "owner", "Board", PAGE.format("the body"))


def test_a_new_page_is_readable_by_every_agent(api, seats, page):
    other = seats["other"]
    meta = api.get(f"/api/v1/artifacts/{page.id}", headers=other).json()
    assert meta["agent"] == "owner" and meta["agents_can_read"] is True
    assert (
        "the body"
        in api.get(f"/api/v1/artifacts/{page.id}/body", headers=other).json()["body"]
    )
    assert (
        api.get(f"/api/v1/artifacts/{page.id}/versions", headers=other).status_code
        == 200
    )
    assert (
        api.get(f"/api/v1/artifacts/{page.id}/versions/1", headers=other).status_code
        == 200
    )


def test_a_reader_sees_the_questions_and_his_answers(db, api, seats, page):
    asked = decisions.ask(
        db,
        agent="owner",
        artifact_id=page.id,
        question="ship?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )
    decisions.answer(db, asked.id, letter="a", note="go", by="jeremy")
    rows = api.get(
        f"/api/v1/artifacts/{page.id}/decisions", headers=seats["other"]
    ).json()
    assert rows[0]["answer"] == "a"
    assert (
        api.get(
            f"/api/v1/decisions/{asked.id}/outcomes", headers=seats["other"]
        ).status_code
        == 200
    )


def test_but_only_the_publisher_can_change_it(api, seats, page):
    other = seats["other"]
    assert (
        api.patch(
            f"/api/v1/artifacts/{page.id}",
            json={"body": PAGE.format("x"), "expected_revision": page.revision},
            headers=other,
        ).status_code
        == 404
    )
    for path, body in (
        ("archive", {"archived": True}),
        ("rename", {"title": "mine now"}),
        ("pin", {"pinned": True}),
        ("agents", {"can_read": False}),
    ):
        assert (
            api.post(
                f"/api/v1/artifacts/{page.id}/{path}", json=body, headers=other
            ).status_code
            == 404
        ), path
    assert api.delete(f"/api/v1/artifacts/{page.id}", headers=other).status_code == 404
    assert (
        api.post(
            f"/api/v1/artifacts/{page.id}/decisions",
            json={
                "question": "q?",
                "options": [
                    {"letter": "a", "label": "x"},
                    {"letter": "b", "label": "y"},
                ],
            },
            headers=other,
        ).status_code
        == 404
    )


def test_a_closed_page_is_invisible_to_others_exactly_like_a_missing_one(
    api, seats, page
):
    closed = api.post(
        f"/api/v1/artifacts/{page.id}/agents",
        json={"can_read": False},
        headers=seats["owner"],
    )
    assert closed.status_code == 200 and closed.json()["agents_can_read"] is False
    for path in ("", "/body", "/versions", "/versions/1", "/decisions"):
        mine = api.get(f"/api/v1/artifacts/{page.id}{path}", headers=seats["other"])
        missing = api.get(f"/api/v1/artifacts/{'b' * 26}{path}", headers=seats["other"])
        assert mine.status_code == missing.status_code == 404, path
        assert mine.json() == missing.json(), path
    # and the publisher still has it
    assert (
        api.get(f"/api/v1/artifacts/{page.id}/body", headers=seats["owner"]).status_code
        == 200
    )


def test_whether_he_has_read_it_is_only_the_publishers_to_know(api, seats, page):
    api.get(f"/a/{page.id}")  # he opens it in the gallery
    assert (
        api.get(f"/api/v1/artifacts/{page.id}", headers=seats["owner"]).json()[
            "seen_version"
        ]
        == 1
    )
    assert (
        api.get(f"/api/v1/artifacts/{page.id}", headers=seats["other"]).json()[
            "seen_version"
        ]
        is None
    )


def test_agents_can_browse_every_open_page_in_the_space(db, api, seats, page):
    hidden = store.publish(db, "owner", "Private", PAGE.format("p"))
    store.set_agents_can_read(db, hidden.id, False, agent="owner")
    theirs = api.get("/api/v1/artifacts?scope=all", headers=seats["other"]).json()
    assert [r["id"] for r in theirs] == [page.id]
    assert theirs[0]["agent"] == "owner"
    # the default listing is unchanged: only your own
    assert api.get("/api/v1/artifacts", headers=seats["other"]).json() == []
    owners = {
        r["id"]
        for r in api.get("/api/v1/artifacts?scope=all", headers=seats["owner"]).json()
    }
    assert owners == {page.id, hidden.id}


def test_he_can_close_and_reopen_any_page_from_its_menu(api, page):
    before = api.get(f"/a/{page.id}").text
    assert (
        "All your agents can read it" in before
        and "Close to just you and owner" in before
    )
    api.post(f"/a/{page.id}/agents", data={"can_read": "0"}, headers=SAME_ORIGIN)
    after = api.get(f"/a/{page.id}").text
    assert (
        "Only you and owner can read it" in after
        and "Let all your agents read it" in after
    )


def test_another_site_cannot_flip_it_with_his_session(api, page):
    response = api.post(
        f"/a/{page.id}/agents",
        data={"can_read": "0"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403


@pytest.mark.postgres_only  # migrates the Postgres schema
@pytest.mark.usefixtures("fresh_schema")
def test_pages_that_existed_before_the_setting_start_closed(db):
    """His 2a: he sees the list and picks before anything opens. The migration
    closes rows that predate the column; new rows are open by default."""
    with db.connect() as conn:
        conn.execute("alter table artifact drop column agents_can_read")
    with db.connect() as conn:
        conn.execute(
            "insert into artifact (id, title, agent, read_token) values ('a'||repeat('x',25), 'Old', 'owner', 't')"
        )
    db.migrate()
    new = store.publish(db, "owner", "New", PAGE.format("n"))
    with db.connect() as conn:
        rows = dict(
            conn.execute("select title, agents_can_read from artifact").fetchall()
        )
    assert rows == {"Old": False, "New": True}
    assert new.id


def test_the_admin_review_opens_all_but_what_he_keeps(db, monkeypatch, capsys):
    from ream import admin

    keep = store.publish(db, "owner", "Keep", PAGE.format("k"))
    go = store.publish(db, "owner", "Go", PAGE.format("g"))
    for p in (keep, go):
        store.set_agents_can_read(db, p.id, False, agent=None)
    monkeypatch.setattr(admin, "_database_url", lambda: db.dsn)
    admin.main(["closed-to-agents"])
    assert "2 closed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        admin.main(["closed-to-agents", "--open-all-except", "nope"])
    admin.main(["closed-to-agents", "--open-all-except", keep.id])
    out = capsys.readouterr().out
    assert re.search(rf"opened\t{go.id}", out) and "1 opened, 1 kept closed" in out
    assert store.get_owned(db, "owner", go.id).agents_can_read is True
    assert store.get_owned(db, "owner", keep.id).agents_can_read is False


# --- one section, read by another agent (10 Oct 2026) -------------------------
# A whole open page was readable by every agent, but one section of it was not:
# gg-ops, building the shared avatar gallery, could fetch artifacts' page whole
# and not the one element it needed. A read of one element follows the same rule
# as a read of the page; writing it stays the publisher's alone.

SECTIONED = PAGE.format('<section id="ideas"><p>drawn</p></section><p id="other">x</p>')


@pytest.fixture()
def sectioned(db):
    return store.publish(db, "owner", "Board", SECTIONED)


def test_another_agent_reads_one_section_of_an_open_page(api, seats, sectioned):
    got = api.get(
        f"/api/v1/artifacts/{sectioned.id}/sections/ideas", headers=seats["other"]
    )
    assert got.status_code == 200, got.text
    assert got.json()["html"] == '<section id="ideas"><p>drawn</p></section>'


def test_a_closed_pages_section_answers_as_no_such_page(db, api, seats, sectioned):
    store.set_agents_can_read(db, sectioned.id, False, agent="owner")
    got = api.get(
        f"/api/v1/artifacts/{sectioned.id}/sections/ideas", headers=seats["other"]
    )
    missing = api.get(
        "/api/v1/artifacts/doesnotexist/sections/ideas", headers=seats["other"]
    )
    assert got.status_code == missing.status_code == 404
    assert got.json() == missing.json()


def test_another_agent_still_cannot_update_a_section(db, api, seats, sectioned):
    r = api.patch(
        f"/api/v1/artifacts/{sectioned.id}/sections/ideas",
        json={"op": "replace", "html": "<p>mine now</p>"},
        headers=seats["other"],
    )
    assert r.status_code == 404
    assert store.get_body(db, "owner", sectioned.id).body == SECTIONED
