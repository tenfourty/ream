"""Each page says whether commenting starts on or off (9 Nov 2026, docs/decisions.md).

The publishing agent chooses, per page. The server refuses a NEW page that does not say; an
update does not need it; only the page's own agent changes it, and "not yours" answers exactly
as "no such page". The suite adds `comments: "on"` to a publish that omits the key
(tests/conftest.py); these tests send exactly what they mean, so they carry
`no_default_comments`. They run on both databases (the `db` fixture follows REAM_TEST_DB).
"""

import pytest
from fastapi.testclient import TestClient

from ream import clientlevel, keys, settings, store
from ream.app import create_app, create_gallery_app

pytestmark = pytest.mark.no_default_comments

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
HOST = "artifacts.tenfourty.site"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def token(db) -> str:
    made, _ = keys.mint(db, agent="seat")
    return made


@pytest.fixture()
def other(db) -> str:
    made, _ = keys.mint(db, agent="other")
    return made


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def _stored(db, page_id: str):
    with db.connect() as conn:
        return conn.execute(
            "select comments_default from artifact where id = %s", (page_id,)
        ).fetchone()[0]


def _publish(client, token, **extra):
    return client.post(
        "/api/v1/pages",
        json={"title": "T", "body": PAGE, **extra},
        headers=auth(token),
    )


@pytest.mark.parametrize("word", ["on", "off"])
@pytest.mark.parametrize("route", ["/api/v1/pages", "/api/v1/artifacts"])
def test_a_publish_stores_the_setting_under_both_words(client, db, token, word, route):
    made = client.post(
        route,
        json={"title": "T", "body": PAGE, "comments": word},
        headers=auth(token),
    )
    assert made.status_code == 201, made.text
    assert made.json()["comments"] == word
    assert _stored(db, made.json()["id"]) == word
    assert store.get_owned(db, "seat", made.json()["id"]).comments == word


needs_step2 = pytest.mark.skipif(
    not clientlevel.COMMENTS_REQUIRED,
    reason="step 2 of the comments setting (the refusal) is off: clientlevel.COMMENTS_REQUIRED",
)


@needs_step2
@pytest.mark.parametrize("route", ["/api/v1/pages", "/api/v1/artifacts"])
@pytest.mark.parametrize("extra", [{}, {"comments": None}])
def test_a_new_page_without_the_setting_is_refused_saying_what_to_send(
    client, db, token, route, extra
):
    refused = client.post(
        route, json={"title": "T", "body": PAGE, **extra}, headers=auth(token)
    )
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["field"] == "comments"
    for words in ('"on"', '"off"', "--comments on|off", "reinstall"):
        assert words in detail["detail"]
    assert store.list_for_agent(db, "seat") == [], "nothing was stored"


@pytest.mark.skipif(clientlevel.COMMENTS_REQUIRED, reason="step 2 is on")
@pytest.mark.parametrize("route", ["/api/v1/pages", "/api/v1/artifacts"])
@pytest.mark.parametrize("extra", [{}, {"comments": None}])
def test_step_one_accepts_a_new_page_without_the_setting_and_stores_none(
    client, db, token, route, extra
):
    made = client.post(
        route, json={"title": "T", "body": PAGE, **extra}, headers=auth(token)
    )
    assert made.status_code == 201, made.text
    (row,) = store.list_for_agent(db, "seat")
    assert row.comments is None, "not stated is stored as null, which reads as on"


def test_a_value_other_than_on_or_off_is_refused(client, token):
    assert _publish(client, token, comments="maybe").status_code == 422


def test_an_update_keeps_the_setting_and_changes_it_when_given(client, db, token):
    page = _publish(client, token, comments="off").json()
    patch = client.patch(
        f"/api/v1/pages/{page['id']}",
        json={"body": PAGE + "<!-- 2 -->", "expected_revision": page["revision"]},
        headers=auth(token),
    )
    assert patch.status_code == 200, patch.text
    assert _stored(db, page["id"]) == "off", "an update without it keeps it"
    again = client.patch(
        f"/api/v1/pages/{page['id']}",
        json={
            "body": PAGE + "<!-- 3 -->",
            "expected_revision": patch.json()["revision"],
            "comments": "on",
        },
        headers=auth(token),
    )
    assert again.status_code == 200, again.text
    assert _stored(db, page["id"]) == "on"


def test_the_small_route_changes_only_the_setting(client, db, token):
    page = _publish(client, token, comments="on").json()
    done = client.post(
        f"/api/v1/pages/{page['id']}/comments-setting",
        json={"comments": "off"},
        headers=auth(token),
    )
    assert done.status_code == 200 and done.json()["comments"] == "off"
    assert _stored(db, page["id"]) == "off"
    assert store.get_owned(db, "seat", page["id"]).current_version == 1, "no version"
    assert (
        client.post(
            f"/api/v1/pages/{page['id']}/comments-setting",
            json={"comments": "sideways"},
            headers=auth(token),
        ).status_code
        == 422
    )


def test_another_key_cannot_change_it_and_is_told_what_a_missing_page_is_told(
    client, db, token, other
):
    page = _publish(client, token, comments="on").json()
    theirs = client.post(
        f"/api/v1/pages/{page['id']}/comments-setting",
        json={"comments": "off"},
        headers=auth(other),
    )
    missing = client.post(
        "/api/v1/pages/nopage000000/comments-setting",
        json={"comments": "off"},
        headers=auth(other),
    )
    assert (theirs.status_code, theirs.json()) == (missing.status_code, missing.json())
    assert theirs.status_code == 404
    patched = client.patch(
        f"/api/v1/pages/{page['id']}",
        json={
            "body": PAGE + "<!-- x -->",
            "expected_revision": page["revision"],
            "comments": "off",
        },
        headers=auth(other),
    )
    assert patched.status_code == 404
    assert _stored(db, page["id"]) == "on"


def test_rows_from_before_the_setting_stay_null_and_read_as_on(client, db, token):
    old = store.publish(db, "seat", "Old", PAGE)
    assert _stored(db, old.id) is None
    html = client.get(f"/a/{old.id}").text
    assert "data-comments-default" not in html


def test_the_gallery_page_carries_the_setting(client, db):
    off = store.publish(db, "seat", "Quiet", PAGE, comments="off")
    on = store.publish(db, "seat", "Loud", PAGE, comments="on")
    assert 'data-comments-default="off"' in client.get(f"/a/{off.id}").text
    assert "data-comments-default" not in client.get(f"/a/{on.id}").text


# --- MCP ---------------------------------------------------------------------


def _mcp(db, token, name, arguments):
    app = TestClient(create_app(db), follow_redirects=False)
    with settings.override(mcp_public_url=f"https://{HOST}"):
        return app.post(
            "/mcp",
            headers={
                "host": HOST,
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ).json()["result"]


@needs_step2
def test_the_mcp_publish_tool_refuses_a_new_page_without_the_setting(db, token):
    refused = _mcp(db, token, "publish_page", {"title": "T", "body": PAGE})
    assert refused["isError"] is True
    assert "comments" in refused["content"][0]["text"]
    assert store.list_for_agent(db, "seat") == []


@pytest.mark.skipif(clientlevel.COMMENTS_REQUIRED, reason="step 2 is on")
def test_step_one_the_mcp_publish_tool_accepts_a_new_page_without_the_setting(
    db, token
):
    made = _mcp(db, token, "publish_page", {"title": "T", "body": PAGE})
    assert not made["isError"], made
    (row,) = store.list_for_agent(db, "seat")
    assert row.comments is None


def test_the_mcp_publish_tool_stores_the_setting(db, token):
    made = _mcp(
        db, token, "publish_page", {"title": "T", "body": PAGE, "comments": "off"}
    )
    assert not made["isError"], made
    (row,) = store.list_for_agent(db, "seat")
    assert row.comments == "off"


def test_the_mcp_organise_tool_changes_it_and_scopes_it(db, token, other):
    page = store.publish(db, "seat", "T", PAGE, comments="on")
    args = {"page_id": page.id, "action": "comments_off"}
    assert _mcp(db, other, "organise_page", args)["isError"] is True
    assert _stored(db, page.id) == "on"
    assert not _mcp(db, token, "organise_page", args)["isError"]
    assert _stored(db, page.id) == "off"


def test_the_mcp_update_tool_takes_it_and_does_not_require_it(db, token):
    page = store.publish(db, "seat", "T", PAGE, comments="on")
    args = {
        "page_id": page.id,
        "body": PAGE + "<!-- 2 -->",
        "expected_revision": page.revision,
    }
    done = _mcp(db, token, "update_page", args)
    assert not done["isError"], done
    assert _stored(db, page.id) == "on"
    args["expected_revision"] = store.get_owned(db, "seat", page.id).revision
    args["body"] = PAGE + "<!-- 3 -->"
    assert not _mcp(db, token, "update_page", {**args, "comments": "off"})["isError"]
    assert _stored(db, page.id) == "off"
