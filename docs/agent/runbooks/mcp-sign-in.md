# Signing an app in to /mcp (REAM_MCP_SIGN_IN)

Design: `docs/agent/specs/2026-11-08-mcp-oauth-design.md` (§7, §9, §10, §12). Trying it from an
app: `docs/mcp.md`. This page is for whoever turns it on and whoever has to end an app.

## Turning it on

All of these are server settings; a missing or bad one refuses to start and names the setting.

1. `REAM_MCP_PUBLIC_URL`: the gallery origin or `https://<REAM_API_HOST>`. `/mcp`, `/oauth/token`
   and `/oauth/revoke` answer on it; the consent page (`/oauth/authorize`, `/oauth/callback`) is
   always on the gallery origin.
2. `REAM_MCP_SIGN_IN=on`.
3. The upstream provider the one reader signs in with:
   - `REAM_OIDC_ISSUER` (https; discovery is read from it);
   - `REAM_OIDC_CLIENT_ID`;
   - `REAM_OIDC_CLIENT_SECRET_FILE`: the path of a file holding the secret. The person who runs
     the provider creates that file; nothing here writes or prints it.
   - Register `<gallery origin>/oauth/callback` as the redirect URI at the provider.
   - Optional: `REAM_OIDC_OWNER_CLAIM` / `REAM_OIDC_OWNER_VALUE` (who the reader is; with `email`
     the provider must say `email_verified`), `REAM_OIDC_OWNER_SUBJECT` (pin the provider `sub`;
     unset, the first owner sign-in pins issuer and `sub`), `REAM_OIDC_SCOPES`.
4. Which apps may ask: `REAM_MCP_ALLOWED_CLIENT_HOSTS`, `REAM_MCP_TRUSTED_CLIENT_HOSTS` (labelled
   Verified on the consent page), `REAM_MCP_SELF_REGISTRATION_REDIRECTS`. See `docs/mcp.md`.
5. Lifetimes, all positive whole numbers: `REAM_MCP_ACCESS_TOKEN_TTL_SECS` (3600),
   `REAM_MCP_REFRESH_TOKEN_TTL_DAYS` (30), `REAM_MCP_APP_MAX_LIFETIME_DAYS` (90; the owner has not
   decided the cap, 90 stands until he does).

Check: `curl -s <public url>/.well-known/oauth-authorization-server` lists `token_endpoint` and
`revocation_endpoint`; connect from the app and watch the consent page appear.

## How long an app lasts

- An access token (`rmo_`) lives an hour. A refresh token (`rmr_`) is single use: each refresh
  returns a new pair and spends the old one.
- An app that is not used for 30 days lapses; every call to `/mcp` and every refresh slides that
  window. No app outlives the cap, however busy; at the cap he consents again.
- The nightly clean-up marks lapsed apps ended (`lapsed`) and drops their tokens, then drops
  sign-ins and tokens that are over or spent (a day after, so a replayed code is still noticed).

## What a refusal looks like

At `/oauth/token`, a JSON error with status 400 (`invalid_grant`, or `invalid_target` for a
wrong `resource`); more than a few failures from one address answer 429. At `/mcp`, a missing,
expired, ended or lapsed token answers 401 with the discovery challenge; the app then refreshes
or asks the person to connect again. `/oauth/revoke` always answers 200, whatever the token.

An app ends by itself, reason recorded in `mcp_app.revoked_reason`, when:

| reason | what happened |
|---|---|
| `reuse` | an authorization code was presented twice |
| `refresh_reuse` | a spent refresh token was presented again (two parties hold it). It can also mean a client lost the response to a refresh; the person simply connects again |
| `revoked` | the app presented its refresh token to `/oauth/revoke` |
| `lapsed` | idle for the window, or past the cap, found by the clean-up |

## Ending an app

- By the app: `POST /oauth/revoke` with `token=<rmr_...>` ends it; with an `rmo_` token only that
  access token goes.
- By the operator, today: set `revoked_at` and `revoked_reason` (for example `operator`) on the
  `mcp_app` row and delete its `mcp_token` rows, in one transaction. A page and admin command
  for this are slice 6 ("Connected apps"); until then this is the way. The next call fails with
  401 and a new consent is needed.
- Everything at once: turn `REAM_MCP_SIGN_IN` off (the endpoints answer 404 and stored tokens
  stop working), or end every app as above.
