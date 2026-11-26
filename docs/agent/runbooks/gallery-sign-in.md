# Signing in to the gallery (REAM_GALLERY_SIGN_IN)

Design: `docs/agent/specs/2026-11-24-people-design.md` (slice 4, and "Slice 4 as built"). The code is
`src/ream/signin.py`. This page is for whoever turns it on and whoever has to roll it back.

**Rolling back below this image with sign-in on and no authenticating proxy in front of the
gallery is FORBIDDEN.** An older image does not read `REAM_GALLERY_SIGN_IN`: it ignores the setting
and serves the whole gallery open. If a rollback is unavoidable, put a deny rule at the proxy first.

## What it does

With `REAM_GALLERY_SIGN_IN` unset (the default) nothing changes: no gate, no cookie, and `/signin`,
`/signin/callback` and `/signout` answer the 404 of a path that does not exist.

With it `on`, a gate in front of the whole gallery app refuses every request that has no live
session, except the open list below. A browser navigation (a GET or HEAD asking for a document) is
sent to `/signin?next=<path>` with a 303; anything else (a script's fetch, a form post) gets a 401
it can read. The gate fails closed: it does not rely on a route asking who is signed in
(`tests/test_gate_covers_every_route.py` fails for a route nobody has classified). Writes also pass
the gallery's own same-origin check at the gate.

The sign-in is Ream's own OpenID Connect round trip, with the same provider settings as
`REAM_MCP_SIGN_IN` (`docs/agent/runbooks/mcp-sign-in.md`: issuer, client id, secret file, owner claim)
and its own cookies, tables and routes. No identity header (`Remote-User`, `X-Forwarded-User`, ...)
is ever read.

1. `GET /signin?next=<path>`: a row in flight (10 minutes) and the cookie `__Host-ream_signin`, then a
   302 to the provider. `next` is kept only if it is a path on this origin.
2. The provider returns to `<gallery origin>/signin/callback`. The row is claimed once by its state,
   tied to the browser by that cookie, the code is exchanged, and `oidc.person_login` decides who the
   person is. A fresh session is minted at every callback (any session the request carried is
   deleted) and the browser is sent on with a 303.
3. The session cookie is `__Host-ream_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain, so
   it is host-only on the gallery host and never reaches the content domain
   (`tests/test_browser_session_cookie.py`, in a real browser). Only its hash is stored
   (`person_session`).

## Settings

- `REAM_GALLERY_SIGN_IN`: `on` or `off` (unset: off). On needs the provider settings of
  `REAM_MCP_SIGN_IN` (`REAM_OIDC_ISSUER`, `REAM_OIDC_CLIENT_ID`, `REAM_OIDC_CLIENT_SECRET_FILE`), and
  `REAM_GALLERY_ORIGIN` must be https (plain http only on loopback), or start-up refuses and names
  the setting. It does not need `REAM_MCP_SIGN_IN` or `REAM_MCP_PUBLIC_URL`.
- `REAM_SIGN_IN_WHO`: `owner` (default) or `anyone`. `anyone` refuses to start while
  `people.OPEN_SIGN_IN` is False (today it is): only the owner may sign in.

## Behind a proxy

Set `REAM_TRUSTED_IP_HEADER` to the header your proxy puts the caller's address in. Without it every
caller shares the proxy's address, and `/signin` and `/signin/callback` share one rate limiter
(10 calls in 60 s, then a 300 s block; one sign-in costs two calls), so a few strangers' requests, or
the owner's own retries, could keep the owner at 429.

Tabs restored after a browser restart reload with the session cookie, so they keep working until the
12 hours end; after that each one is sent to sign in and, once signed in, returns to its own path.
Sent there together, they do not all get through: a browser holds one sign-in in progress (one
`__Host-ream_signin` cookie), so only the tab that started last comes back signed in; the others
answer "This sign-in was not started in this browser". Reload one of them once the first is signed in.

## Registering the redirect address

At the provider, add a **second** redirect URI to the same client: `<gallery origin>/signin/callback`
(for example `https://gallery.example/signin/callback`), beside `<gallery origin>/oauth/callback`
that MCP sign-in uses. It is built from `REAM_GALLERY_ORIGIN`, never from the Host header. Without
it the provider refuses the round trip and the sign-in page says the provider could not be reached or
the sign-in was not completed.

## What the gate lets through unsigned

Exactly these (`signin.OPEN_PATHS`, `OPEN_PREFIXES`):

- `/healthz`, `/favicon.svg`;
- `/signin`, `/signin/callback`;
- `/mcp`, and the prefixes `/oauth/` and `/.well-known/` (MCP: its own credential, an agent key or an
  `rmo_` token, never this cookie);
- the prefix `/api/v1/` (the agent API: an agent key in `Authorization`).

Everything else needs a session, `/signout` included. The content domain is not behind the gate:
pages are served from their own origin and never see the session.

## Signing out

`POST /signout` ends the request's session and clears the cookie. It answers 200 with a page and a
link to sign in again, not a redirect: the gallery's CSP (`form-action 'self'`) blocks a redirect out
of a form post, and a redirect to sign in would leave for the provider.

## Sessions

A session lasts 12 hours from sign-in and is not renewed by use (`last_seen_at` is touched at most
every 5 minutes, for the record). A disabled person's sessions stop at once. The nightly pass
(`maintenance.nightly`) deletes expired sessions and sign-ins left in flight (`signin.sweep`). To end
one by hand, delete its `person_session` row; to end every session, delete all of them.

## Rolling back

Turning the setting off and restarting removes the gate: the whole gallery is open unless something in
front of it authenticates, and the sign-in routes answer 404 again (the tables stay). So it carries the
same condition as a rollback: **first restore authentication or a deny rule at the proxy.** Rolling back to an image that predates this feature is **FORBIDDEN** while the setting is on and
nothing in front authenticates (see the top of this page): put the deny rule at the proxy first.
