"""Gallery sign-in (people design, slice 4; docs/agent/runbooks/gallery-sign-in.md).

With `REAM_GALLERY_SIGN_IN=on` a gate in front of the whole gallery app refuses every request
without a live session, except the fixed open list below, and `people.current` answers the person
the gate found. With it unset the gate passes everything through and `/signin*` and `/signout`
answer the 404 of a path that does not exist: no new header, cookie, form or route is seen.

The gate fails CLOSED: it does not rely on a route depending on `people.current`. What it lets
through unsigned is `OPEN_PATHS` and `OPEN_PREFIXES`, each there for a reason given beside it, and
`tests/test_gate_covers_every_route.py` fails for a route nobody has classified.

The flow reuses the MCP sign-in's provider client (`oidc.begin`, `oidc.exchange`) and nothing
else of it: its own in-flight table (`person_signin`), its own cookies, its own session table
(`person_session`). The redirect address is built from settings, never from the Host header. The
in-flight row is claimed once by its state's hash and tied to the browser by a cookie's hash; a
new session token is minted at every callback and the one the request carried is deleted.
`oidc.begin`/`exchange` block, so the handlers are plain `def` (run in the threadpool).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ream import oidc, people, ratelimit, settings
from ream.db import Database

log = logging.getLogger("ream.signin")

# `__Host-`: Secure, Path=/, no Domain, so the browser keeps it host-only and a content
# subdomain cannot plant one. Only these exact names are ever read.
SESSION_COOKIE = "__Host-ream_session"
SIGNIN_COOKIE = "__Host-ream_signin"
SESSION_LASTS = timedelta(hours=12)  # absolute, not renewed by use
SIGN_IN_LASTS = timedelta(minutes=10)
TOUCH_EVERY = timedelta(minutes=5)
CALLBACK_PATH = "/signin/callback"
MAX_NEXT = 512
_NO_STORE = {"cache-control": "private, no-store", "pragma": "no-cache"}

# What the gate lets through with no session, and why. Exact paths:
OPEN_PATHS = frozenset(
    {
        "/healthz",  # the health check
        "/signin",  # begins a sign-in
        CALLBACK_PATH,  # the provider's return
        "/favicon.svg",  # the tab icon, asked for before anyone is signed in
        "/mcp",  # MCP: its own credential (an agent key or an `rmo_` token), never a cookie
    }
)
# ...and prefixes:
OPEN_PREFIXES = (
    "/api/v1/",  # the agent API: an agent key in `Authorization`, never a cookie
    "/.well-known/",  # discovery documents
    "/oauth/",  # the MCP authorization server: its own sign-in and consent
)

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_SAFE = ("GET", "HEAD", "OPTIONS")


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def callback_uri() -> str:
    """Built from settings only: a spoofed Host header changes nothing here."""
    return settings.mcp_origin(settings.current().gallery_origin) + CALLBACK_PATH


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_next(raw: str | None) -> str:
    """Where to go after signing in: a path on this origin and nothing else (starts with one `/`,
    no backslash, no control character, not the sign-in's own paths), else `/`."""
    if not raw or len(raw) > MAX_NEXT or not raw.startswith("/"):
        return "/"
    if raw.startswith("//") or any(
        c == "\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in raw
    ):
        return "/"
    path = raw.split("?", 1)[0]
    if path in ("/signin", "/signout") or path.startswith("/signin/"):
        return "/"
    return raw


# --- Sessions ---------------------------------------------------------------------------------


def open_session(db: Database, person_id: str, now: datetime) -> str:
    """A new session for `person_id`; returns the cookie's token (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "insert into person_session (token_sha256, person, created_at, expires_at)"
            " values (%s, %s, %s, %s)",
            (_hash(token), person_id, now, now + SESSION_LASTS),
        )
    return token


def session_person(
    db: Database, token: str | None, now: datetime
) -> people.Person | None:
    """The person a live session belongs to, or None: no token, an unknown or expired one, or
    a disabled person. Touches `last_seen_at` at most every `TOUCH_EVERY`."""
    if not token:
        return None
    hashed = _hash(token)
    with db.connect() as conn:
        row = conn.execute(
            "select p.id, p.name from person_session s join person p on p.id = s.person"
            " where s.token_sha256 = %s and s.expires_at > %s and p.disabled_at is null",
            (hashed, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "update person_session set last_seen_at = %s where token_sha256 = %s"
            " and (last_seen_at is null or last_seen_at < %s)",
            (now, hashed, now - TOUCH_EVERY),
        )
    return people.person_from_row(row)


def end_session(db: Database, token: str | None) -> None:
    if not token:
        return
    with db.connect() as conn:
        conn.execute(
            "delete from person_session where token_sha256 = %s", (_hash(token),)
        )


def sweep(db: Database, now: datetime | None = None) -> int:
    """Drop sign-ins in flight and sessions that have expired."""
    now = now or datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        n = conn.execute(
            "delete from person_signin where expires_at < %s", (now,)
        ).rowcount
        n += conn.execute(
            "delete from person_session where expires_at < %s", (now,)
        ).rowcount
    return n


# --- Pages and cookies ------------------------------------------------------------------------


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    from ream import gallery

    return HTMLResponse(
        gallery._shell(title, body, home=False, profile=False),
        status_code=status,
        headers={**gallery.gallery_headers(), **_NO_STORE},
    )


def _error_page(words: str, status: int = 400) -> HTMLResponse:
    return _page(
        "Cannot sign in",
        f'<p>{escape(words)}</p><p><a href="/signin">Try again</a></p>',
        status,
    )


def _set_cookie(response: Response, name: str, value: str, lasts: timedelta) -> None:
    response.set_cookie(
        name,
        value,
        max_age=int(lasts.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


# --- The flow ---------------------------------------------------------------------------------


def start(db: Database, request: Request) -> Response:
    """Step one: a row in flight, the browser cookie, and the provider's address (302)."""
    now = datetime.now(UTC)
    next_path = safe_next(request.query_params.get("next"))
    try:
        begun = oidc.begin(callback_uri())
    except oidc.SignInRefused as exc:
        # The provider is down or misconfigured: its words stay in the log.
        log.warning("gallery sign-in: the provider refused: %s", exc)
        return _error_page(
            "Sign-in is unavailable: the sign-in provider could not be reached."
            " Try again later.",
            502,
        )
    browser = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "insert into person_signin (id, state_sha256, browser_sha256, oidc_nonce,"
            " oidc_verifier, next_path, created_at, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                secrets.token_urlsafe(18),
                _hash(begun.state),
                _hash(browser),
                begun.nonce,
                begun.verifier,
                next_path,
                now,
                now + SIGN_IN_LASTS,
            ),
        )
    out = RedirectResponse(begun.url, status_code=302, headers=_NO_STORE)
    _set_cookie(out, SIGNIN_COOKIE, browser, SIGN_IN_LASTS)
    return out


def _claim(db: Database, state: str, now: datetime):
    """Take the in-flight row whose state hash is `state`, clearing the hash in the same
    statement so it can be taken once. The row, or None."""
    with db.connect() as conn:
        return conn.execute(
            "update person_signin set state_sha256 = null"
            " where state_sha256 = %s and expires_at > %s"
            " returning id, browser_sha256, oidc_nonce, oidc_verifier, next_path",
            (_hash(state), now),
        ).fetchone()


def callback(db: Database, request: Request) -> Response:
    """Step two: the person is back. Claims the row, checks the browser cookie, exchanges the
    code, decides who it is, ends any session the request carried and mints a new one (303)."""
    q = request.query_params
    now = datetime.now(UTC)
    given = q.get("state", "")
    claimed = _claim(db, given, now) if given else None
    if claimed is None:
        return _error_page(
            "This sign-in is unknown, used already or too old. Start again."
        )
    row_id, browser, nonce, verifier, next_path = claimed
    got = request.cookies.get(SIGNIN_COOKIE)
    if not got or not hmac.compare_digest(_hash(got).encode(), browser.encode()):
        return _error_page("This sign-in was not started in this browser. Start again.")
    with db.connect() as conn:
        conn.execute("delete from person_signin where id = %s", (row_id,))
    if q.get("error") or not q.get("code"):
        return _error_page("The sign-in was not completed.", 403)
    try:
        claims = oidc.exchange(q["code"], verifier, nonce, callback_uri())
        login = oidc.person_login(db, claims, who=settings.current().sign_in_who)
    except oidc.SignInRefused as exc:
        return _error_page(str(exc) or "Sign-in was refused.", 403)
    with db.connect() as conn:
        found = conn.execute(
            "select disabled_at from person where id = %s", (login.person,)
        ).fetchone()
    if found is None or found[0] is not None:
        return _error_page("This account may not sign in to this Ream.", 403)
    end_session(db, request.cookies.get(SESSION_COOKIE))
    out = RedirectResponse(safe_next(next_path), status_code=303, headers=_NO_STORE)
    _set_cookie(out, SESSION_COOKIE, open_session(db, login.person, now), SESSION_LASTS)
    _clear_cookie(out, SIGNIN_COOKIE)
    return out


def sign_out(db: Database, request: Request) -> Response:
    """End the request's session and say so (200, not a redirect: the CSP's `form-action 'self'`
    blocks a redirect out of a form post, and a redirect to sign in would leave for the provider)."""
    end_session(db, request.cookies.get(SESSION_COOKIE))
    out = _page(
        "Signed out",
        '<p>You are signed out.</p><p><a href="/signin">Sign in again</a></p>',
    )
    _clear_cookie(out, SESSION_COOKIE)
    return out


# --- Routes -----------------------------------------------------------------------------------


def signin_router(
    db: Database, limiter: ratelimit.FailedAuthLimiter | None = None
) -> APIRouter:
    """`/signin`, `/signin/callback`, `/signout`. Registered always, for every method, and each
    answers the 404 (or 405) of a path that does not exist while sign-in is off."""
    router = APIRouter()
    limiter = limiter or ratelimit.FailedAuthLimiter()

    def check(request: Request, method: str) -> None:
        if not settings.current().gallery_signs_in():
            raise HTTPException(404, "Not Found")
        if request.method != method:
            raise HTTPException(405, "Method Not Allowed", headers={"Allow": method})

    def counted(request: Request) -> Response | None:
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        _count, blocked, _newly = limiter.failed(address)  # every call counts
        if blocked:
            return _error_page(
                "Too many sign-ins from this address. Wait a few minutes.", 429
            )
        return None

    @router.api_route("/signin", methods=_ALL_METHODS, include_in_schema=False)
    def signin_route(request: Request):
        check(request, "GET")
        return counted(request) or start(db, request)

    @router.api_route(CALLBACK_PATH, methods=_ALL_METHODS, include_in_schema=False)
    def callback_route(request: Request):
        check(request, "GET")
        return counted(request) or callback(db, request)

    @router.api_route("/signout", methods=_ALL_METHODS, include_in_schema=False)
    def signout_route(request: Request):
        check(request, "POST")
        return sign_out(db, request)

    return router


# --- The gate ---------------------------------------------------------------------------------


def _sign_in_address(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return "/signin?" + urlencode({"next": safe_next(target)})


def _refuse(request: Request) -> Response:
    """No live session. A browser navigation is sent to sign in (303); anything else (a script's
    fetch, a form post, another method) is a 401 it can read."""
    if (
        request.method in ("GET", "HEAD")
        and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
    ):
        return RedirectResponse(
            _sign_in_address(request), status_code=303, headers=_NO_STORE
        )
    return JSONResponse(
        {"detail": "sign in required"}, status_code=401, headers=_NO_STORE
    )


def install_gate(app: FastAPI, db: Database) -> None:
    """Put the gate in front of `app`. Call it before any middleware that should wrap it (the
    access log). While `REAM_GALLERY_SIGN_IN` is off it only calls on."""

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        if not settings.current().gallery_signs_in() or is_open(request.url.path):
            return await call_next(request)
        person = await run_in_threadpool(
            session_person, db, request.cookies.get(SESSION_COOKIE), datetime.now(UTC)
        )
        if person is None:
            return _refuse(request)
        if request.method not in _SAFE:
            # The same check the gallery's own forms make, so a write route that forgot it
            # still fails closed. (Raised here it would be a 500: this sits outside the
            # exception handlers.)
            from ream import gallery

            try:
                gallery._same_origin_write(request)
            except HTTPException as refused:
                return JSONResponse(
                    {"detail": refused.detail},
                    status_code=refused.status_code,
                    headers=_NO_STORE,
                )
        request.state.ream_person = person
        response = await call_next(request)
        # Never in a shared cache: the page is this person's. A response that is already
        # private (the icon, a thumbnail) keeps its own, longer policy.
        if "private" not in response.headers.get("cache-control", ""):
            response.headers["cache-control"] = "private, no-store"
        vary = response.headers.get("vary")
        if not vary or "cookie" not in vary.lower():
            response.headers["vary"] = f"{vary}, Cookie" if vary else "Cookie"
        return response
