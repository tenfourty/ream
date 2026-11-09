"""Slice 6: "Connected apps" on the machines page, and `artifacts-admin list-apps` / `revoke-app`."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_mcp_sign_in import stub  # noqa: F401 -- fixture
from test_oauth import (  # noqa: F401 -- fixture
    begin,
    exchange,
    full_flow,
    landing,
    sign_in,
    web,
)
from test_oauth_refresh import at_mcp
from test_oidc import provider  # noqa: F401 -- fixture

from ream.admin import main
from ream.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def gallery(db):
    return TestClient(create_gallery_app(db))


def connect(web, provider):
    return exchange(web, full_flow(web, provider)["code"]).json()


def app_id(db) -> str:
    with db.connect() as conn:
        return conn.execute("select id from mcp_app").fetchone()[0]


def test_a_live_app_is_listed_and_the_page_shows_no_token_or_hash(
    web, provider, db, gallery
):
    pair = connect(web, provider)
    page = gallery.get("/machines").text
    assert "Connected apps" in page
    assert "Jeremy, via claude.ai" in page
    assert "read pages, comments and questions" in page
    assert "Lapses on" in page
    for secret in (pair["access_token"], pair["refresh_token"]):
        assert secret not in page
    assert "rmo_" not in page and "rmr_" not in page
    assert not re.search(r"[0-9a-f]{64}", page)
    with db.connect() as conn:
        digest = conn.execute("select token_sha256 from mcp_token").fetchone()[0]
    assert digest not in page


def test_revoke_asks_once_more_then_stops_the_next_call(web, provider, db, gallery):
    pair = connect(web, provider)
    ident = app_id(db)
    assert at_mcp(web, pair["access_token"]).status_code == 200
    asked = gallery.get(f"/machines?revoke_app={ident}").text
    assert f'action="/machines/apps/{ident}/revoke"' in asked
    done = gallery.post(f"/machines/apps/{ident}/revoke", headers=SAME_ORIGIN)
    assert done.status_code == 200 and "Revoked" in done.text
    assert at_mcp(web, pair["access_token"]).status_code == 401
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 0
    assert "Revoked or lapsed (1)" in gallery.get("/machines").text


def test_revoke_forgets_consent(web, provider, db, gallery):
    connect(web, provider)
    gallery.post(f"/machines/apps/{app_id(db)}/revoke", headers=SAME_ORIGIN)
    # a remembered consent would land straight on a code; now the screen shows again
    page = sign_in(web, provider, begin(web))
    assert 'name="consent"' in page.text


def test_revoke_refuses_a_cross_site_post_and_an_unknown_app(
    web, provider, db, gallery
):
    connect(web, provider)
    ident = app_id(db)
    cross = gallery.post(
        f"/machines/apps/{ident}/revoke", headers={"sec-fetch-site": "cross-site"}
    )
    assert cross.status_code == 403
    with db.connect() as conn:
        assert conn.execute("select revoked_at from mcp_app").fetchone()[0] is None
    gone = gallery.post("/machines/apps/app_nope/revoke", headers=SAME_ORIGIN)
    assert gone.status_code == 404


def test_a_lapsed_app_is_listed_as_lapsed(
    web, provider, db, gallery, capsys, monkeypatch
):
    connect(web, provider)
    past = datetime.now(UTC) - timedelta(days=1)
    with db.connect() as conn:
        conn.execute("update mcp_app set idle_expires_at = %s", (past,))
    page = gallery.get("/machines").text
    assert "Revoked or lapsed (1)" in page and "lapsed</li>" in page
    assert "Revoke</button>" not in page
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    main(["list-apps"])
    assert "\tlapsed\t" in capsys.readouterr().out


def test_admin_lists_and_revokes_an_app(web, provider, db, capsys, monkeypatch):
    pair = connect(web, provider)
    ident = app_id(db)
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    main(["list-apps"])
    out = capsys.readouterr().out
    assert ident in out and "\tlive\t" in out
    assert pair["access_token"] not in out and pair["refresh_token"] not in out
    main(["revoke-app", ident])
    assert at_mcp(web, pair["access_token"]).status_code == 401
    with db.connect() as conn:
        assert (
            conn.execute("select revoked_reason from mcp_app").fetchone()[0] == "admin"
        )
    with pytest.raises(SystemExit):
        main(["revoke-app", ident])
    capsys.readouterr()
    main(["list-apps"])
    assert "\tended\t" in capsys.readouterr().out
