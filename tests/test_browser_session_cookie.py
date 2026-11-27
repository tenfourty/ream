"""The gallery's session cookie never reaches the content domain, in a REAL browser.

The gallery host holds the sign-in session; pages are served from their own origin
(`<id>.<content domain>`) so a page's own script can never see it. `__Host-ream_session` has no
Domain attribute, so the browser keeps it host-only: a request to the content host must carry no
cookie at all. A cookie jar's scoping is the browser's, so it is checked in one.

The real flow runs: `/signin` on the gallery, the fake OIDC provider (`test_oidc.provider`, its
`authorize` address answered by the browser's own route), `/signin/callback`, then a page whose
frame is the content host. Every host is routed back to one live server with its own Host header.
The browser does not hand a routed request its cookies, so `_forward` adds the Cookie header the
browser WOULD send, from its jar (`ctx.cookies(url)`), and records it: the gallery's requests
must carry the session (the control: the check can fail), the content host's none.

Needs the system's headless browser; without one it skips, except where REAM_REQUIRE_BROWSER=1
(CI) makes a missing browser fail the run at start (tests/conftest.py).
"""

import os
import re
import urllib.error
import urllib.request
from html import escape
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from playwright import sync_api
from test_client import _LiveServer
from test_oidc import provider  # noqa: F401 -- provider is a fixture

from ream import events, owner, settings, signin, store, thumbs
from ream.app import create_app

pytestmark = [
    pytest.mark.skipif(
        not thumbs.available(),
        reason="no headless browser: set REAM_BROWSER to a chrome binary, or put "
        "chromium on PATH; these browser tests do NOT run without one",
    ),
    pytest.mark.slow(
        reason="a real browser against a live server: seconds, not milliseconds"
    ),
]

GALLERY_HOST = "gallery.tenfourty.site"
GALLERY = f"https://{GALLERY_HOST}"
# A sibling of the gallery host under the SAME registrable domain: a session cookie scoped to the
# parent domain (`Domain=tenfourty.site`) WOULD be sent to `<id>.tenfourty.site`, so a leak fails.
CONTENT_DOMAIN = "tenfourty.site"
PAGE = (
    "<!doctype html><html><head><title>B</title></head><body><p>hello</p></body></html>"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # hand the redirect to the browser as it was sent


@pytest.fixture()
def served(db, provider, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", CONTENT_DOMAIN)
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", GALLERY)
    with db.connect() as conn:  # person #1, as `owner.check` seeds it at start-up
        owner.seed_person(conn, "jeremy")
    server = _LiveServer(create_app(db))
    live = server.start()
    page = store.publish(db, "seat", "Board", PAGE)
    with settings.override(gallery_sign_in="on"):
        try:
            yield live, page.id
        finally:
            # The page holds a "has this been republished" request open; end it as
            # SIGTERM would, or the server's stop finds it still running.
            events.begin_shutdown()
            try:
                server.stop()
            finally:
                events.clear_shutdown()


@pytest.fixture()
def browser():
    with sync_api.sync_playwright() as p:
        # CI's job container runs as root, where chromium refuses to start without
        # --no-sandbox; a person's shell is not root and keeps the sandbox.
        b = p.chromium.launch(
            executable_path=thumbs.browser_path(),
            args=["--no-sandbox"] if os.geteuid() == 0 else [],
        )
        try:
            yield b
        finally:
            b.close()


def _is_content(host):
    return host != GALLERY_HOST and host.endswith("." + CONTENT_DOMAIN)


def _forward(ctx, live, sent, provider):
    """Serve `https://<host>/...` from the live server with that Host, the browser's own
    headers and the Cookie header its jar holds for the URL; record (host, cookie) in `sent`.
    The jar's cookies for a URL are added whatever their SameSite: the gallery and the content host
    are the same site, so a Lax cookie would be sent to both anyway; only Domain and the
    `__Host-` prefix keep it off the content host, and those are what the test checks."""
    opener = urllib.request.build_opener(_NoRedirect)

    def handle(route):
        req = route.request
        # The page's two long polls would hold this (single) handler thread for their whole
        # wait, and say nothing about cookies: refuse them.
        if urlsplit(req.url).path.endswith(("/current", "/comments-live")):
            route.abort()
            return
        host = urlsplit(req.url).hostname
        headers = {k: v for k, v in req.all_headers().items() if not k.startswith(":")}
        headers.pop("cookie", None)
        jar = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(req.url))
        if jar:
            headers["cookie"] = jar
        sent.append((host, jar))
        headers["host"] = host
        if req.resource_type == "document" and _is_content(host):
            headers["sec-fetch-dest"] = (
                "iframe"  # added by the network stack, absent here
            )
        path = req.url.split("/", 3)[3] if req.url.count("/") >= 3 else ""
        fwd = urllib.request.Request(live + "/" + path, headers=headers)  # noqa: S310
        try:
            got = opener.open(fwd, timeout=10)
        except urllib.error.HTTPError as err:
            got = err
        with got:
            status = getattr(got, "status", None) or got.code
            out = {}
            for key, value in got.headers.items():
                # several Set-Cookie lines travel as one value, newline-separated
                out[key] = out[key] + "\n" + value if key in out else value
            body = got.read()
            where = out.get("location")
            if where is not None:
                # Playwright does not route the hop after a fulfilled redirect, so a redirect
                # becomes a page that makes the same hop (Set-Cookie kept: the browser's jar
                # takes it as it would from the redirect itself).
                if where.startswith("https://auth.example/"):
                    where = _provider_answers(provider, where)
                out = {k: v for k, v in out.items() if k == "set-cookie"}
                out["content-type"] = "text/html"
                status = 200
                body = (
                    f'<!doctype html><meta http-equiv="refresh" content="0;url={escape(where)}">'
                ).encode()
            route.fulfill(status=status, headers=out, body=body)

    return handle


def _provider_answers(provider, location):
    """Where the browser goes when the gallery sends it to the provider's `authorize` address:
    the provider signs the token for the request's nonce and sends it back with a code, as a
    real one does. (Playwright does not route the hop after a fulfilled redirect, so the
    forwarder asks for it here, at the redirect.)"""
    q = parse_qs(urlsplit(location).query)
    provider.id_token_override = provider.sign(provider.good(q["nonce"][0]))
    return f"{q['redirect_uri'][0]}?code=c&state={quote(q['state'][0])}"


def test_the_session_cookie_never_reaches_the_content_domain(served, provider, browser):
    live, ident = served
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    sent: list[tuple[str, str]] = []
    ctx.route(
        re.compile(rf"^https://{re.escape(GALLERY_HOST)}/"),
        _forward(ctx, live, sent, provider),
    )
    ctx.route(
        re.compile(
            rf"^https://(?!{re.escape(GALLERY_HOST)}/)[^/]+\.{re.escape(CONTENT_DOMAIN)}/"
        ),
        _forward(ctx, live, sent, provider),
    )
    pg = ctx.new_page()

    pg.goto(f"{GALLERY}/signin?next={quote(f'/a/{ident}')}")
    pg.wait_for_url(f"{GALLERY}/a/{ident}")  # through the provider and the callback
    pg.wait_for_load_state("load")
    assert pg.url == f"{GALLERY}/a/{ident}", (
        f"sign-in did not land on the page: {pg.url}"
    )
    assert pg.locator("iframe").count() == 1

    # The session is in the browser, host-only on the gallery host...
    session = [c for c in ctx.cookies() if c["name"] == signin.SESSION_COOKIE]
    assert len(session) == 1 and session[0]["domain"] == GALLERY_HOST
    assert session[0]["secure"] and session[0]["httpOnly"]
    assert not ctx.cookies(f"https://{ident}.{CONTENT_DOMAIN}/")
    assert not ctx.cookies(f"https://{CONTENT_DOMAIN}/")

    # ...the frame's document was fetched from the content host...
    frame = next(f for f in pg.frames if f != pg.main_frame)
    frame.wait_for_load_state("load")
    assert frame.url.startswith(f"https://{ident}.{CONTENT_DOMAIN}/")
    assert "hello" in frame.content()
    content = [jar for host, jar in sent if _is_content(host)]
    assert content, "no request reached the content host: the check proved nothing"

    # ...and none of those requests carried the session, or any cookie. The gallery's did
    # (the control: a check that cannot fail proves nothing).
    assert all(signin.SESSION_COOKIE not in jar for jar in content), content
    assert all(jar == "" for jar in content), content
    gallery = [jar for host, jar in sent if host == GALLERY_HOST]
    assert any(signin.SESSION_COOKIE in jar for jar in gallery), gallery
    ctx.close()
