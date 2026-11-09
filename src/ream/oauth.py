"""The authorization server up to the first tokens (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
sections 9 and 10): `/oauth/authorize`, the sign-in round trip, `/oauth/callback`, the consent
screen, the one-time code and `/oauth/token` for the authorization_code grant, and the
`rmo_` access token `/mcp` takes.

Everything in flight lives in `mcp_signin` (database, not memory: more than one process runs).
Secrets there are hashes; the upstream state is single-use and tied to the browser that began the
sign-in by a cookie. The redirect address given to the OIDC provider is built from settings,
never from the request. oidc.begin/finish are blocking, so every handler here is a plain `def`
(run in the threadpool). Not here (slice 5c): refresh, revoke, lapse, clean-up."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ream import db as dbmod
from ream import hosts, mcp_apps, mcp_clients, oidc, ratelimit, settings
from ream.db import Database

ACCESS_PREFIX = "rmo_"
REFRESH_PREFIX = "rmr_"
CALLBACK_PATH = "/oauth/callback"
COOKIE = "ream_oauth"
SIGN_IN_LASTS = timedelta(minutes=10)
CODE_LASTS = timedelta(seconds=60)
TOUCH_EVERY = timedelta(minutes=5)
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SCOPES = {"ream:read": "read", "ream:write": "write", "ream:relay": "relay"}
_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}
MAX_FIELD = 512  # state and scope, as sent to /oauth/authorize
MAX_FORM = 64 * 1024  # a form body at /oauth/token or the consent post


async def _read_form(request: Request):
    """The form of `request`, refusing a body past MAX_FORM before it is parsed. Returns the
    form, or None when the body is too big or its Content-Length is not a number."""
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > MAX_FORM:
            return None
    except ValueError:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FORM:
            return None
        chunks.append(chunk)
    request._body = b"".join(chunks)
    return await request.form()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())  # type: ignore[union-attr]


def new_token(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(43))


def issuer() -> str:
    return settings.mcp_origin(settings.current().mcp_public_url)


def resource() -> str:
    return issuer() + "/mcp"


def callback_uri() -> str:
    """Built from settings only: a spoofed Host header changes nothing here."""
    return settings.mcp_origin(settings.current().gallery_origin) + CALLBACK_PATH


def requested_permissions(scope: str) -> list[str]:
    """The permissions a `scope` string asks for; an empty one asks for all three."""
    words = scope.split()
    if not words:
        return list(mcp_apps.PERMISSIONS)
    return [p for s, p in SCOPES.items() if s in words]


# --- Pages ------------------------------------------------------------------------------------


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    from ream import gallery

    headers = {**gallery.gallery_headers(), **_NO_STORE}
    return HTMLResponse(
        gallery._shell(title, body, home=False), status_code=status, headers=headers
    )


def _error_page(words: str, status: int = 400) -> HTMLResponse:
    return _page("Cannot sign in", f"<p>{escape(words)}</p>", status)


def _interstitial(target: str, params: dict[str, str]) -> HTMLResponse:
    """A 200 page that moves the browser on: the gallery CSP's `form-action 'self'` blocks a
    302 out of a form post (spec 9)."""
    sep = "&" if "?" in target else "?"
    url = target + sep + urlencode(params)
    link = escape(url, quote=True)
    page = _page(
        "Returning to your app",
        f"<p>Returning to <code>{escape(target)}</code>. If nothing happens, "
        f'<a href="{link}">continue</a>.</p>',
    )
    page.body = page.body.replace(
        b"<head>",
        f'<head><meta http-equiv="refresh" content="0;url={link}">'.encode(),
        1,
    )
    page.headers["content-length"] = str(len(page.body))
    return page


def _redirect_error(
    uri: str, error: str, state: str | None, note: str = ""
) -> HTMLResponse:
    params = {"error": error, "iss": issuer()}
    if state:
        params["state"] = state
    if note:
        params["error_description"] = note
    return _interstitial(uri, params)


def _consent_page(
    client: mcp_clients.Client, nonce: str, redirect: str, wanted: list[str]
) -> HTMLResponse:
    cfg = settings.current()
    host = escape(client.host)
    label = "Verified" if client.verified else "Unverified"
    if client.kind == "registered" and mcp_clients.is_loopback_redirect(redirect):
        host = "An app on this computer"
    name = escape(client.name) or "an app"
    warn = (
        ""
        if client.verified
        else "<p><strong>Continue only if you started this sign-in from that app"
        " yourself.</strong></p>"
    )
    words = {
        "read": "Read pages, comments and questions",
        "write": f"Publish, comment and ask, signed “{escape(cfg.owner_name)}, via {escape(client.host)}”",
        "relay": "Pass on your answers to questions put to you",
    }
    boxes = "".join(
        f'<p><label><input type="checkbox" name="perm" value="{p}"'
        f"{' checked' if p in wanted else ''}> {words[p]}</label></p>"
        for p in mcp_apps.PERMISSIONS
    )
    body = (
        f"<h2>{host} · {label}</h2><p>calls itself “{name}”</p>"
        f"<p>wants to act as <strong>{escape(cfg.owner_name)}</strong> on Ream.</p>"
        f"<p>It will send you back to <code>{escape(redirect)}</code>.</p>{warn}"
        f'<form method="post" action="/oauth/authorize">'
        f'<input type="hidden" name="consent" value="{escape(nonce, quote=True)}">'
        f"{boxes}"
        '<p><button name="decision" value="allow">Allow</button> '
        '<button name="decision" value="deny">Deny</button></p></form>'
    )
    return _page("Connect an app", body)


# --- Rows -------------------------------------------------------------------------------------


def _claim(db: Database, column: str, value: str, now: datetime, extra: str = ""):
    """Take the sign-in row whose `column` hash is `value`, clearing the column in the same
    statement so it can be taken once. The row, or None."""
    with db.connect() as conn:
        return conn.execute(
            f"update mcp_signin set {column} = null"  # noqa: S608 -- fixed column names
            f" where {column} = %s and expires_at > %s {extra}"
            " returning id, client_id, redirect_uri, code_challenge, resource, scope,"
            " client_state, browser_sha256, oidc_nonce, oidc_verifier, person, groups",
            (_hash(value), now),
        ).fetchone()


_FIELDS = [
    "id",
    "client_id",
    "redirect_uri",
    "challenge",
    "resource",
    "scope",
    "client_state",
    "browser",
    "nonce",
    "verifier",
    "person",
    "groups",
]


def _row(row) -> dict[str, Any]:
    return dict(zip(_FIELDS, row, strict=True))


def _gallery_here(request: Request) -> bool:
    return hosts.is_gallery(hosts.normalise(hosts.first_host_header(request.scope)))


def _public_here(request: Request) -> bool:
    public = settings.current().mcp_public_url
    return bool(public) and hosts.normalise(hosts.first_host_header(request.scope)) == (
        (urlsplit(public).hostname or "").lower()
    )


def _cookie_matches(request: Request, row: dict[str, Any]) -> bool:
    got = request.cookies.get(COOKIE)
    return bool(got) and _same(_hash(got), row["browser"])


# --- Authorize --------------------------------------------------------------------------------


def authorize(db: Database, request: Request) -> Response:
    q = request.query_params
    state = q.get("state")
    try:
        client = mcp_clients.client_for(db, q.get("client_id", ""))
    except mcp_clients.FetchBusy:
        return _error_page("Too many sign-ins are starting. Try again shortly.", 503)
    except mcp_clients.ClientRefused as exc:
        return _error_page(f"This app cannot ask to connect: {exc}")
    uri = q.get("redirect_uri", "")
    if not mcp_clients.redirect_matches(client.redirect_uris, uri):
        return _error_page("The return address is not one this app registered.")

    def fail(error: str, note: str) -> Response:
        echo = state if state is None or len(state) <= MAX_FIELD else None
        return _redirect_error(uri, error, echo, note)

    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "response_type must be code")
    challenge = q.get("code_challenge", "")
    if q.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
        return fail("invalid_request", "PKCE with S256 is required")
    if not _same(q.get("resource"), resource()):
        return fail("invalid_target", f"resource must be {resource()}")
    if not state:
        return fail("invalid_request", "state is required")
    scope = q.get("scope", "")
    if len(state) > MAX_FIELD or len(scope) > MAX_FIELD:
        return fail(
            "invalid_request", f"state and scope are at most {MAX_FIELD} characters"
        )
    if scope.split() and not requested_permissions(scope):
        return fail("invalid_scope", "none of the scopes asked for is known")
    now = datetime.now(UTC)
    start = oidc.begin(callback_uri())
    browser = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "insert into mcp_signin (id, client_id, redirect_uri, code_challenge, resource,"
            " scope, client_state, oidc_state_sha256, browser_sha256, oidc_nonce,"
            " oidc_verifier, created_at, expires_at) values"
            " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                secrets.token_urlsafe(18),
                client.id,
                uri,
                challenge,
                resource(),
                scope,
                state,
                _hash(start.state),
                _hash(browser),
                start.nonce,
                start.verifier,
                now,
                now + SIGN_IN_LASTS,
            ),
        )
    out = RedirectResponse(start.url, status_code=302, headers=_NO_STORE)
    out.set_cookie(
        COOKIE,
        browser,
        max_age=int(SIGN_IN_LASTS.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/oauth",
    )
    return out


def callback(db: Database, request: Request) -> Response:
    q = request.query_params
    now = datetime.now(UTC)
    given = q.get("state", "")
    claimed = _claim(db, "oidc_state_sha256", given, now) if given else None
    if claimed is None:
        return _error_page(
            "This sign-in is unknown, used already or too old. Start again."
        )
    row = _row(claimed)
    if not _cookie_matches(request, row):
        return _error_page("This sign-in was not started in this browser. Start again.")
    uri = row["redirect_uri"]
    if q.get("error") or not q.get("code"):
        return _redirect_error(uri, "access_denied", row["client_state"])
    try:
        login = oidc.finish(
            db, q["code"], row["verifier"], row["nonce"], callback_uri()
        )
    except oidc.SignInRefused as exc:
        return _error_page(str(exc) or "Sign-in was refused.", 403)
    try:
        client = mcp_clients.recorded(db, row["client_id"])
    except mcp_clients.ClientRefused as exc:
        return _error_page(f"This app cannot ask to connect: {exc}")
    wanted = requested_permissions(row["scope"])
    with db.connect() as conn:
        conn.execute(
            "update mcp_signin set person = %s, groups = %s where id = %s",
            (login.person, dbmod.jsonb(list(login.groups)), row["id"]),
        )
    held = mcp_apps.live_app(db, login.person, client.id)
    if (
        held is not None
        and held.live(now)
        and client.kind == "cimd"  # a registered app is never remembered
        and held.redirect_uri == uri
        and set(wanted) <= set(held.permissions)
    ):
        given = [p for p in held.permissions if p in wanted]
        return _allow(db, row["id"], held, given, now)
    nonce = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "update mcp_signin set consent_sha256 = %s, expires_at = %s where id = %s",
            (_hash(nonce), now + SIGN_IN_LASTS, row["id"]),
        )
    return _consent_page(client, nonce, uri, wanted)


def decide(db: Database, request: Request, form) -> Response:
    from ream import gallery

    gallery._same_origin_write(request)
    now = datetime.now(UTC)
    nonce = str(form.get("consent", ""))
    claimed = _claim(db, "consent_sha256", nonce, now) if nonce else None
    if claimed is None:
        return _error_page(
            "This consent screen is used already or too old. Start again."
        )
    row = _row(claimed)
    if not _cookie_matches(request, row) or row["person"] is None:
        return _error_page("This sign-in was not started in this browser. Start again.")
    if form.get("decision") != "allow":
        return _redirect_error(
            row["redirect_uri"], "access_denied", row["client_state"]
        )
    wanted = set(requested_permissions(row["scope"]))
    ticked = [
        p for p in mcp_apps.PERMISSIONS if p in form.getlist("perm") and p in wanted
    ]
    if not ticked or ("relay" in ticked and "read" not in ticked):
        words = (
            "Tick at least one box, or press Deny."
            if not ticked
            else "Passing on answers needs reading: tick both, or neither."
        )
        # Put the consent back for another try.
        again = secrets.token_urlsafe(32)
        with db.connect() as conn:
            conn.execute(
                "update mcp_signin set consent_sha256 = %s where id = %s",
                (_hash(again), row["id"]),
            )
        try:
            client = mcp_clients.recorded(db, row["client_id"])
        except mcp_clients.ClientRefused as exc:
            return _error_page(f"This app cannot ask to connect: {exc}")
        page = _consent_page(client, again, row["redirect_uri"], sorted(wanted))
        page.body = page.body.replace(
            b"<form", f"<p><strong>{escape(words)}</strong></p><form".encode(), 1
        )
        page.headers["content-length"] = str(len(page.body))
        return page
    try:
        client = mcp_clients.recorded(db, row["client_id"])
    except mcp_clients.ClientRefused as exc:
        return _error_page(f"This app cannot ask to connect: {exc}")
    person = row["person"]
    held = mcp_apps.live_app(db, person, client.id)
    if held is not None and not held.live(now):  # idle or past its cap: start afresh
        end_app(db, held.id, "lapsed", now)
        held = None
    if held is None:
        agent = mcp_apps.agent_for(db, person, client.id, client.host)
        held = mcp_apps.create_app(
            db,
            person=person,
            client_id=client.id,
            agent=agent.agent,
            permissions=ticked,
            redirect_uri=row["redirect_uri"],
            groups=row["groups"] or (),
            now=now,
        )
    else:  # a wider request, or a registered app again: this consent replaces the old
        with db.connect() as conn:
            conn.execute(
                "update mcp_app set permissions = %s, redirect_uri = %s where id = %s",
                (dbmod.jsonb(ticked), row["redirect_uri"], held.id),
            )
    return _allow(db, row["id"], held, ticked, now)


def _allow(
    db: Database,
    signin_id: str,
    app: mcp_apps.App,
    permissions: list[str],
    now: datetime,
) -> Response:
    code = secrets.token_urlsafe(32)
    with db.connect() as conn:
        found = conn.execute(
            "update mcp_signin set code_sha256 = %s, code_expires_at = %s, app_id = %s,"
            " permissions = %s where id = %s returning redirect_uri, client_state",
            (
                _hash(code),
                now + CODE_LASTS,
                app.id,
                dbmod.jsonb(permissions),
                signin_id,
            ),
        ).fetchone()
    uri, client_state = found
    return _interstitial(uri, {"code": code, "state": client_state, "iss": issuer()})


# --- Token ------------------------------------------------------------------------------------


def _grant_error(error: str, words: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": words},
        status_code=status,
        headers=_NO_STORE,
    )


def end_app(db: Database, app_id: str, reason: str, now: datetime) -> None:
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update mcp_app set revoked_at = %s, revoked_reason = %s"
            " where id = %s and revoked_at is null",
            (now, reason, app_id),
        )
        conn.execute("delete from mcp_token where app_id = %s", (app_id,))


def issue_tokens(db: Database, app: mcp_apps.App, now: datetime) -> dict[str, Any]:
    cfg = settings.current()
    access, refresh = new_token(ACCESS_PREFIX), new_token(REFRESH_PREFIX)
    ttl = cfg.access_token_ttl_secs()
    refresh_ends = min(
        now + timedelta(days=cfg.refresh_token_ttl_days()), app.expires_at
    )
    with db.connect() as conn, conn.transaction():
        for token, kind, ends in (
            (access, "access", now + timedelta(seconds=ttl)),
            (refresh, "refresh", refresh_ends),
        ):
            conn.execute(
                "insert into mcp_token (token_sha256, app_id, kind, created_at, expires_at)"
                " values (%s, %s, %s, %s, %s)",
                (_hash(token), app.id, kind, now, ends),
            )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "scope": " ".join(f"ream:{p}" for p in app.permissions),
    }


def _slide_idle(db: Database, app_id: str, now: datetime) -> None:
    with db.connect() as conn:
        conn.execute(
            "update mcp_app set last_used_at = %s, idle_expires_at = %s where id = %s",
            (now, now + mcp_apps.IDLE_LASTS, app_id),
        )


def refresh(db: Database, form, now: datetime) -> JSONResponse:
    """The refresh_token grant, strict rotation (section 9): each use spends the token and
    gives a new one; a spent one presented again ends the app, since two parties hold it."""
    raw = str(form.get("refresh_token", ""))
    if not raw.startswith(REFRESH_PREFIX):
        return _grant_error("invalid_grant", "unknown refresh token")
    digest = _hash(raw)
    with db.connect() as conn:
        row = conn.execute(
            "select app_id, used_at, expires_at from mcp_token"
            " where token_sha256 = %s and kind = 'refresh'",
            (digest,),
        ).fetchone()
    if row is None:
        return _grant_error("invalid_grant", "unknown refresh token")
    app_id, used_at, ends = row
    if used_at is not None:
        end_app(db, app_id, "refresh_reuse", now)
        return _grant_error("invalid_grant", "this refresh token was already used")
    app = mcp_apps.app(db, app_id)
    if ends is None or now >= ends or app is None or not app.live(now):
        return _grant_error("invalid_grant", "this connection has ended")
    if not _same(form.get("client_id"), app.client_id):
        return _grant_error("invalid_grant", "this token was issued to another client")
    sent_resource = form.get("resource")
    if sent_resource is not None and not _same(str(sent_resource), resource()):
        return _grant_error("invalid_target", "resource does not match")
    with db.connect() as conn:  # one conditional update: the race has one winner
        won = conn.execute(
            "update mcp_token set used_at = %s"
            " where token_sha256 = %s and kind = 'refresh' and used_at is null"
            " and expires_at > %s and exists (select 1 from mcp_app where id = %s"
            " and revoked_at is null and idle_expires_at > %s and expires_at > %s)",
            (now, digest, now, app_id, now, now),
        ).rowcount
    if won != 1:  # lost the race: the token is spent, so it is a second presentation
        end_app(db, app_id, "refresh_reuse", now)
        return _grant_error("invalid_grant", "this refresh token was already used")
    _slide_idle(db, app_id, now)
    issued = issue_tokens(db, app, now)
    after = mcp_apps.app(db, app_id)
    if after is None or after.revoked_at is not None:
        # the app ended between the spend and the insert (a reuse elsewhere): drop what
        # was just written so no token outlives the end
        with db.connect() as conn:
            conn.execute("delete from mcp_token where app_id = %s", (app_id,))
        return _grant_error("invalid_grant", "this connection has ended")
    return JSONResponse(issued, headers=_NO_STORE)


def revoke(db: Database, form, now: datetime | None = None) -> None:
    """RFC 7009. Never says whether the token was known. An access token goes alone; a
    refresh token ends its app and everything it holds."""
    now = now or datetime.now(UTC)
    raw = str(form.get("token", ""))
    if not raw.startswith((ACCESS_PREFIX, REFRESH_PREFIX)):
        return
    with db.connect() as conn:
        row = conn.execute(
            "select app_id, kind from mcp_token where token_sha256 = %s", (_hash(raw),)
        ).fetchone()
    if row is None:
        return
    app = mcp_apps.app(db, row[0])
    client = form.get("client_id")
    if app is None or (client is not None and not _same(str(client), app.client_id)):
        return
    if row[1] == "refresh":
        end_app(db, app.id, "revoked", now)
    else:
        with db.connect() as conn:
            conn.execute("delete from mcp_token where token_sha256 = %s", (_hash(raw),))


def sweep(db: Database, now: datetime | None = None) -> int:
    """Drop sign-ins that are over or used and tokens that are expired or spent. A day's
    grace keeps a just-spent row around for the reuse check."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=1)
    with db.connect() as conn, conn.transaction():
        lapsed = [
            r[0]
            for r in conn.execute(
                "select id from mcp_app where revoked_at is null"
                " and (idle_expires_at <= %s or expires_at <= %s)",
                (now, now),
            ).fetchall()
        ]
        for app_id in lapsed:
            conn.execute(
                "update mcp_app set revoked_at = %s, revoked_reason = 'lapsed'"
                " where id = %s",
                (now, app_id),
            )
            conn.execute("delete from mcp_token where app_id = %s", (app_id,))
        n = len(lapsed)
        n += conn.execute(
            "delete from mcp_signin where expires_at < %s"
            " or (used_at is not null and used_at < %s)",
            (cutoff, cutoff),
        ).rowcount
        n += conn.execute(
            "delete from mcp_token where expires_at < %s"
            " or (used_at is not null and used_at < %s)",
            (cutoff, cutoff),
        ).rowcount
    return n


def token(db: Database, form, now: datetime | None = None) -> JSONResponse:
    now = now or datetime.now(UTC)
    if form.get("grant_type") == "refresh_token":
        return refresh(db, form, now)
    if form.get("grant_type") != "authorization_code":
        return _grant_error("unsupported_grant_type", "only authorization_code here")
    for name in ("client_id", "redirect_uri", "resource", "code", "code_verifier"):
        if name in form and not isinstance(form.get(name), str):
            return _grant_error("invalid_request", f"{name} must be a plain field")
    code = str(form.get("code", ""))
    if not code:
        return _grant_error("invalid_request", "code is required")
    with db.connect() as conn:
        taken = conn.execute(
            "update mcp_signin set used_at = %s where code_sha256 = %s and used_at is null"
            " returning client_id, redirect_uri, code_challenge, resource, app_id,"
            " code_expires_at, permissions",
            (now, _hash(code)),
        ).fetchone()
        if taken is None:
            seen = conn.execute(
                "select app_id from mcp_signin where code_sha256 = %s and used_at is not null",
                (_hash(code),),
            ).fetchone()
        else:
            seen = None
    if seen is not None:  # presented twice: two parties hold it, so the app ends
        if seen[0]:
            end_app(db, seen[0], "reuse", now)
        return _grant_error("invalid_grant", "this code was already used")
    if taken is None:
        return _grant_error("invalid_grant", "unknown code")
    client_id, redirect, challenge, res, app_id, ends, _perms = taken
    verifier = str(form.get("code_verifier", ""))
    if ends is None or now >= ends:
        return _grant_error("invalid_grant", "this code has expired")
    if not _same(form.get("client_id"), client_id):
        return _grant_error("invalid_grant", "this code was issued to another client")
    if not _same(form.get("redirect_uri"), redirect):
        return _grant_error("invalid_grant", "redirect_uri does not match")
    sent_resource = form.get("resource")
    if sent_resource is not None and not _same(str(sent_resource), res):
        return _grant_error("invalid_target", "resource does not match")
    if not (43 <= len(verifier) <= 128) or not _same(
        oidc.challenge(verifier), challenge
    ):
        return _grant_error("invalid_grant", "code_verifier does not match")
    app = mcp_apps.app(db, app_id)
    if app is None or not app.live(now):
        return _grant_error("invalid_grant", "this connection has ended")
    return JSONResponse(issue_tokens(db, app, now), headers=_NO_STORE)


def verify_access(
    db: Database, raw: str, now: datetime | None = None
) -> mcp_apps.App | None:
    """The live app an `rmo_` token belongs to, or None. Touches `last_used_at` at most every
    five minutes."""
    if not raw.startswith(ACCESS_PREFIX):
        return None
    now = now or datetime.now(UTC)
    with db.connect() as conn:
        row = conn.execute(
            "select app_id from mcp_token where token_sha256 = %s and kind = 'access'"
            " and expires_at > %s",
            (_hash(raw), now),
        ).fetchone()
    if row is None:
        return None
    app = mcp_apps.app(db, row[0])
    if app is None or not app.live(now):
        return None
    if app.last_used_at is None or now - app.last_used_at > TOUCH_EVERY:
        with db.connect() as conn:
            conn.execute(
                "update mcp_app set last_used_at = %s, idle_expires_at = %s where id = %s",
                (now, now + mcp_apps.IDLE_LASTS, app.id),
            )
    return app


# --- Routes -----------------------------------------------------------------------------------


def oauth_router(db: Database, limiter: ratelimit.FailedAuthLimiter) -> APIRouter:
    router = APIRouter()

    def gone() -> Response:
        return Response("not found", status_code=404, media_type="text/plain")

    def on(request: Request, here) -> bool:
        return settings.current().mcp_signs_in() and here(request)

    @router.get("/oauth/authorize", include_in_schema=False)
    def authorize_route(request: Request):
        if not on(request, _gallery_here):
            return gone()
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        _count, blocked, _newly = limiter.failed(address)  # every call counts
        if blocked:
            return _error_page(
                "Too many sign-ins from this address. Wait a few minutes.", 429
            )
        return authorize(db, request)

    @router.get(CALLBACK_PATH, include_in_schema=False)
    def callback_route(request: Request):
        if not on(request, _gallery_here):
            return gone()
        return callback(db, request)

    @router.post("/oauth/authorize", include_in_schema=False)
    async def decide_route(request: Request):
        if not on(request, _gallery_here):
            return gone()
        form = await _read_form(request)
        if form is None:
            return _error_page("That request is too large.", 413)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(decide, db, request, form)

    @router.post("/oauth/token", include_in_schema=False)
    async def token_route(request: Request):
        if not on(request, _public_here):
            return gone()
        address = ratelimit.client_address(
            request.headers, request.client.host if request.client else None
        )
        form = await _read_form(request)
        if form is None:
            return _grant_error("invalid_request", "the request body is too large", 413)
        from starlette.concurrency import run_in_threadpool

        answer = await run_in_threadpool(token, db, form)
        if answer.status_code >= 400:
            _count, blocked, _newly = limiter.failed(address)
            if blocked:
                return _grant_error("invalid_request", "too many failures; wait", 429)
        return answer

    @router.post("/oauth/revoke", include_in_schema=False)
    async def revoke_route(request: Request):
        if not on(request, _public_here):
            return gone()
        form = await request.form()
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(revoke, db, form)
        return Response(status_code=200, headers=_NO_STORE)

    return router
