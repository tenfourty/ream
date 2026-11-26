"""Gallery sign-in (people design, slice 4): the gate in front of the gallery host, the sign-in
round trip against the fake OIDC provider, the session and its cookie, and what the gate refuses.
Written against `REAM_GALLERY_SIGN_IN=on`; `test_gallery_sign_in_off.py` holds the other half."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_oidc import ISSUER, provider  # noqa: F401 -- provider is a fixture

from ream import oidc, owner, people, settings, signin
from ream.app import create_app

ORIGIN = "https://artifacts.tenfourty.site"
SAME = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def web(db, provider):
    with db.connect() as conn:  # person #1, as `owner.check` seeds it at start-up
        owner.seed_person(conn, "jeremy")
    with settings.override(gallery_sign_in="on"):
        yield TestClient(create_app(db), base_url=ORIGIN, follow_redirects=False)


def begin(web, next_="/"):
    got = web.get("/signin", params={"next": next_})
    assert got.status_code == 302, got.text
    q = parse_qs(urlsplit(got.headers["location"]).query)
    return got, q["state"][0], q["nonce"][0]


def sign_in(web, provider, next_="/", **claims):
    _, state, nonce = begin(web, next_)
    provider.id_token_override = provider.sign(provider.good(nonce, **claims))
    return web.get("/signin/callback", params={"code": "c", "state": state})


def sessions(db):
    with db.connect() as conn:
        return [r[0] for r in conn.execute("select person from person_session")]


def signed_in(web, provider):
    got = sign_in(web, provider)
    assert got.status_code == 303, got.text
    return got


def test_no_session_a_navigation_is_sent_to_sign_in_and_anything_else_is_refused(web):
    got = web.get("/a/abc?x=1")
    assert got.status_code == 303
    assert got.headers["location"] == "/signin?next=%2Fa%2Fabc%3Fx%3D1"
    # a script's fetch gets an answer it can read, not a redirect to the provider
    fetch = web.get("/a/abc", headers={"sec-fetch-mode": "cors"})
    assert fetch.status_code == 401 and fetch.json() == {"detail": "sign in required"}
    assert web.post("/a/abc/archive", headers=SAME).status_code == 401
    assert "set-cookie" not in got.headers


def test_the_whole_flow_signs_the_owner_in(web, provider, db):
    got = sign_in(web, provider, next_="/inbox")
    assert got.status_code == 303 and got.headers["location"] == "/inbox"
    assert sessions(db) == ["jeremy"]
    page = web.get("/")
    assert page.status_code == 200
    # a signed-in page is never kept by a shared cache, and varies on the cookie
    assert "no-store" in page.headers["cache-control"]
    assert "private" in page.headers["cache-control"]
    assert "cookie" in page.headers["vary"].lower()
    # only the token's hash is stored
    token = web.cookies.get(signin.SESSION_COOKIE)
    with db.connect() as conn:
        stored = [r[0] for r in conn.execute("select token_sha256 from person_session")]
    assert token and token not in stored and len(stored) == 1


def test_the_owner_login_fills_person_one_and_people_current_is_the_owner(
    web,
    provider,
    db,
):
    signed_in(web, provider)
    with db.connect() as conn:
        row = conn.execute(
            "select issuer, subject from person where id = 'jeremy'"
        ).fetchone()
    assert tuple(row) == (ISSUER, "u-1")


def test_cookie_attributes(web, provider):
    got = signed_in(web, provider)
    cookies = got.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(signin.SESSION_COOKIE + "="))
    lower = session.lower()
    for attribute in ("secure", "httponly", "samesite=lax", "path=/", "max-age=43200"):
        assert attribute in lower.split("; "), (attribute, lower)
    assert "domain" not in lower
    # the in-flight cookie, same shape, is cleared at the callback
    cleared = next(c for c in cookies if c.startswith(signin.SIGNIN_COOKIE + "="))
    assert "max-age=0" in cleared.lower()
    begun, _, _ = begin(web)
    flight = begun.headers["set-cookie"].lower()
    for attribute in ("secure", "httponly", "samesite=lax", "path=/", "max-age=600"):
        assert attribute in flight.split("; "), (attribute, flight)
    assert "domain" not in flight


def test_new_session_each_sign_in_and_the_old_one_is_deleted(web, provider, db):
    signed_in(web, provider)
    first = web.cookies.get(signin.SESSION_COOKIE)
    signed_in(web, provider)
    second = web.cookies.get(signin.SESSION_COOKIE)
    assert first and second and first != second
    assert sessions(db) == ["jeremy"]  # the first was ended, not left to expire
    old = TestClient(create_app(db), base_url=ORIGIN, follow_redirects=False)
    old.cookies.set(signin.SESSION_COOKIE, first, domain="artifacts.tenfourty.site")
    assert old.get("/").status_code == 303


def test_supplied_cookie_not_adopted(web, provider, db):
    web.cookies.set(
        signin.SESSION_COOKIE,
        "planted-by-someone-else",
        domain="artifacts.tenfourty.site",
    )
    assert web.get("/").status_code == 303  # not a session
    got = sign_in(web, provider)
    assert got.status_code == 303
    assert web.cookies.get(signin.SESSION_COOKIE) != "planted-by-someone-else"
    with db.connect() as conn:
        count = conn.execute("select count(*) from person_session").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    "header",
    [
        "x-remote-user",
        "remote-user",
        "x-forwarded-user",
        "x-forwarded-email",
        "remote-email",
        "x-authentik-username",
        "x-real-user",
    ],
)
def test_identity_headers_do_not_sign_in(web, header):
    got = web.get("/", headers={header: "jeremy"})
    assert got.status_code == 303 and got.headers["location"].startswith("/signin")


def test_an_expired_session_a_disabled_person_and_a_signed_out_one_are_refused(
    web,
    provider,
    db,
):
    signed_in(web, provider)
    assert web.get("/").status_code == 200
    with db.connect() as conn:
        conn.execute(
            "update person_session set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert web.get("/").status_code == 303
    signed_in(web, provider)
    assert web.get("/").status_code == 200
    with db.connect() as conn:
        conn.execute("update person set disabled_at = %s", (datetime.now(UTC),))
    assert web.get("/").status_code == 303
    with db.connect() as conn:
        conn.execute("update person set disabled_at = null")
    assert web.get("/").status_code == 200


def test_sign_out_ends_the_session_and_clears_the_cookie(web, provider, db):
    signed_in(web, provider)
    page = web.get("/")
    assert 'action="/signout"' in page.text  # the profile menu's form
    got = web.post("/signout", headers=SAME)
    assert got.status_code == 200 and "signed out" in got.text.lower()
    assert 'action="/signout"' not in got.text
    assert "max-age=0" in got.headers["set-cookie"].lower()
    assert sessions(db) == []
    assert web.get("/").status_code == 303


def test_a_cross_site_sign_out_is_refused_and_the_session_stays(web, provider, db):
    signed_in(web, provider)
    got = web.post("/signout", headers={"sec-fetch-site": "cross-site"})
    assert got.status_code == 403
    assert sessions(db) == ["jeremy"]


def _post_routes(db):
    from fastapi.routing import APIRoute

    from ream.gallery import gallery_router

    for route in gallery_router(db).routes:
        if isinstance(route, APIRoute):
            for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
                yield method, re.sub(r"\{[^}]+\}", "x", route.path)


def test_cross_site_post_refused_on_every_post_route(web, provider, db):
    signed_in(web, provider)
    seen = list(_post_routes(db))
    assert len(seen) > 25, "the walk is broken"
    for method, path in seen:
        for headers in (
            {"sec-fetch-site": "cross-site"},
            {"sec-fetch-site": "same-site"},
            {"origin": "https://evil.example"},
            {},  # no Fetch Metadata and no Origin
        ):
            got = web.request(method, path, headers=headers)
            assert got.status_code == 403, (method, path, headers, got.status_code)


def test_other_person_gets_404_on_the_owner_routes(web, db, provider):
    with db.connect() as conn:
        conn.execute(
            "insert into person (id, issuer, subject) values ('other', 'i', 's')"
        )
    token = signin.open_session(db, "other", datetime.now(UTC))
    web.cookies.set(signin.SESSION_COOKIE, token, domain="artifacts.tenfourty.site")
    for path in ("/machines", "/connect"):
        got = web.get(path)
        assert got.status_code == 404, path
    assert web.post("/machines/k/revoke", headers=SAME).status_code == 404


def test_the_gate_leaves_the_agent_and_mcp_and_oauth_doors_to_their_own_checks(web):
    for method, path in (
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/skill.md"),
        ("POST", "/mcp"),
        ("GET", "/oauth/authorize"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/healthz"),
        ("GET", "/favicon.svg"),
    ):
        got = web.request(method, path)
        assert got.status_code != 303, (path, got.status_code)
        assert "sign in required" not in got.text, path


# --- The callback's refusals -----------------------------------------------------------------


def test_an_unknown_or_replayed_state_is_refused(web, provider):
    assert (
        web.get("/signin/callback", params={"code": "c", "state": "nope"}).status_code
        == 400
    )
    assert web.get("/signin/callback").status_code == 400
    _, state, nonce = begin(web)
    provider.id_token_override = provider.sign(provider.good(nonce))
    assert (
        web.get("/signin/callback", params={"code": "c", "state": state}).status_code
        == 303
    )
    again = web.get("/signin/callback", params={"code": "c", "state": state})
    assert again.status_code == 400


def test_a_callback_from_another_browser_is_refused(web, provider, db):
    _, state, nonce = begin(web)
    provider.id_token_override = provider.sign(provider.good(nonce))
    other = TestClient(create_app(db), base_url=ORIGIN, follow_redirects=False)
    got = other.get("/signin/callback", params={"code": "c", "state": state})
    assert got.status_code == 400 and not sessions(db)
    # the row was claimed by the refused try: the right browser cannot use it now
    assert (
        web.get("/signin/callback", params={"code": "c", "state": state}).status_code
        == 400
    )


def test_an_expired_sign_in_is_refused(web, provider, db):
    _, state, nonce = begin(web)
    with db.connect() as conn:
        conn.execute(
            "update person_signin set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    provider.id_token_override = provider.sign(provider.good(nonce))
    assert (
        web.get("/signin/callback", params={"code": "c", "state": state}).status_code
        == 400
    )


def test_the_provider_saying_no_signs_nobody_in(web, db):
    _, state, _ = begin(web)
    got = web.get("/signin/callback", params={"error": "access_denied", "state": state})
    assert got.status_code == 403 and not sessions(db)


def test_a_token_for_a_different_nonce_is_refused(web, provider, db):
    _, state, _ = begin(web)
    provider.id_token_override = provider.sign(provider.good("some-other-nonce"))
    got = web.get("/signin/callback", params={"code": "c", "state": state})
    assert got.status_code == 403 and not sessions(db)


def test_someone_who_is_not_the_owner_is_refused_and_nothing_is_stored(
    web, provider, db
):
    got = sign_in(web, provider, preferred_username="mallory", sub="u-9")
    assert got.status_code == 403 and not sessions(db)
    assert (
        "set-cookie" not in got.headers
        or signin.SESSION_COOKIE + "=" not in got.headers["set-cookie"]
    )
    with db.connect() as conn:
        assert conn.execute("select count(*) from person").fetchone()[0] == 1


def test_the_provider_being_down_is_a_502_and_stores_nothing(web, provider, db):
    provider.down = True
    oidc.clear_caches()
    got = web.get("/signin")
    assert got.status_code == 502
    with db.connect() as conn:
        assert conn.execute("select count(*) from person_signin").fetchone()[0] == 0


def test_person_login_is_told_who_from_the_setting(web, provider, monkeypatch):
    seen = []
    real = oidc.person_login

    def spy(db, claims, *, who="owner"):
        seen.append(who)
        return real(db, claims, who=who)

    monkeypatch.setattr(oidc, "person_login", spy)
    signed_in(web, provider)
    with settings.override(sign_in_who="anyone"):
        signed_in(web, provider)  # people.OPEN_SIGN_IN is False: still only the owner
    assert seen == ["owner", "anyone"]


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("/a/abc", True),
        ("/a/abc?x=1&y=2", True),
        ("/", True),
        (None, False),
        ("", False),
        ("a/b", False),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("/a\\b", False),
        ("https://evil.example/", False),
        ("/\t/evil.example", False),
        ("/a\nb", False),
        ("/signin", False),
        ("/signin/callback?x=1", False),
        ("/signout", False),
        ("/" + "a" * 600, False),
    ],
)
def test_next_is_a_path_on_this_origin_and_nothing_else(raw, kept):
    assert signin.safe_next(raw) == (raw if kept else "/")


def test_an_open_redirect_through_next_lands_on_the_wall(web, provider):
    got = sign_in(web, provider, next_="//evil.example/x")
    assert got.status_code == 303 and got.headers["location"] == "/"


def test_sign_in_starts_are_counted_per_address(web):
    codes = [web.get("/signin").status_code for _ in range(12)]
    assert codes[0] == 302 and codes[-1] == 429


def test_the_redirect_address_is_built_from_settings_not_the_host_header(web):
    got = web.get("/signin", headers={"x-forwarded-host": "evil.example"})
    q = parse_qs(urlsplit(got.headers["location"]).query)
    assert q["redirect_uri"] == [ORIGIN + "/signin/callback"]


def test_the_sweep_drops_expired_rows_and_keeps_live_ones(web, provider, db):
    begin(web)
    signed_in(web, provider)
    now = datetime.now(UTC)
    assert signin.sweep(db, now) == 0
    assert signin.sweep(db, now + timedelta(days=1)) == 2  # one sign-in, one session


# --- Settings --------------------------------------------------------------------------------


def _problems(**more):
    return settings.current().problems() if not more else _with(**more)


def _with(**more):
    with settings.override(**more) as current:
        return current.problems()


def test_gallery_sign_in_needs_the_provider_values_and_says_so(tmp_path):
    found = _with(gallery_sign_in="on")
    for name in (
        "REAM_OIDC_ISSUER",
        "REAM_OIDC_CLIENT_ID",
        "REAM_OIDC_CLIENT_SECRET_FILE",
    ):
        assert f"REAM_GALLERY_SIGN_IN=on needs {name}" in found
    assert not any("REAM_MCP_SIGN_IN" in p for p in found)


def test_both_sign_ins_on_do_not_repeat_a_problem():
    found = _with(gallery_sign_in="on", mcp_sign_in="on", mcp_public_url=ORIGIN)
    assert found.count("REAM_MCP_SIGN_IN=on needs REAM_OIDC_ISSUER") == 1
    assert found.count("REAM_GALLERY_SIGN_IN=on needs REAM_OIDC_ISSUER") == 1


def test_gallery_sign_in_is_on_or_off():
    assert "REAM_GALLERY_SIGN_IN must be on or off" in _with(gallery_sign_in="maybe")
    assert not [p for p in _with(gallery_sign_in="off") if "GALLERY_SIGN_IN" in p]


def test_gallery_sign_in_needs_an_https_gallery_origin_except_on_loopback(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("x\n")
    full = {
        "gallery_sign_in": "on",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "ream",
        "oidc_client_secret_file": str(secret),
    }
    assert _with(**full) == []
    assert any(
        "must be https" in p
        for p in _with(**full, gallery_origin="http://gallery.example")
    )
    assert (
        _with(
            **full,
            gallery_origin="http://localhost:8000",
            content_domain="localhost:8000",
        )
        == []
    )


def test_anyone_is_refused_while_sign_in_is_not_open():
    assert people.OPEN_SIGN_IN is False
    assert any("REAM_SIGN_IN_WHO=anyone" in p for p in _with(sign_in_who="anyone"))
    assert any("REAM_SIGN_IN_WHO must be" in p for p in _with(sign_in_who="everyone"))
    assert not [p for p in _with(sign_in_who="owner") if "SIGN_IN_WHO" in p]


def test_the_defaults_are_off_and_owner():
    s = settings.current()
    assert s.gallery_signs_in() is False and s.sign_in_who == "owner"
