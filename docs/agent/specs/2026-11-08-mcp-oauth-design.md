# Ream in Claude.ai: sign in once, no key (design, 8 Nov 2026)

A person adds Ream (formerly artifacts) to Claude.ai, or to any other app that speaks MCP, as a
remote connector, signs in once, and from then on their Claude publishes, reads, comments, asks
and passes on answers as them, with no key to mint or paste. Keys stay for machines, offices and
CI. This turns the intent page "Ream in Claude.ai: sign in once, no key"
(`4zs2zrosdnteydvi3bxrkq2pze`) into a buildable design; the build slices are in
`docs/agent/implementation-plan.md`, "MCP sign-in for people's agents". Nothing here is built.

Settled by the owner on 8 Nov 2026 (`docs/decisions.md`, "Ream for GitGuardian…" item 9, and
"Ream in Claude.ai: the six sign-in choices"). Where this design follows cove's MCP sign-in and
where it differs is section 14; the owner asked for cove's choices to be reused wherever his six
answers allow.

Primary sources, read 8 Nov 2026: MCP authorization
[2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) and
[2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) (with
its discovery, client-registration and security-considerations pages), Streamable HTTP for both
revisions, [Claude's connector authentication](https://claude.com/docs/connectors/building/authentication.md),
[workers-oauth-provider](https://github.com/cloudflare/workers-oauth-provider) (README,
`storage-schema.md`, `docs/authorization-server.md`), and cove's `docs/admin/mcp-sign-in.md`,
`docs/agent-ops/mcp.md`, `docs/admin/configuration.md` (`[mcp]`, `[mcp.oauth]`),
`docs/admin/behind-a-proxy.md` ("An MCP-only host") and `docs/vocabulary.md` ("Agent access (MCP)").

## 1. The six answers this builds on

1. **Answering for him.** When his Claude acts as him it may pass on his answer to any question
   put to him, not only its own: a relay, never a tap, labelled with the app, carrying his words.
2. **Reach.** Read everything he can read; change only pages made under his sign-in (by any of his
   connected apps); comment and ask anywhere he could.
3. **Consent.** Three plain permissions, each ticked and each untickable: read; publish, comment
   and ask; pass on answers. The screen names the app by its checked web address and where it
   sends him back, and is remembered per app until he revokes.
4. **Homelab sign-in.** Through Authelia's standard OIDC sign-in, as the forge does: the same path
   the company sign-on takes.
5. **Lifetime.** A row in "Machines and keys" with Revoke; hour-long tokens renewed silently; it
   lapses after 30 days unused, and ends 90 days after consent so he signs in again, as cove's do
   (Jeremy, 9 Nov 2026).
6. **Which apps.** Any MCP app, shown by its checked address, once approved on the consent screen;
   a deployment may set an allowed list.

Earlier the same day: MCP with OAuth for people's agents, keys kept for machines; in the core;
the homelab first, tried from Claude.ai before the company build; the agent field names the
client ("Jeremy, via claude.ai").

## 2. Architecture

Ream is its own OAuth 2.1 authorization server towards MCP apps, in the shape of Cloudflare's
workers-oauth-provider and of cove. When a person must prove who they are, Ream hands the login to
an upstream OpenID Connect provider (Authelia at home, the company sign-on at work), gets them back,
asks consent itself, and issues its own opaque tokens, good only at Ream's `/mcp`.

```
Claude.ai backend ──POST /mcp (no token)──────────────▶ Ream public host ── 401 + resource_metadata
Claude.ai backend ──GET /.well-known/oauth-*──────────▶ Ream public host
person's browser  ──GET /oauth/authorize──────────────▶ Ream gallery host (behind Authelia at home)
                  ◀─302 to OIDC provider; sign in; 302 back to /oauth/callback
                  ◀─consent screen; Allow ─ 200 page that sends the browser to claude.ai with a code
Claude.ai backend ──POST /oauth/token (code + PKCE verifier + resource)──▶ access + refresh token
Claude.ai backend ──POST /mcp, Bearer rmo_…───────────▶ tools run as "Jeremy, via claude.ai"
```

Why Ream runs the sign-in itself rather than letting the identity provider be the authorization
server: Claude.ai needs a server that accepts its client metadata document (or open registration)
and issues tokens bound to Ream's address; few company sign-ons do either and Authelia's support
is unconfirmed. With Ream as the server, the one list of connections lives in Ream beside the keys,
where it can be revoked instantly, and the upstream needs only plain OIDC. The MCP spec allows the
authorization server to live with the resource server.

**One process, no new dependency.** The MCP endpoint, the authorization server and the OIDC client
are modules of the existing FastAPI service, on Postgres or SQLite through `db.py`. Outbound fetches
(client metadata documents, the OIDC provider) use the standard library, as `webhook.py` does, and
reuse its address guard. No JWT library: the upstream ID token arrives straight from the provider's
token endpoint over TLS, so its claims are checked (`iss`, `aud`, `exp`, `nonce`) without verifying
its signature, which OIDC Core 3.1.3.7 allows for a token received that way. Ream's own tokens are
random strings stored as hashes, so nothing is signed and no signing key exists (section 15).

**The MCP layer is hand-written,** a small JSON-RPC handler: stateless, JSON responses only, no
server-to-client requests, no subscriptions. Both revisions Claude.ai uses must be negotiated
explicitly (section 6), and the surface is small enough that an SDK would add more than it saves;
an SDK whose revision lags would also decide which clients Ream can serve. New modules:
`ream/mcp.py` (transport and dispatch), `ream/mcp_tools.py` (tools, resources, prompts),
`ream/oauth.py` (authorization server), `ream/oidc.py` (upstream login), `ream/apps.py` (connected
apps, tokens, client identification).

## 3. Hosts and endpoints

`REAM_MCP_PUBLIC_URL` is an origin with no path (cove's `public_url`). Setting it turns `/mcp` on.
It must be the gallery origin or `https://<REAM_API_HOST>`; anything else refuses to start. The
**issuer** is that origin and the **protected resource** is `<public url>/mcp`.

Served on the public URL's host, anonymous (the "must be reachable" list, as cove's):

| Method and path | What it is |
|---|---|
| `POST /mcp` | The MCP endpoint, Streamable HTTP. `GET` and `DELETE` answer 405. |
| `GET /.well-known/oauth-protected-resource/mcp` | Protected-resource metadata (RFC 9728), path form |
| `GET /.well-known/oauth-protected-resource` | The same document, root form |
| `GET /.well-known/oauth-authorization-server` | Authorization-server metadata (RFC 8414) |
| `POST /oauth/token` | Code exchange and refresh (form-encoded, as Claude requires) |
| `POST /oauth/revoke` | RFC 7009: the app ends its own connection; always 200 |
| `POST /oauth/register` | Open registration (RFC 7591), only while self-registration is on |

Served on the gallery host only, for the person's browser (behind Authelia at home, as cove's
consent page is behind Warpgate):

| Method and path | What it is |
|---|---|
| `GET /oauth/authorize` | Checks the request, then sends the browser to the OIDC provider |
| `GET /oauth/callback` | The OIDC provider's return; checks the login and shows the consent screen |
| `POST /oauth/authorize` | Allow or Deny from the consent screen |
| `POST /machines/apps/{id}/revoke` | Revoke from "Machines and keys" (section 10) |

When the public URL is the gallery origin (the homelab), every path above is on one host and the
proxy must let the first table through without its login (section 16). When it is the API host,
the API host serves only the first table and `/api/v1`, and the gallery host serves only the
second, exactly as the host router already splits them. On the former host (`REAM_FORMER_HOST`)
none of these is served: it forwards people and keeps `/api/v1`, and MCP clients refuse redirects.

`/mcp` is not under `/api/v1` (the intent page proposed `/api/v1/mcp`): the path matches cove's,
the specs' examples and what a person types, and it keeps MCP out of `_BothWords` and out of the
OpenAPI schema, whose shapes are fixed by other specs (cove's D3). It costs one more proxy rule.

## 4. Discovery documents

Protected-resource metadata, both paths:

```json
{
  "resource": "https://ream.example/mcp",
  "authorization_servers": ["https://ream.example"],
  "scopes_supported": ["ream:read", "ream:write", "ream:relay"],
  "bearer_methods_supported": ["header"],
  "resource_name": "Ream"
}
```

`resource` equals the MCP URL exactly, path included, and Claude uses only the first authorization
server, so there is exactly one.

Authorization-server metadata:

```json
{
  "issuer": "https://ream.example",
  "authorization_endpoint": "https://ream.example/oauth/authorize",
  "token_endpoint": "https://ream.example/oauth/token",
  "revocation_endpoint": "https://ream.example/oauth/revoke",
  "registration_endpoint": "https://ream.example/oauth/register",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "revocation_endpoint_auth_methods_supported": ["none"],
  "scopes_supported": ["ream:read", "ream:write", "ream:relay"],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true
}
```

`authorization_endpoint` is on the gallery origin, which may differ from the issuer.
`registration_endpoint` is left out while self-registration is off. `offline_access` is not listed
(the 2026 revision says not to; Claude would append it if it were) and refresh tokens are issued
regardless. Claude uses a client metadata document only when both
`client_id_metadata_document_supported` and `"none"` are advertised; both are.

A request to `/mcp` with no token, or a token that is unknown, expired, revoked or for another
audience, gets **401** with

```
WWW-Authenticate: Bearer realm="ream", resource_metadata="https://ream.example/.well-known/oauth-protected-resource/mcp", scope="ream:read ream:write ream:relay"
```

Claude requires the 401 (it ignores the header on a 200).

## 5. Clients: who the app is

Three kinds, never confused, as in cove.

- **Client metadata document (CIMD), the preferred route.** `client_id` is an `https` URL with a
  path. Ream fetches it at `/oauth/authorize` with cove's fences: `https` only, public addresses
  only (the `webhook.py` guard: the address checked is the one connected to, DNS answer pinned),
  no redirects, `application/json` only, at most 64 KiB, 5 s, cached 60 s. The document must have
  `client_id` equal to the URL byte for byte, `client_name` and `redirect_uris`; every redirect URI
  must be `https` on the document's own host, or loopback (below). The request's `redirect_uri`
  must be in the list exactly.
- **Self-registration (RFC 7591), the fallback.** Deprecated by the 2026 revision, kept for clients
  that cannot do CIMD (Claude Code's local sign-in). Public clients only (`none`), PKCE S256
  required. Its ids are `reg_` plus random. Allowed return addresses come from
  `REAM_MCP_SELF_REGISTRATION_REDIRECTS` (default `localhost`: loopback only; `none` turns
  registration off). Limits from cove: 10 per hour per address (IPv6 counted per /64), 1000
  stored, one never used expires after 24 hours; over a limit, 429 or 503.
- **Loopback redirects** (`http` on `localhost`, `127.0.0.1`, `[::1]`) match on everything but the
  port, any port except 0 and an explicit `:80` (RFC 8252).

**How an app is shown: by its checked address.** A CIMD app is headed by the host of its
`client_id` (`claude.ai`); the name it gives itself is shown second, smaller, as its own claim. A
self-registered app is headed "An app on this computer" when its return address is loopback, else
by its return address's host, never by the name it chose. `REAM_MCP_TRUSTED_CLIENT_HOSTS` decides
only the **Verified** / **Unverified** label (default, cove's list: `claude.ai`, `chatgpt.com`,
`vscode.dev`, `zed.dev`, `opencode.ai`, `goose-docs.ai`, `pi.dev`). `REAM_MCP_ALLOWED_CLIENT_HOSTS`,
when set, refuses every other host before anything is fetched (`none`: no app at all); unset, any
`https` host may ask, and the person decides on the consent screen (answer 6). A self-registered
app's "host" for these lists is its return address's host, and `localhost` for loopback.

What an MCP app says about itself inside a call (`clientInfo`, user agents) is never read for any
decision.

## 6. Transport: Streamable HTTP, both revisions

Stateless, as cove's: one `POST /mcp` per message, a JSON response (`application/json`), never a
stream, no `Mcp-Session-Id` issued (one sent is ignored), `GET` and `DELETE` 405 (Ream has no
server-initiated messages: Claude.ai cannot receive a nudge anyway). Notifications get 202 with no
body. Request bodies over 12 MiB get 413 (a page as JSON, as the ingress allows).

- **2025-11-25 and the two before it (2025-06-18, 2025-03-26)**: `initialize` answers with the
  client's version when supported, else `2025-11-25`; later requests carry `MCP-Protocol-Version`,
  and an unsupported one gets 400. No version header is read as 2025-03-26.
- **2026-07-28**: no `initialize`; every request carries `MCP-Protocol-Version`, which must equal
  `io.modelcontextprotocol/protocolVersion` in the body's `_meta` (else 400, `HeaderMismatch`,
  -32020), plus `Mcp-Method` and, on `tools/call`, `resources/read` and `prompts/get`, `Mcp-Name`,
  each checked against the body. `subscriptions/listen` answers method-not-found. Capabilities are
  answered the way the 2026 schema asks; slice 1 pins the exact method names against the published
  schema and keeps a recorded request from each revision as a test fixture.

Methods: `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/templates/list`,
`resources/read`, `prompts/list`, `prompts/get`, and `initialize` for the older revisions.
Capabilities: `tools`, `resources`, `prompts`, none with `listChanged` or `subscribe`.

**The `Origin` check (cove's).** A request carrying `Origin` must carry exactly the public URL's
origin, else **403** with a JSON-RPC error and no `id`. No `Origin` passes: Claude.ai's backend
sends none, and a browser always does. The host router already refuses any `Host` that is not one
of Ream's names, so cove's separate `Host` list is not needed.

## 7. Credentials at `/mcp`

As cove decided, and for its reason: a `/mcp` reachable from the internet would otherwise take any
valid key, and a key that leaks could drive it from anywhere.

- **Sign-in off** (`REAM_MCP_SIGN_IN` unset): `/mcp` takes agent keys, with the key's own reach,
  exactly as `/api/v1` does. This is slice 1, and how an office's Claude Code uses Ream over MCP.
- **Sign-in on**: `/mcp` takes only sign-in tokens. A key gets 401 with the sign-in challenge and
  the message "Agent keys are not accepted at this server's /mcp. Sign in from your MCP app, or use
  the agent API at /api/v1 with your key." Logged at WARN as `reason=key_on_mcp` with the key's
  public id. `REAM_MCP_ACCEPT_KEYS=1` accepts keys beside sign-in; `REAM_MCP_ACCEPT_KEYS=0` with
  sign-in off refuses to start.
- A sign-in token (`rmo_…`) works only at `/mcp`. Anywhere under `/api/v1` it is refused like any
  unknown key, without a lookup, with "a sign-in token works only at /mcp".
- Failed credentials at `/mcp` and `/oauth/token` count in the existing `FailedAuthLimiter`.

## 8. Data model

New tables, written to read the same on both databases (`docs/agent/specs/2026-10-09-sqlite-design.md`):
`text` ids, `timestamptz` (SQLite text, as `db.py` stores it), `jsonb` through `db.jsonb`, `boolean`,
partial unique indexes (both support `where`). Postgres: `create table if not exists` in
`schema.sql`. SQLite: one new numbered step in `db._SQLITE_STEPS`. The copier's table list in
`tests/test_migrate.py` gains each table.

```sql
-- An app that may ask to connect: a fetched client metadata document (cached) or a
-- self-registered client. Never a secret: public clients only.
create table if not exists mcp_client (
    id             text primary key,     -- the CIMD URL, or 'reg_' + random
    kind           text not null,        -- 'cimd' | 'registered'
    host           text not null,        -- the checked address it is shown by
    name           text not null default '',  -- its own claim, shown second
    redirect_uris  jsonb not null,
    created_at     timestamptz not null default now(),
    fetched_at     timestamptz,          -- cimd: when the document was last read
    registered_from text,                -- registered: the address, for the hourly limit
    last_used_at   timestamptz
);

-- One person's consent for one app (cove's "connected app"). Remembered: a later sign-in
-- from the same app by the same person, asking for no more, skips the consent screen.
create table if not exists mcp_app (
    id               text primary key,   -- 'app_' + random; the public handle
    person           text not null,      -- the owner id today (section 13)
    client_id        text not null,
    agent            text not null,      -- the person-and-app agent, e.g. 'jeremy--claude-ai'
    permissions      jsonb not null,     -- subset of ["read","write","relay"]
    redirect_uri     text not null,      -- the return address consented to
    groups           jsonb not null default '[]',  -- from the login; unused in core
    created_at       timestamptz not null default now(),
    last_used_at     timestamptz,
    idle_expires_at  timestamptz not null,  -- slides 30 days on each refresh
    expires_at       timestamptz not null,  -- the absolute cap, 90 days from consent
    revoked_at       timestamptz,
    revoked_reason   text,               -- 'revoked' | 'app' | 'reuse' | 'lapsed' | 'admin'
    events_seen      bigint not null default 0   -- whats_new's cursor
);
create unique index if not exists mcp_app_live
    on mcp_app (person, client_id) where revoked_at is null;

-- The agent name each person-and-app pair publishes under. Never deleted: pages keep it after
-- the app is revoked, and a later consent from the same app reuses it.
create table if not exists mcp_agent (
    agent      text primary key,
    person     text not null,
    client_id  text not null,
    host       text not null,
    created_at timestamptz not null default now()
);
create unique index if not exists mcp_agent_pair on mcp_agent (person, client_id);

-- Tokens, as SHA-256 hashes only. A refresh token is used once; its replacement names it.
create table if not exists mcp_token (
    token_sha256  text primary key,
    app_id        text not null references mcp_app(id) on delete cascade,
    kind          text not null,         -- 'access' | 'refresh'
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    used_at       timestamptz            -- refresh: when it was exchanged
);
create index if not exists mcp_token_app on mcp_token (app_id);

-- One sign-in in flight, from /oauth/authorize to the code exchange. Every secret in it is
-- a hash; each stage is single-use and the row is dead after ten minutes.
create table if not exists mcp_signin (
    id                 text primary key,
    client_id          text not null,
    redirect_uri       text not null,
    code_challenge     text not null,     -- S256 only
    resource           text not null,
    scope              text not null default '',
    client_state       text,              -- echoed back to the app, opaque
    oidc_state_sha256  text unique,       -- the upstream round trip
    oidc_nonce         text,
    oidc_verifier      text,              -- Ream's own PKCE verifier upstream; 10 minutes
    person             text,              -- set once the login is checked
    groups             jsonb not null default '[]',
    consent_sha256     text unique,       -- the consent form's one-time nonce
    code_sha256        text unique,       -- the authorization code
    app_id             text,              -- set at Allow
    permissions        jsonb not null default '[]',
    created_at         timestamptz not null default now(),
    expires_at         timestamptz not null,
    used_at            timestamptz
);
```

Codes, consent nonces and sign-in state live in the database, not in memory as cove's do: Ream
runs more than one process (two pods during a rollout, and the suite runs two processes against one
database), so a code minted by one must be exchangeable at the other. The maintenance pass deletes
`mcp_signin` rows an hour after they expire, `mcp_token` rows a day after they expire, and unused
registered clients after 24 hours.

## 9. Tokens and their lifecycle

Opaque random tokens from `secrets`, as cove's: access tokens `rmo_` + 43 base62, refresh tokens
`rmr_` + 43 base62, codes 256 bits. Only their SHA-256 is stored, as keys' are; none is ever logged.
Both prefixes join `secret_shapes.txt`, so a token pasted into a page or comment is refused at
publish like a key.

- **Authorize** (`GET /oauth/authorize`): requires `response_type=code`, `client_id`,
  `redirect_uri`, `code_challenge` with `code_challenge_method=S256`, `resource` equal to
  `<public url>/mcp`, and `state` (passed back unchanged). The client is identified (section 5)
  before anything else; an unknown client or a return address not in its list gets an error page
  and is **never** redirected to. Other errors go back to the return address with `error`, `state`
  and `iss`.
- **Login**: a `mcp_signin` row, then a 302 to the OIDC provider (code flow, Ream's own PKCE,
  `state` and `nonce`). At `/oauth/callback` Ream exchanges the code with its client secret, checks
  the ID token's `iss`, `aud`, `exp` and `nonce`, reads the person from the owner claim (section 13)
  and the groups claim, and shows the consent screen, or skips it (remembered consent, below).
- **Allow**: a code bound to the client, return address, PKCE challenge, resource, person and
  ticked permissions, valid **60 seconds**, single use. The browser is sent on with a 200 page that
  moves it to `redirect_uri?code=…&state=…&iss=<issuer>` (cove's interstitial: the gallery's CSP
  `form-action 'self'` blocks a 302 out of a form post).
- **Code exchange** (`POST /oauth/token`, `grant_type=authorization_code`): checks the code, the
  `code_verifier`, `client_id`, `redirect_uri` and `resource`. A code presented twice ends the app
  it made. Returns `access_token`, `token_type: "Bearer"`, `expires_in: 3600`, `refresh_token`,
  `scope` (the granted permissions as scopes).
- **Refresh** (`grant_type=refresh_token`): the refresh token is used once. The response carries a
  new access token and a new refresh token, and the old refresh token is dead from that moment. A
  used refresh token presented again **ends the whole connected app** (two parties hold it); so
  does one presented by another `client_id`. `resource`, when sent, must match. Each refresh slides
  `idle_expires_at` to 30 days from now, never past `expires_at`. Dead refresh tokens answer
  `invalid_grant`. Claude refreshes on a 401 and up to five minutes before expiry, so the person
  never sees it.
- **Use**: each `/mcp` call looks up the access token's hash, checks it is unexpired and that its
  app is live (not revoked, not past either expiry), then touches `last_used_at` at most every five
  minutes, as `keys.touch` does. Revocation therefore stops an app at its next call.
- **Lapse**: an app whose refresh token goes unused for 30 days, or that reaches 90 days from
  consent, stops; the next sign-in asks consent again. Its row moves to the revoked list with
  reason `lapsed`.
- **Strict rotation, not a grace window.** workers-oauth-provider keeps the previous refresh token
  valid until its replacement is first used; cove and this design do not. The cost: a refresh
  response lost in transit ends the app and the person signs in again; the gain: a stolen refresh
  token is detected the first time either party uses it.

Defaults are settings (section 12): `REAM_MCP_ACCESS_TOKEN_TTL_SECS=3600`,
`REAM_MCP_REFRESH_TOKEN_TTL_DAYS=30`, `REAM_MCP_APP_MAX_LIFETIME_DAYS=90`.

## 10. Consent, connected apps, revocation

**The consent screen** (gallery host, gallery look, `frame-ancestors 'none'` as every gallery page,
valid 10 minutes and once):

> **claude.ai** · Verified
> calls itself "Claude"
> wants to act as **Jeremy** on Ream.
> It will send you back to `https://claude.ai/api/mcp/auth_callback`.
>
> [x] Read pages, comments and questions
> [x] Publish, comment and ask, signed "Jeremy, via claude.ai"
> [x] Pass on your answers to questions put to you
>
> [Allow] [Deny]

An Unverified app adds cove's warning: continue only if you started this sign-in from that app
yourself. Allow with nothing ticked is refused on the page. Passing on answers needs reading (an
app cannot find a question it cannot read); ticking it without read is refused on the page with
that reason. The page is a plain form (scripts off it still works), posted with the gallery's
`_same_origin_write` guard and the one-time nonce. Deny sends `error=access_denied` back.

**Remembered consent** (answer 3; cove does not remember). A later sign-in by the same person from
the same `client_id`, to the same return address, asking for no more than the live app holds,
skips the screen and adds a token pair to the existing app. The app's permissions stay what was
ticked; a request for more shows the screen again. Self-registered apps are never remembered:
anyone can register one, and only a checked address can carry consent forward. Revoking an app
forgets its consent.

**In "Machines and keys"** (answer 5): a section "Connected apps" beside "Connected machines", one
row per live app: "Jeremy, via claude.ai" (with the app's own name), its permissions in words,
connected on, last used, lapses on, and **Revoke**, which asks once more (as a machine's does) and
stops the app at its next call. Revoked and lapsed apps fold under "Revoked", as keys do. The
agent equivalents (every human action has one): the app ends itself with `POST /oauth/revoke`;
`artifacts-admin list-apps` and `artifacts-admin revoke-app <id>` for the operator. No agent can
revoke another's app, and no tool lists or revokes apps: keys and connections are never tools.

## 11. Identity, reach and the tools

### 11.1 The person-and-app agent

The first consent from an app makes an agent for that person and app (`mcp_agent`), named
`<person>--<host slug>` (`jeremy--claude-ai`), with `-2`, `-3` for a second app on the same host
(Claude Code's metadata document is also on `claude.ai`). The name passes `keys.AGENT_NAME`, can
never be the owner id, and everything Ream does per agent (whose pages, whose questions, who is
told) works unchanged. Wherever the gallery or the API shows an agent, a person-and-app agent is
shown as "<owner name>, via <host>" (`Jeremy, via claude.ai`). His own gallery acts still say
"Jeremy"; his Claude's are stored as the agent's, never under the owner id.

**Sessions.** MCP carries no session label. A tool may pass one (`session`, same rules as
`X-Ream-Session`); otherwise the work is labelled `<host>/<YYYY-MM-DD>`. With a key (slice 2),
the order is the tool's `session`, then the request's `X-Ream-Session` header, then
`mcp/<YYYY-MM-DD>` (UTC): a key has no app host to name.

### 11.2 Reach (answer 2)

| | A key (unchanged) | A signed-in app |
|---|---|---|
| Read pages | its own, and others' where `agents_can_read` | everything the owner can read, pages closed to agents included; not the trash |
| Read questions | its own; all with `decisions:read-all` | every question on a page it can read |
| Change a page | its own agent's | pages of any of the person's app agents |
| Comment | on its own pages | on any page the person can read |
| Ask | on its own pages | on any page the person can read (11.4) |
| Relay an answer | from `terminal`, `telegram`, `in-person`, `other` | source `mcp`, any open question put to the person, with `relay` |
| Share, change who sees, destroy, give away, keys | never through the API | never |

"Not yours" still answers exactly as "no such page" for a change outside the app's reach. The
reach is one value carried with the caller, `Reach(agent, readable, writable_agents, can_relay,
permissions)`, computed once per request: from the key (as today) or from the app. Store functions
that take `agent` today take the reach where a signed-in app widens it; the API routes pass a key's
reach and behave exactly as before.

### 11.3 Permissions, scopes and tools

| Consent box | OAuth scope | Tools it allows |
|---|---|---|
| Read pages, comments and questions | `ream:read` | `whoami`, `list_pages`, `read_page`, `get_template`, `comments`, `questions`, `whats_new`; every resource and prompt |
| Publish, comment and ask | `ream:write` | `publish_page`, `update_page`, `organise_page`, `delete_page`, `comment`, `ask`, `close_question` |
| Pass on your answers | `ream:relay` | `record_answer` |

`whoami` is always listed. Claude.ai asks for every scope in `scopes_supported`; the person
narrows it on the consent screen and the token response's `scope` says what was granted. A tool
outside the grant is left out of `tools/list`, and calling it anyway answers a tool error naming
the missing permission and saying to reconnect; there is no 403 step-up (cove's choice; Claude.ai's
handling of a second consent is unconfirmed). A key holds all three, within its own reach.

Each tool calls the module functions its route calls, with the caller's reach, never the route
over HTTP; secret scanning, size limits and events are therefore the same. Annotations:
`readOnlyHint` on reads, `destructiveHint` on `delete_page`, `idempotentHint` where true.

| Tool | What it does | Today's route |
|---|---|---|
| `whoami` | who this is ("Jeremy, via claude.ai"), its permissions, its reach | `GET /me` |
| `list_pages` | pages, newest first, by folder | `GET /pages`, `/folders`, `/folders/{slug}/pages` |
| `read_page` | a page's text, or one version's, labelled with the agent that wrote it | `GET /pages/{id}/body`, `/versions`, `/versions/{n}`, `/sections/{el}` |
| `get_template` | the house templates | `GET /templates`, `/templates/{name}` |
| `publish_page` | a new page, HTML or markdown | `POST /pages` |
| `update_page` | a new version, or one section | `PATCH /pages/{id}`, `PATCH /pages/{id}/sections/{el}` |
| `organise_page` | rename, pin, archive, file in a folder, restore | `POST /pages/{id}/rename`, `/pin`, `/archive`, `POST /pages/move`, `/restore` |
| `delete_page` | delete, restorable | `DELETE /pages/{id}` |
| `comments` | a page's comments | `GET /pages/{id}/comments` |
| `comment` | comment, reply, resolve, reopen | `POST /pages/{id}/comments`, `/comments/{id}/resolve`, `/reopen` |
| `ask` | a question with lettered options | `POST /pages/{id}/decisions` |
| `questions` | questions waiting on the person, and those this app asked | `GET /questions`, `GET /decisions` |
| `record_answer` | pass on an answer the person gave in the chat | `POST /decisions/{id}/record` |
| `close_question` | withdraw a question it asked | `POST /decisions/{id}/close` |
| `whats_new` | answers, comments and changes since it last looked | `GET /events` |

`whats_new` keeps its cursor on the server (`mcp_app.events_seen`; for a key, a small per-key row),
because a chat does not remember one: it is the stand-in for a nudge, which Claude.ai cannot
receive. It never waits.

Resources, for a person to attach: `ream://templates/{name}` and `ream://guide` (the shared skill
text). Prompts: "Publish this as a page" and "What is waiting on me". Never offered, as tool,
resource or prompt: who can see a page, share links, email codes, destroy, transfer to another
agent, keys, apps, admin.

### 11.4 Asking on a page that is not the app's

Today a question belongs to its page's agent, who is told the answer (`decision` has no asker
column). Answer 2's "ask anywhere" needs a question whose asker is not the page's agent: a new
nullable `decision.asked_by` (null: the page's agent, so every existing row reads as today). The
answer then goes to `asked_by`; the page's agent is told a question was put on its page, as a
comment would tell it. This is its own slice, after the rest works; until it lands, `ask` works on
the app's own pages and says so.

### 11.5 Passing on an answer (answer 1)

`record_answer(decision_id, letter, words)`: the app passes on what the person said in the chat.
It goes through `decisions.record_elsewhere` with `source='mcp'` and `recorded_by` the app's agent,
so it is a relay, never a tap: it closes the question and tells the agent that asked, exactly as
an office's relay does, and the gallery shows "Jeremy said (b) in claude.ai", recorded by "Jeremy,
via claude.ai", with his words. `words` is required and not empty. Any open question put to the
person, on any page the app can read, not only its own. `mcp` is a core stored source, recorded
only by this path, like `slack`: it is not in `ANSWER_SOURCES`, so a key cannot claim it, and an
app cannot claim any other source. Forms: the same, per field, through `formanswer.relay`.

## 12. Settings

New `Settings` fields, read at call time with `settings.current()`, each `REAM_<NAME>` with the
`ARTIFACTS_<NAME>` twin read by `envnames` (cove's TOML keys, as environment variables):

| Variable | Default | Cove | Meaning |
|---|---|---|---|
| `REAM_MCP_PUBLIC_URL` | unset: `/mcp` off | `[mcp] public_url` | the origin `/mcp` and the issuer live on: the gallery origin or `https://<api host>` |
| `REAM_MCP_ACCEPT_KEYS` | on without sign-in, off with | `accept_api_keys` | section 7 |
| `REAM_MCP_SIGN_IN` | off | `[mcp.oauth] enabled` | `on` turns the authorization server on |
| `REAM_MCP_TRUSTED_CLIENT_HOSTS` | cove's seven | `trusted_client_hosts` | hosts labelled Verified |
| `REAM_MCP_ALLOWED_CLIENT_HOSTS` | unset: any | `allowed_client_hosts` | the only hosts that may ask; `none`: no app |
| `REAM_MCP_SELF_REGISTRATION_REDIRECTS` | `localhost` | `self_registration_redirects` | return hosts open registration allows; `none`: off |
| `REAM_MCP_ACCESS_TOKEN_TTL_SECS` | `3600` | `access_token_ttl_secs` | |
| `REAM_MCP_REFRESH_TOKEN_TTL_DAYS` | `30` | `refresh_token_ttl_days` | the idle lapse |
| `REAM_MCP_APP_MAX_LIFETIME_DAYS` | `90` | `app_max_lifetime_days` | the absolute cap |
| `REAM_OIDC_ISSUER` | unset | (none) | the upstream provider's issuer; its discovery document is read |
| `REAM_OIDC_BACKCHANNEL_URL` | unset | | where Ream's own calls to the provider (discovery, token, keys) go: `scheme://host[:port]` only, needs the issuer. Path and query are kept, `Host` and `X-Forwarded-Proto/Host` say the issuer; every check against the issuer is unchanged; a private address is allowed for this origin only, and plain http only to a private or loopback address |
| `REAM_OIDC_CLIENT_ID` | unset | | Ream's client id there |
| `REAM_OIDC_CLIENT_SECRET_FILE` | unset | | a file holding the client secret, read when needed, never printed |
| `REAM_OIDC_SCOPES` | `openid profile email groups` | | |
| `REAM_OIDC_OWNER_CLAIM` | `preferred_username` | | the claim that names the person |
| `REAM_OIDC_OWNER_VALUE` | the owner id | | the value that claim must have for the one reader |
| `REAM_OIDC_GROUPS_CLAIM` | `groups` | | stored on the app; unused in core |

`problems()` refuses to start, naming every gap at once: sign-in on without the public URL, the
three OIDC values or a readable secret file; a public URL that is neither the gallery origin nor
the API host, or not `https` (loopback `http` excepted, for tests); `REAM_MCP_ACCEPT_KEYS=0` with
sign-in off; a TTL that is not a positive integer. cove has an `enabled` switch beside
`public_url` and a separate `authorize_url`; Ream's convention is that the one value turns a
feature on, and the authorize page is always the gallery's.

## 13. One reader in the core

The core stays single-reader until the company design adds many. The only person who can sign in
is the owner: the login's `REAM_OIDC_OWNER_CLAIM` must equal `REAM_OIDC_OWNER_VALUE`, else the
callback shows "Only Jeremy can sign in to this Ream" and nothing is stored. `person` is stored
(always the owner id today) so that the many-readers work changes who may sign in, not the tables.
At home Authelia's own rule already lets only Jeremy through, with two factors.

## 14. Following cove

Cove (`~/repos/cove`) built MCP with browser sign-in in October 2026. This design takes its
choices unless one of the owner's six answers says otherwise.

**Followed:**
- Paths: `/mcp`; `/.well-known/oauth-protected-resource` (root and `/mcp` forms),
  `/.well-known/oauth-authorization-server`; `/oauth/authorize`, `/oauth/token`, `/oauth/revoke`,
  `/oauth/register`. The issuer is the public origin and the resource is `<origin>/mcp`.
- Split by audience: the anonymous routes on the public host, the browser legs (authorize and
  consent) on the internal, logged-in host.
- Transport: stateless Streamable HTTP, JSON responses, no session ids; 403 on a foreign `Origin`,
  a missing `Origin` passes.
- Tokens: opaque, prefixed (`rmo_`/`rmr_` for cove's `cvo_`/`cvr_`), stored as SHA-256; one hour;
  30 days idle, sliding; a 90-day cap; rotation on every refresh, reuse or another client ends the
  connection; the token works only at `/mcp`, checked against the resource at authorize and
  exchange.
- Client registration: CIMD from any `https` host with cove's fetch fences; open registration as a
  fallback, loopback returns only by default, with cove's limits; exact return-address matching,
  loopback ports per RFC 8252.
- Showing an app: headed by its checked host, its own name second; Verified/Unverified from a
  trusted list (cove's seven by default); an allowed list that refuses before fetching.
- Keys refused at `/mcp` when sign-in is on, accepted beside it by setting, and the only
  credential when sign-in is off; the refusal carries the sign-in challenge.
- The consent interstitial (200, not 302), its 10-minute single use, the Unverified warning.
- No fresh-login requirement at consent, no step-up scopes, scope shortfalls answered inside
  JSON-RPC.
- Vocabulary: "connected app" (one person's consent for one MCP app), "MCP apps that sign in
  with a browser", "consent page", "client metadata document", Verified/Unverified.
- Settings: cove's keys and defaults, one for one, as `REAM_MCP_*` variables.
- The proxy: forward only the anonymous paths around the login (cove's "An MCP-only host" list),
  no Access-style login in front of them.

**Different, and why:**
- **Upstream login.** Cove has none: its consent page trusts the bastion's sign-in. Ream signs the
  person in through OIDC (answer 4), so the homelab trial proves the company path; the consent page
  is still behind Authelia at home as well.
- **Permissions.** Cove offers two access levels as radio buttons (everything except destructive,
  or full). Ream offers three tickable permissions (answer 3), and stores them as scopes on the app.
- **Remembered consent.** Cove makes a new connected app at every Allow. Ream remembers consent per
  person and app until revoked (answer 3), for checked addresses only.
- **Who the app acts as.** Cove's app acts as the user with a key-like source. Ream gives each
  person-and-app pair its own agent, shown "Jeremy, via claude.ai" (the 8 Nov decision that the
  agent field names the client), because Ream's ownership, questions and nudges are per agent.
- **Reach.** Cove derives scopes from its tool catalogue. Ream's reach is wider than a key's for
  reading, commenting and asking, and narrower than the owner's for changing (answer 2).
- **Relaying answers.** Cove has no equivalent; Ream's `record_answer` (answer 1).
- **Codes and sign-in state in the database,** not in memory: Ream runs several processes on one
  database.
- **Revision support.** Cove caps at 2025-11-25; Ream also speaks 2026-07-28, which Claude.ai is
  moving to. Ream has no `GET /mcp` stream (cove streams tool-list changes; Ream's list is fixed).
- **No admin-defined clients** (`[[mcp.oauth.clients]]`) yet: Claude.ai and Claude Code need none.
  A file setting can add them later.
- **No `Host` list** (`extra_hosts`): Ream's host router already admits only its own names.
- **Lapsed apps** are listed (reason `lapsed`) rather than vanishing, as Ream lists revoked keys.

## 15. Security

- **Prompt injection is the big one.** Pages, comments and questions are written by other agents;
  when a person's Claude reads them, that text reaches a model holding the person's powers (the
  "lethal trifecta": private data, untrusted text, a way out). Ream's part: no tool widens who sees
  a page, shares, emails, destroys or transfers; read results are text, never rendered, wrapped in
  a label naming the agent that wrote it and saying it is page content, not instructions; an
  answer is a relay with a source, never a tap, and shows on the page as relayed; a person can
  connect read-only by unticking the other boxes; tool descriptions say to pass on only what the
  person said in this chat. Claude.ai has its own ways out (web fetch, other connectors) that Ream
  cannot close; the docs say so rather than claiming otherwise.
  The fence is applied in one place, deny by default: every tool declares its `Fences` (which
  fields are Ream's own, who wrote the rest), and the registry fences every other string of the
  result, at any depth, before it becomes `structuredContent` and the text block (the text is
  the fenced structure as JSON, so the two agree). A field nobody classified goes out fenced;
  a Ream field holding anything but one plain token is fenced too. Only the owner's own words
  (his comments, his answer's note) and Ream's fixed text (templates, the guide) go back bare;
  `src/ream/mcp_tools.py` lists which fields are which.
- **The confused deputy.** Ream is one fixed client at the upstream provider while acting as a
  server to many apps. So: consent per app at Ream, naming the checked address and the return
  address; return addresses matched exactly; one-time state that dies in ten minutes; a 60-second
  single-use code bound to client, return address, PKCE challenge and resource; the consent page
  cannot be framed and posts only same-origin.
- **Token theft.** Random tokens, hashes at rest, one-hour access tokens, rotated refresh tokens
  with reuse detection, the 30-day idle lapse and the 90-day cap; Revoke stops the next call.
- **Audience.** Every token is bound to `<public url>/mcp`; a token for anything else is refused,
  and Ream never forwards a token anywhere (the specs forbid passthrough).
- **Fetching someone else's address.** The client metadata fetch is fenced (section 5): public
  addresses only, pinned, no redirects, small, quick.
- **Page content keeps its own origin.** MCP never serves a page to a browser; links it returns
  point at the gallery, which frames the page from its content origin as today. Publishing through
  MCP is scanned for secrets exactly as through the API.
- **No secret leaves the server.** The OIDC client secret is read from a file when used; the
  upstream ID token and access token are discarded once the person is known. Ream holds no upstream
  refresh token.

## 16. Homelab asks (for the homelab office)

For the trial from Claude.ai, in the homelab repo (Ream's own manifests and Authelia's):

1. **An Authelia OIDC client for Ream.** `client_id: ream`, a confidential client
   (`client_secret_basic`), redirect URI `https://ream.tenfourty.site/oauth/callback`, scopes
   `openid profile email groups`, PKCE S256 required, `authorization_policy: two_factor`, and a
   consent mode that does not ask a second time (Ream asks). It goes in the one
   `authelia-oidc-clients` secret file, beside the forge's and Grafana's, followed by Authelia's
   restart.
2. **Authelia bypass rules on `ream.tenfourty.site`** for exactly `^/mcp$`,
   `^/\.well-known/oauth-protected-resource(/mcp)?$`, `^/\.well-known/oauth-authorization-server$`
   and `^/oauth/(token|revoke|register)$`, beside the existing `^/api/v1/` bypass. `/oauth/authorize`
   and `/oauth/callback` stay behind the login. Nothing changes on `artifacts.tenfourty.site`.
3. **Cloudflare bot protection.** Claude.ai calls from Anthropic's egress range `160.79.104.0/21`.
   Bot Fight Mode is on for every path on the free plan; find whether that range gets through to
   the paths above (an IP access rule, or another way), or prove it does. The first trial tests it.
4. **Ream's settings** in `artifacts-config.env`: `REAM_MCP_PUBLIC_URL=https://ream.tenfourty.site`,
   `REAM_MCP_SIGN_IN=on`, `REAM_OIDC_ISSUER=https://authelia.tenfourty.site`,
   `REAM_OIDC_CLIENT_ID=ream`, `REAM_OIDC_CLIENT_SECRET_FILE=<mounted path>`, and for the first trial
   `REAM_MCP_ALLOWED_CLIENT_HOSTS=claude.ai`.
5. **Secrets, by name only:** the OIDC client secret (one sops secret holding the value Ream reads
   and the hash Authelia's client entry holds), mounted as a file into the Ream pod. No signing key
   is needed: Ream's tokens are random and hashed, and it signs nothing.
6. **The network policy** lets the Ream pod reach Authelia's OIDC endpoints (in-cluster or by its
   public name) and, for client metadata documents, `https` on the public internet.

## 17. Core and configuration

**Core, off unless a deployment turns it on:** `/mcp` and its tools; the authorization server (its
two documents, the authorize, consent, token, revoke and register routes); login through any
standard OIDC provider; person-and-app agents; connected apps in "Machines and keys"; the `mcp`
relay source; `decision.asked_by`. A setting half filled in refuses to start.

**Configuration only:** which provider (Authelia is just an OIDC provider), the owner claim, the
lists of trusted and allowed hosts, the lifetimes, the public URL and the cluster entries above. No
plug-in is needed. A plug-in could later turn company groups into folders or teams, once the company
design says what groups mean.

## 18. The company deployment

The same code, configured differently:

- The provider is the company sign-on (`REAM_OIDC_*`); groups are read from the login and stored on
  the app.
- **Many readers.** Each person gets their own identity, so Alice's Claude acts as Alice. This needs
  the many-readers work from the company deployment's design; section 13's single-reader check is
  where it plugs in.
- An owner of the company's Claude organisation adds Ream once for everyone (Organisation settings,
  Connectors); each person clicks Connect and signs in.
- `REAM_MCP_ALLOWED_CLIENT_HOSTS=claude.ai` to start.
- Reachable from Anthropic's cloud over the public internet (Claude.ai refuses addresses that
  resolve to a private network): a company that keeps Ream internal needs a public front door for
  the anonymous paths, restricted to Anthropic's published ranges. The API host can be that door.
- **Open for the company design:** its rule that a session lasts at most 12 hours, so leaving a
  team takes effect within a day (8 Nov, answer 7), meets this design's 30-day apps, whose groups
  are read only at sign-in. Either the company sets `REAM_MCP_APP_MAX_LIFETIME_DAYS` low (daily
  sign-ins in Claude.ai), or Ream keeps the upstream refresh token and re-reads groups at each of
  its own refreshes. That choice belongs to the company build.

## 19. Testing

All in CI on both databases (never on baba). The transport and tools against `TestClient` with
recorded requests from each revision; the authorization server against a fake OIDC provider served
in-process (a stub behind the module's one fetch function, issuing ID tokens with chosen
claims) and fake client metadata documents on the same seam; the address guard with literal private,
loopback and link-local answers; every refusal proved with the literal input a real client sends
(a real Claude.ai `client_id` URL, the real callback, a 43-character verifier). The consent page in
the browser tests (scripts off and on). The end-to-end trial from Claude.ai is the homelab slice's,
by hand, and says which it checked: the token reached `/mcp`, and a page published from a chat shows
in the gallery as "Jeremy, via claude.ai".
