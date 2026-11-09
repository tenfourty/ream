"""Part 5c: the refresh_token grant, revoke, the idle lapse and the cap, the sweep."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

from test_mcp_sign_in import CLAUDE_ID, PING, PUBLIC, stub  # noqa: F401 -- fixture
from test_oauth import exchange, full_flow, web  # noqa: F401 -- fixture
from test_oidc import provider  # noqa: F401 -- fixture

from ream import oauth

FAR = "2100-01-01T00:00:00+00:00"


def tokens(web, provider):
    return exchange(web, full_flow(web, provider)["code"]).json()


def refresh(web, raw, **more):
    data = {"grant_type": "refresh_token", "refresh_token": raw, "client_id": CLAUDE_ID}
    data.update(more)
    return web.post("/oauth/token", data=data)


def at_mcp(web, access):
    return web.post("/mcp", json=PING, headers={"authorization": "Bearer " + access})


def app_row(db):
    with db.connect() as conn:
        return conn.execute("select revoked_at, revoked_reason from mcp_app").fetchone()


def form_for(raw: str) -> dict:
    return {"grant_type": "refresh_token", "refresh_token": raw, "client_id": CLAUDE_ID}


def body(response) -> dict:
    return json.loads(response.body)


def never_expire_access(db):
    """So only the app's own idle lapse and cap decide, not the hour an access token lives."""
    with db.connect() as conn:
        conn.execute(
            "update mcp_token set expires_at = %s where kind = 'access'", (FAR,)
        )


def test_a_refresh_rotates_and_the_new_pair_works(web, provider):
    first = tokens(web, provider)
    got = refresh(web, first["refresh_token"])
    assert got.status_code == 200
    second = got.json()
    assert second["refresh_token"].startswith("rmr_")
    assert second["refresh_token"] != first["refresh_token"]
    assert at_mcp(web, second["access_token"]).status_code == 200


def test_a_spent_refresh_token_ends_the_app(web, provider, db):
    first = tokens(web, provider)
    second = refresh(web, first["refresh_token"]).json()
    assert refresh(web, first["refresh_token"]).status_code == 400
    assert app_row(db)[1] == "refresh_reuse"
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 0
    assert refresh(web, second["refresh_token"]).status_code == 400
    assert at_mcp(web, second["access_token"]).status_code == 401


def test_another_client_or_resource_is_refused_and_does_not_spend(web, provider, db):
    first = tokens(web, provider)
    other = refresh(web, first["refresh_token"], client_id="https://x.example/c")
    assert other.status_code == 400
    bad = refresh(web, first["refresh_token"], resource="https://else.example/mcp")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_target"
    assert app_row(db)[0] is None
    ok = refresh(web, first["refresh_token"], resource=PUBLIC + "/mcp")
    assert ok.status_code == 200


def test_an_access_token_or_junk_is_not_a_refresh_token(web, provider):
    first = tokens(web, provider)
    assert refresh(web, first["access_token"]).status_code == 400
    assert refresh(web, "rmr_nothing").status_code == 400
    assert refresh(web, "").status_code == 400


def test_two_redeemers_at_once_leave_at_most_one_winner(web, provider, db):
    first = tokens(web, provider)["refresh_token"]
    codes: list[int] = []

    def go():
        codes.append(refresh(web, first).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(codes) == 2 and codes.count(200) <= 1


def test_revoking_a_refresh_token_ends_the_app_and_always_answers_200(
    web, provider, db
):
    first = tokens(web, provider)
    assert web.post("/oauth/revoke", data={"token": "rmr_unknown"}).status_code == 200
    assert web.post("/oauth/revoke", data={}).status_code == 200
    assert app_row(db)[0] is None
    done = web.post("/oauth/revoke", data={"token": first["refresh_token"]})
    assert done.status_code == 200
    assert app_row(db)[1] == "revoked"
    assert at_mcp(web, first["access_token"]).status_code == 401
    assert refresh(web, first["refresh_token"]).status_code == 400


def test_revoking_an_access_token_leaves_the_refresh_token(web, provider, db):
    first = tokens(web, provider)
    web.post("/oauth/revoke", data={"token": first["access_token"]})
    assert at_mcp(web, first["access_token"]).status_code == 401
    assert refresh(web, first["refresh_token"]).status_code == 200


def test_another_clients_revoke_does_nothing(web, provider, db):
    first = tokens(web, provider)
    web.post(
        "/oauth/revoke",
        data={"token": first["refresh_token"], "client_id": "https://x.example/c"},
    )
    assert app_row(db)[0] is None


def test_an_idle_app_lapses_and_use_slides_the_window(web, provider, db):
    first = tokens(web, provider)
    never_expire_access(db)
    start = datetime.now(UTC)
    day29 = oauth.token(
        db, form_for(first["refresh_token"]), start + timedelta(days=29)
    )
    assert day29.status_code == 200
    # day 58 is past the first 30-day window but inside the slid one
    day58 = oauth.token(
        db, form_for(body(day29)["refresh_token"]), start + timedelta(days=58)
    )
    assert day58.status_code == 200
    access = body(day58)["access_token"]
    with db.connect() as conn:
        conn.execute(
            "update mcp_token set expires_at = %s where kind = 'access'", (FAR,)
        )
    assert oauth.verify_access(db, access, start + timedelta(days=88)) is not None


def test_an_idle_app_with_no_use_lapses_at_mcp_and_at_refresh(web, provider, db):
    first = tokens(web, provider)
    never_expire_access(db)
    start = datetime.now(UTC)
    day31 = start + timedelta(days=31)
    assert oauth.verify_access(db, first["access_token"], day31) is None
    assert oauth.token(db, form_for(first["refresh_token"]), day31).status_code == 400


def test_the_hard_cap_ends_an_app_however_busy(web, provider, db):
    first = tokens(web, provider)
    start = datetime.now(UTC)
    raw, got = first["refresh_token"], None
    for day in (20, 40, 60, 80):  # busy: a refresh every 20 days
        got = oauth.token(db, form_for(raw), start + timedelta(days=day))
        assert got.status_code == 200
        raw = body(got)["refresh_token"]
    access = body(got)["access_token"]
    never_expire_access(db)
    assert oauth.verify_access(db, access, start + timedelta(days=89)) is not None
    past = start + timedelta(days=91)
    assert oauth.verify_access(db, access, past) is None
    assert oauth.token(db, form_for(raw), past).status_code == 400


def test_a_lapsed_pair_can_be_consented_again(web, provider, db):
    tokens(web, provider)
    with db.connect() as conn:
        conn.execute(
            "update mcp_app set idle_expires_at = %s", ("2000-01-01T00:00:00+00:00",)
        )
    again = tokens(web, provider)
    assert again["access_token"].startswith("rmo_")


def test_the_sweep_drops_finished_rows_and_keeps_live_ones(web, provider, db):
    first = tokens(web, provider)
    refresh(web, first["refresh_token"])  # spends one, leaves a live pair
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_signin").fetchone()[0] == 1
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 4
    assert oauth.sweep(db) == 0  # inside the grace
    far = datetime.now(UTC) + timedelta(days=2)
    assert oauth.sweep(db, far) >= 3
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_signin").fetchone()[0] == 0
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 1
    assert app_row(db)[0] is None


def test_the_sweep_ends_a_lapsed_app_and_its_tokens(web, provider, db):
    tokens(web, provider)
    oauth.sweep(db, datetime.now(UTC) + timedelta(days=100))
    assert app_row(db)[1] == "lapsed"
    with db.connect() as conn:
        assert conn.execute("select count(*) from mcp_token").fetchone()[0] == 0
