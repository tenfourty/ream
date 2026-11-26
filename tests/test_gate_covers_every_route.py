"""The gate fails CLOSED (people design, slice 4). Every route the gallery app serves is listed
here, open or gated, so a new route fails this test until someone classifies it; and a request
with no session, to any path (a served one, an unserved one, or one dressed as an open one), is
refused unless the path is exactly on the open list."""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ream import settings, signin
from ream.app import create_gallery_app

# The routes that answer without a session, each with its own check (see signin.OPEN_PATHS).
OPEN = {
    "/healthz",
    "/favicon.svg",
    "/signin",
    "/signin/callback",
    "/mcp",
    # `/api/v1/` (the agent API, an agent key), `/.well-known/` and `/oauth/` are prefixes:
    # their routes are listed below by prefix so a new one under them is a conscious act.
}
OPEN_PREFIXED = {
    "/.well-known/agent.json",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/authorize",
    "/oauth/callback",
    "/oauth/register",
    "/oauth/revoke",
    "/oauth/token",
}
# Everything else under /api/v1/ is the agent API; counted, not listed, because every route
# there reads `Authorization` and never a cookie (tests/test_api_never_reads_cookies below).
GATED = {
    "/",
    "/a/{artifact_id}",
    "/a/{artifact_id}/agents",
    "/a/{artifact_id}/archive",
    "/a/{artifact_id}/comments-live",
    "/a/{artifact_id}/current",
    "/a/{artifact_id}/delete",
    "/a/{artifact_id}/destroy",
    "/a/{artifact_id}/duplicate",
    "/a/{artifact_id}/folder",
    "/a/{artifact_id}/forms.json",
    "/a/{artifact_id}/forms/{form}",
    "/a/{artifact_id}/frame",
    "/a/{artifact_id}/move",
    "/a/{artifact_id}/pin",
    "/a/{artifact_id}/rename",
    "/a/{artifact_id}/restore",
    "/a/{artifact_id}/share",
    "/a/{artifact_id}/share/email",
    "/a/{artifact_id}/share/email/{share_id}/withdraw",
    "/a/{artifact_id}/share/stop",
    "/a/{artifact_id}/stats",
    "/c/{artifact_id}",
    "/c/{artifact_id}/image",
    "/c/{comment_id}/delete",
    "/c/{comment_id}/reopen",
    "/c/{comment_id}/resolve",
    "/c/{comment_id}/restore",
    "/comments.js",
    "/connect",
    "/connect.js",
    "/connect/approve",
    "/connect/deny",
    "/d/{decision_id}/answer",
    "/d/{decision_id}/mode",
    "/f/{folder_id}",
    "/f/{folder_id}/archive",
    "/f/{folder_id}/rename",
    "/forms.js",
    "/gallery.js",
    "/inbox",
    "/live.js",
    "/machines",
    "/machines/apps/{app_id}/revoke",
    "/machines/{key_id}/rename",
    "/machines/{key_id}/revoke",
    "/pq/{question_id}/answer",
    "/select",
    "/signout",
    "/thumb/{artifact_id}.png",
}
OPEN_API_ROUTES_AT_LEAST = 100


def _walk(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


@pytest.fixture()
def app(db):
    return create_gallery_app(db)


def _routes(app):
    found = list(_walk(app.router.routes))
    assert len(found) > 150, "the walk is broken"
    return found


def test_every_route_is_an_api_route(app):
    """A mount, a websocket or a bare Starlette route would bypass the HTTP middleware's
    assumptions: none exists, and one fails here until the gate is taught about it."""
    odd = [r for r in _routes(app) if not isinstance(r, APIRoute)]
    assert odd == []


def test_every_route_is_classified(app):
    paths = {r.path for r in _routes(app)}
    api = {p for p in paths if p.startswith("/api/v1/")}
    rest = paths - api
    assert {
        p for p in rest if signin.is_open(p) and not p.startswith(signin.OPEN_PREFIXES)
    } == OPEN
    assert {p for p in rest if p.startswith(signin.OPEN_PREFIXES)} == OPEN_PREFIXED
    assert {p for p in rest if not signin.is_open(p)} == GATED
    assert len(api) >= OPEN_API_ROUTES_AT_LEAST
    # the open list names nothing the app does not serve, so a typo cannot open a path
    assert signin.OPEN_PATHS <= paths


def test_the_open_list_is_exactly_this():
    assert signin.OPEN_PATHS == {
        "/healthz",
        "/signin",
        "/signin/callback",
        "/favicon.svg",
        "/mcp",
    }
    assert signin.OPEN_PREFIXES == ("/api/v1/", "/.well-known/", "/oauth/")


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_no_session_every_gated_route_is_refused_on_every_method(app, db):
    with settings.override(gallery_sign_in="on"):
        client = TestClient(
            app, base_url="https://artifacts.tenfourty.site", follow_redirects=False
        )
        checked = 0
        for route in _routes(app):
            if signin.is_open(route.path):
                continue
            for method in sorted(route.methods):
                got = client.request(method, _fill(route.path))
                want = 303 if method in ("GET", "HEAD") else 401
                assert got.status_code == want, (method, route.path, got.status_code)
                checked += 1
        assert checked > 60


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/api/v1",  # the prefix without its slash
        "/api/v1x",
        "//api/v1/me",
        "/mcp/",
        "/mcp/x",
        "/signin/",
        "/signin/callback/",
        "/signinx",
        "/healthz/",
        "/healthz/x",
        "/favicon.svg/",
        "/oauth",
        "/oauthx/token",
        "/.well-known",
        "/.well-knownx/a",
        "/a/%2e%2e/api/v1/me",
        "/ api/v1/me",
        "/API/V1/me",
        "/Signin",
    ],
)
def test_a_path_dressed_as_an_open_one_is_gated(app, path):
    with settings.override(gallery_sign_in="on"):
        client = TestClient(
            app, base_url="https://artifacts.tenfourty.site", follow_redirects=False
        )
        got = client.get(path)
        assert got.status_code == 303, (path, got.status_code)
        assert got.headers["location"].startswith("/signin?next=")


def test_the_api_reads_no_cookie():
    """The open `/api/v1/` prefix is safe because nothing under it takes a cookie."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ream"
    for name in ("api.py", "mcp.py"):
        text = (src / name).read_text()
        assert not re.search(r"\.cookies\b|\bCookie\(|\"cookie\"", text), name
