"""With `REAM_GALLERY_SIGN_IN` unset the gallery is as it was (people design, slice 4): the gate
is a pass-through, no response carries a new header or cookie, no page a new form, and `/signin`,
`/signin/callback` and `/signout` answer exactly as a path that does not exist."""

import pytest
from fastapi.testclient import TestClient

from ream import gallery, settings, signin, store
from ream.app import create_app

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
H = {"sec-fetch-site": "same-origin"}


@pytest.fixture(params=["unset", "off"])
def web(request, db):
    if request.param == "off":
        with settings.override(gallery_sign_in="off"):
            yield TestClient(create_app(db), follow_redirects=False)
    else:
        yield TestClient(create_app(db), follow_redirects=False)


def _url(path):
    return "https://artifacts.tenfourty.site" + path


@pytest.mark.parametrize(
    "path", ["/signin", "/signin/callback", "/signout", "/signin/x"]
)
@pytest.mark.parametrize("method", METHODS)
def test_the_sign_in_paths_answer_as_a_path_that_does_not_exist(web, path, method):
    got = web.request(method, _url(path), headers=H)
    ref = web.request(method, _url("/no-such-path-at-all"), headers=H)
    assert got.status_code == ref.status_code == 404
    assert got.content == ref.content
    assert dict(got.headers) == dict(ref.headers)


def test_no_new_header_cookie_or_form_on_a_page(web, db):
    page = store.publish(db, "seat", "Board", "<p>hello</p>")
    for path in (
        "/",
        f"/a/{page.id}",
        "/inbox",
        "/machines",
        "/healthz",
        "/favicon.svg",
    ):
        got = web.get(_url(path))
        assert got.status_code == 200, path
        assert "set-cookie" not in got.headers, path
        assert "vary" not in got.headers, path
        assert "signout" not in got.text and "signin" not in got.text, path
        assert "private, no-store" not in got.headers.get("cache-control", ""), path
    # the profile menu is the bytes it always was
    assert gallery._PROFILE in web.get(_url("/")).text
    assert gallery._profile() == gallery._PROFILE


def test_a_session_cookie_means_nothing_while_it_is_off(web, db):
    web.cookies.set(
        signin.SESSION_COOKIE, "anything", domain="artifacts.tenfourty.site"
    )
    assert web.get(_url("/")).status_code == 200


def test_the_owner_is_still_the_person_and_no_identity_header_changes_it(web, db):
    for headers in ({}, {"x-remote-user": "mallory"}, {"remote-user": "mallory"}):
        assert web.get(_url("/"), headers=headers).status_code == 200


def test_the_gate_does_not_touch_an_agent_or_unknown_route(web):
    assert web.get(_url("/api/v1/me")).status_code in (401, 403)
    assert web.get(_url("/no-such-path")).status_code == 404
