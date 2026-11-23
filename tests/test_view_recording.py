"""Views recorded where a reader is served a page body (runcove-5t3oj.158.1, slice 2;
docs/agent/specs/2026-11-22-view-stats-design.md). The four surfaces count; the things
that look like reads but are not (a screenshot, a thumbnail, a frame pass, the shared
stylesheet, the publisher's own read, a confirm page, a 404) never do. Both databases."""

import pytest
from fastapi.testclient import TestClient
from test_mcp import HOST, call_tool
from test_screenshot_api import a_shot

from ream import email_share, frame_pass, keys, pageviews, rendercore, settings, store
from ream.app import create_app, create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>{}</p></body></html>"
CONTENT = "tenfourty.com"


@pytest.fixture()
def page(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", CONTENT)
    keys.mint(db, agent="publisher")
    return store.publish(db, "publisher", "Plan", PAGE.format("the plan"))


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def outside(db):
    return TestClient(create_app(db))


def _host(page):
    return {"host": f"{page.id}.{CONTENT}"}


def _rows(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select kind, viewer, views from page_viewer where artifact_id = %s"
            " order by kind, viewer",
            (page_id,),
        ).fetchall()


def _days(db, page_id):
    with db.connect() as conn:
        return conn.execute(
            "select kind, views from page_view_day where artifact_id = %s order by kind",
            (page_id,),
        ).fetchall()


def _device(db, share, token):
    """A confirmed device for a named share: the cookie header a recipient's browser sends."""
    code = email_share.new_code(db, share)
    cookie = email_share.check_code(db, share, code)
    assert cookie
    return {"cookie": f"artifacts_share_{share.id}={cookie}"}


# --- the four surfaces count -----------------------------------------------------------


def test_the_gallery_page_counts_one_owner_view(db, gallery, page):
    assert gallery.get(f"/a/{page.id}").status_code == 200
    assert _rows(db, page.id) == [("owner", "", 1)]
    assert _days(db, page.id) == [("owner", 1)]
    gallery.get(f"/a/{page.id}")  # a reload inside the window
    assert _rows(db, page.id) == [("owner", "", 1)]


def test_the_anonymous_link_counts_one_external_view(db, gallery, outside, page):
    token = store.start_sharing(db, page.id)
    got = outside.get(f"/s/{token}/", headers=_host(page))
    assert got.status_code == 200 and "the plan" in got.text
    assert _rows(db, page.id) == [("external", "", 1)]
    assert _days(db, page.id) == [("external", 1)]


def test_a_named_recipient_counts_under_their_share_id_and_stamps_last_open(
    db, outside, page
):
    share, token = email_share.create(db, page.id, "friend@example.com")
    cookie = _device(db, share, token)
    got = outside.get(f"/e/{token}/", headers={**_host(page), **cookie})
    assert "the plan" in got.text
    assert _rows(db, page.id) == [("external", share.id, 1)]
    assert email_share.people(db, page.id)[0].last_opened_at is not None


def test_another_agent_reading_the_body_counts_by_name(db, outside, page):
    token, _ = keys.mint(db, agent="reader")
    auth = {"Authorization": f"Bearer {token}"}
    gallery_api = TestClient(create_gallery_app(db))
    assert (
        gallery_api.get(f"/api/v1/pages/{page.id}/body", headers=auth).status_code
        == 200
    )
    assert _rows(db, page.id) == [("agent", "reader", 1)]
    got = gallery_api.get(f"/api/v1/artifacts/{page.id}/versions/1", headers=auth)
    assert got.status_code == 200
    assert _rows(db, page.id) == [("agent", "reader", 1)]  # inside the window
    assert _days(db, page.id) == [("agent", 1)]


def test_another_agent_reading_through_mcp_counts(db, page):
    token, _ = keys.mint(db, agent="mcp-reader")
    client = TestClient(create_app(db), follow_redirects=False)
    with settings.override(mcp_public_url=f"https://{HOST}"):
        error, _read = call_tool(client, token, "read_page", page_id=page.id)
        assert not error
        # history, a listing, is not a body read
        call_tool(client, token, "read_page", page_id=page.id, history=True)
    assert _rows(db, page.id) == [("agent", "mcp-reader", 1)]


# --- what must not count ---------------------------------------------------------------


def test_the_publishers_own_reads_do_not_count(db, page):
    token, _ = keys.mint(db, agent="publisher")
    api = TestClient(create_gallery_app(db))
    auth = {"Authorization": f"Bearer {token}"}
    api.get(f"/api/v1/pages/{page.id}/body", headers=auth)
    api.get(f"/api/v1/pages/{page.id}/versions/1", headers=auth)
    client = TestClient(create_app(db), follow_redirects=False)
    with settings.override(mcp_public_url=f"https://{HOST}"):
        call_tool(client, token, "read_page", page_id=page.id)
    assert _rows(db, page.id) == []


def test_a_closed_page_read_by_another_agent_is_a_404_and_counts_nothing(db, page):
    store.set_agents_can_read(db, page.id, False, agent="publisher")
    token, _ = keys.mint(db, agent="reader")
    got = TestClient(create_gallery_app(db)).get(
        f"/api/v1/pages/{page.id}/body", headers={"Authorization": f"Bearer {token}"}
    )
    assert got.status_code == 404
    assert _rows(db, page.id) == []


def test_a_screenshot_does_not_count(db, page, monkeypatch, tmp_path):
    binary = tmp_path / "chromium"
    binary.write_text("")
    monkeypatch.setattr(rendercore, "shoot", lambda path, html, request, **kw: a_shot())
    token, _ = keys.mint(db, agent="publisher")
    with settings.override(browser=str(binary), renderer_url=""):
        got = TestClient(create_gallery_app(db)).get(
            f"/api/v1/pages/{page.id}/screenshot",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert got.status_code == 200
    assert _rows(db, page.id) == [] and _days(db, page.id) == []


def test_a_screenshot_of_another_agents_page_does_not_count(
    db, page, monkeypatch, tmp_path
):
    binary = tmp_path / "chromium"
    binary.write_text("")
    monkeypatch.setattr(rendercore, "shoot", lambda path, html, request, **kw: a_shot())
    token, _ = keys.mint(db, agent="reader")
    with settings.override(browser=str(binary), renderer_url=""):
        got = TestClient(create_gallery_app(db)).get(
            f"/api/v1/pages/{page.id}/screenshot",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert got.status_code == 200
    assert _rows(db, page.id) == [] and _days(db, page.id) == []


def test_a_thumbnail_does_not_count(db, gallery, page):
    gallery.get(f"/thumb/{page.id}.png")  # 200 or 404 by whether a picture exists
    assert _rows(db, page.id) == []


def test_a_frame_pass_spend_does_not_count(db, outside, page):
    got = outside.get(
        f"/p/{frame_pass.mint(db, page.id, None)}/",
        headers={**_host(page), "sec-fetch-dest": "iframe"},
    )
    assert got.status_code == 200 and "the plan" in got.text
    assert _rows(db, page.id) == [] and _days(db, page.id) == []


def test_the_shared_stylesheet_does_not_count(db, outside, page):
    got = outside.get("/_shared.css", headers=_host(page))
    assert got.status_code == 200
    assert _rows(db, page.id) == []


def test_a_confirm_page_without_a_device_does_not_count(db, outside, page):
    _share, token = email_share.create(db, page.id, "friend@example.com")
    got = outside.get(f"/e/{token}/", headers=_host(page))
    assert got.status_code == 200 and "the plan" not in got.text
    wrong = outside.get(
        f"/e/{token}/", headers={**_host(page), "cookie": "artifacts_share_x=nope"}
    )
    assert "the plan" not in wrong.text
    assert _rows(db, page.id) == []
    assert email_share.people(db, page.id)[0].last_opened_at is None


def test_a_withdrawn_or_wrong_link_does_not_count(db, outside, page):
    token = store.start_sharing(db, page.id)
    assert outside.get("/s/not-the-token/", headers=_host(page)).status_code == 404
    store.stop_sharing(db, page.id)
    assert outside.get(f"/s/{token}/", headers=_host(page)).status_code == 404
    share, etoken = email_share.create(db, page.id, "friend@example.com")
    cookie = _device(db, share, etoken)
    email_share.withdraw(db, page.id, share.id)
    got = outside.get(f"/e/{etoken}/", headers={**_host(page), **cookie})
    assert got.status_code == 404
    assert _rows(db, page.id) == []


def test_the_gallery_404_for_an_unknown_page_counts_nothing(db, gallery, page):
    assert gallery.get("/a/nosuchpage").status_code == 404
    with db.connect() as conn:
        assert conn.execute("select count(*) from page_viewer").fetchone()[0] == 0


def test_a_deleted_page_is_not_counted_in_the_gallery(db, gallery, page):
    store.set_deleted(db, page.id, True, agent="publisher")
    gallery.get(f"/a/{page.id}")
    assert _rows(db, page.id) == []


# --- a failure to record never fails the read ------------------------------------------


def test_a_failing_recorder_does_not_fail_any_read(
    db, gallery, outside, page, monkeypatch
):
    def boom(*_a, **_k):
        raise RuntimeError("down")

    # `record` itself never raises, but the failure inside it is the realistic one
    monkeypatch.setattr(pageviews, "_utc", boom)
    token = store.start_sharing(db, page.id)
    assert gallery.get(f"/a/{page.id}").status_code == 200
    assert outside.get(f"/s/{token}/", headers=_host(page)).status_code == 200
    key, _ = keys.mint(db, agent="reader")
    got = gallery.get(
        f"/api/v1/pages/{page.id}/body", headers={"Authorization": f"Bearer {key}"}
    )
    assert got.status_code == 200


def test_a_sibling_app_agent_reading_through_an_app_does_not_count(db):
    """A signed-in app writes the pages of its person's other app agents (`reach.writes()`),
    so it is one publisher with them: office decision D. An office's page, which it cannot
    write, still counts under the app-agent name."""
    import test_mcp_reach as r

    app = r._app(db)
    sibling = r._app(db, r.CLAUDE_CODE)
    mine = store.publish(db, sibling.agent, "By the sibling", PAGE.format("sib"))
    theirs = store.publish(db, "office", "By an office", PAGE.format("off"))
    for page_id in (mine.id, theirs.id):
        out = r._tool(r._as(db, app), "read_page", page_id=page_id)
        assert out["isError"] is False, out
    assert _rows(db, mine.id) == []
    assert _rows(db, theirs.id) == [("agent", app.agent, 1)]
