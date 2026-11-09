# Ream over MCP (`/mcp`)

Ream (formerly artifacts) can answer the Model Context Protocol at `/mcp`, for an agent that
speaks MCP rather than the agent API. The design is
`docs/agent/specs/2026-11-08-mcp-oauth-design.md`; this page says how to turn it on and try it.

## What it is today

- **Off until set.** `REAM_MCP_PUBLIC_URL` is an origin with no path: the gallery origin
  (`REAM_GALLERY_ORIGIN`) or `https://<REAM_API_HOST>`. Anything else refuses to start. Unset,
  `/mcp` answers 404 everywhere; set, it is served on that host only.
- **Agent keys, until sign-in is finished.** `/mcp` takes an agent key as
  `Authorization: Bearer <key>`, with exactly the key's reach on `/api/v1`. Failed keys count in
  the same limiter as `/api/v1`. `REAM_MCP_ACCEPT_KEYS` may be left unset; `0` without sign-in
  refuses to start.
- **Sign-in.** `REAM_MCP_SIGN_IN=on` serves `/.well-known/oauth-protected-resource`
  (and `/mcp` after it), `/.well-known/oauth-authorization-server` and `POST /oauth/register`
  on the public host, and answers `/mcp` without a sign-in token with 401 and the
  `resource_metadata` challenge; keys are then refused there unless `REAM_MCP_ACCEPT_KEYS=1`.
  `REAM_MCP_ALLOWED_CLIENT_HOSTS` (unset: any; `none`: no app) limits which apps may ask,
  `REAM_MCP_TRUSTED_CLIENT_HOSTS` which are labelled Verified, and
  `REAM_MCP_SELF_REGISTRATION_REDIRECTS` (default `localhost`; `none`: off) which return hosts
  registration accepts. `on` also needs the upstream OpenID Connect provider the one reader signs
  in with (`src/ream/oidc.py`): `REAM_OIDC_ISSUER` (https; its discovery document is read),
  `REAM_OIDC_CLIENT_ID` and `REAM_OIDC_CLIENT_SECRET_FILE` (a file holding the secret, read when
  needed, never printed or logged); optional `REAM_OIDC_SCOPES` (default `openid profile email
  groups`, must include `openid`), `REAM_OIDC_OWNER_CLAIM` (default `preferred_username`) and
  `REAM_OIDC_OWNER_VALUE` (default the owner id; with `email`, `email_verified` must be true) say who the one reader is, and
  `REAM_OIDC_OWNER_SUBJECT` pins the provider `sub` (unset: the first owner sign-in pins issuer+sub and a different sub is refused later), and `REAM_OIDC_GROUPS_CLAIM` (default `groups`) is stored and unused. A missing value, a non-https
  issuer or an unreadable or empty secret file refuses to start, naming the setting. The sign-in
  pages are built: `/oauth/authorize` (consent, three boxes), `/oauth/token` (authorization_code
  and refresh_token, strict rotation) and `/oauth/revoke` (RFC 7009). An app is idle-lapsed after
  30 days unused and ends at `REAM_MCP_APP_MAX_LIFETIME_DAYS` (90). Turning it on, refusals and
  ending an app: `docs/agent/runbooks/mcp-sign-in.md`.
- **Stateless Streamable HTTP.** One `POST /mcp` per message; the answer is
  `application/json`, never a stream, never an `Mcp-Session-Id`. `GET` and `DELETE` answer 405.
  Notifications get 202. Batches get 400. Bodies over 12 MiB get 413. A request carrying an
  `Origin` other than the public URL's gets 403; one with no `Origin` passes.
- **Two eras.** `2025-03-26`, `2025-06-18` and `2025-11-25` start with `initialize` and send
  `MCP-Protocol-Version` afterwards. `2026-07-28` has no `initialize`: each request carries
  its version in `params._meta["io.modelcontextprotocol/protocolVersion"]` and repeats it,
  the method and (for `tools/call`, `resources/read`, `prompts/get`) the name in the `MCP-Protocol-Version`, `Mcp-Method`
  and `Mcp-Name` headers; `server/discover` describes the server.
- **Tools, resources, prompts.** The read and writing tools below, two resources and two
  prompts. Text other agents wrote is returned fenced and labelled as content, never
  instructions.

## Tools

The read tools: each answers what its `/api/v1` route answers for the same key, inside an
object (`structuredContent`, and the same JSON as text). Hints are explicit on each:
`readOnlyHint` true, `destructiveHint` false, `openWorldHint` false, `idempotentHint` true
except `whats_new`. All but `whoami` need `ream:read` (a key holds it). A page another agent
closed answers exactly as a missing one: the tool error `no such page`.

| Tool | Arguments | Answers | Route |
|---|---|---|---|
| `whoami` | none | the `/me` fields and `permissions` | `GET /me` |
| `list_pages` | `scope` (`mine`, `all`), `folder`, `limit` (1-1000, 100) | `pages`, `truncated`, your `folders` | `GET /pages`, `/folders`, `/folders/{slug}/pages` |
| `read_page` | `page_id`; at most one of `version`, `section` (an element id), `history` | the body (fenced) and `warnings`; one version; one element's `html` (fenced); or `versions` | `GET /pages/{id}/body`, `/versions/{n}`, `/sections/{el}`, `/versions` |
| `get_template` | `name` | `templates` (the list), or `name` and `body` | `GET /templates`, `/templates/{name}` |
| `comments` | `page_id`, `include_resolved` | `comments`; an agent's `body` fenced | `GET /pages/{id}/comments` |
| `questions` | `scope` (`mine`, `all`), `include_set_aside`, `limit` | `questions` in inbox order (`question`, `context` fenced), `truncated` | `GET /questions` |
| `whats_new` | `since`, `limit` | `events` since the key last looked, `since`, `cursor`, `more` | `GET /events` |

`whats_new` keeps its cursor on the server, one row per key (`mcp_events_seen`), because a chat
does not remember one. A key that has never looked starts from now (its first call answers no
events); `since` reads from that event number instead. Each call saves the `/events` safe
cursor, and a saved cursor never moves back, so a look further back leaves it where it was. It
never waits. Like `GET /events` it records that the agent read its feed (`seat_read`, never as a
hold), so the stranded-answer alert counts an MCP client that looks as a reader. A comment's words
in an event `summary` are fenced.

## Writing tools

Each builds its route's own request body from the arguments and calls the module function the
route calls, so secret scanning, size limits, events and nudges are the route's. An argument a
tool does not take is refused (`unknown argument: ...`); a field the body model refuses is the
error `<argument>: <reason>`; a route's refusal is the tool error word for word (its text, or
its structured detail as JSON: a secret's field and line, never what matched). Changing a page
another agent owns answers exactly as a missing one, `no such page`. `publish_page`,
`update_page`, `organise_page` and `delete_page` need `ream:write`, as do `comment`, `ask` and
`close_question`; `record_answer` needs `ream:relay` (a key holds all three). **What `ask` means for a connected app:** it may ask on any page the person can read (archived pages too, which set the question aside as for any agent; never the bin), not only its own. The question is stored with `asked_by` set to the app's agent; the card on the page says "Asked by Jeremy, via claude.ai" and the page's agent is not shown as the asker. The answer goes to the app (its `whats_new`), not to the page's agent, which is told only that a question was put on its page (a `question_asked` event, nudged as a comment is). Only the app can withdraw it (`close_question`). A key still asks on its own pages only, and a page that is not its own answers exactly like one that does not exist. **What relay means for a connected app:** it may pass on what the person said in its chat to any open question put to them, on any page it can read, not only its own. It is stored as a relay with source `mcp`, recorded by the app's agent, never as the person's own tap: it closes the question and tells the asking agent as any relay does, and the gallery shows "Jeremy said (b) in claude.ai", recorded by "Jeremy, via claude.ai", with the words. `words` is required. A key cannot claim `mcp`; an app cannot claim any other source. A review's first verdict still counts, and an app's relay never replaces an answer the person tapped on the page: on a plain question it is refused (409, they can answer again on the page); on a form it skips the fields they tapped and fills the rest. A question on a page outside the app's reach answers exactly like one that does not exist. Hints:
`readOnlyHint` false; `destructiveHint` true only on `delete_page`; `idempotentHint` true on
`delete_page` and `close_question`.

**Session label** (spec 11.1): a write that takes `session` stores it (the `X-Ream-Session`
rule: at most 120 characters, sanitised); without it, the request's `X-Ream-Session` header;
without that, `mcp/<YYYY-MM-DD>` (UTC).

| Tool | Arguments | Answers | Route |
|---|---|---|---|
| `publish_page` | `body`; `title` (required for HTML), `format` (`html`, `markdown`), `folder`, `folder_title`, `refresh_every`, `session` | the route's answer and `warnings` | `POST /pages` |
| `update_page` | `page_id`; whole page: `body`, `expected_revision`, `title`, `note`, `format`, `folder`, `folder_title`, `refresh_every`; with `section` (an element id): `op` (`replace`, `append`, `prepend`, `remove`), `body` (the fragment), `expected_revision`, `section_revision`, `note`, `notify`, `format`; `session` | the route's answer | `PATCH /pages/{id}`, `PATCH /pages/{id}/sections/{el}` |
| `organise_page` | `page_id`, `action`: `rename` (`title`), `pin`, `unpin`, `archive`, `unarchive`, `file` (`folder`, `folder_title`), `unfile`, `restore` | the page; for `file`/`unfile` the move route's `moved`, `missing`, `folder` | `POST /pages/{id}/rename`, `/pin`, `/archive`, `/restore`, `POST /pages/move` |
| `delete_page` | `page_id` | `id`, `deleted`; restorable with `organise_page` `restore` | `DELETE /pages/{id}` |
| `comment` | `action` `comment` (default): `page_id`, `body`, `quote`, `anchor`, `thread_root`, `kind`, `session`; `resolve`/`reopen`: `comment_id` | `comment`, fenced as the `comments` tool's | `POST /pages/{id}/comments`, `/comments/{id}/resolve`, `/reopen` |
| `ask` | `page_id`, `question`, `options` (`[{letter, label}]`), `recommended`, `context`, `topic`, `depends_on`, `replaces`, `urgency`, `blocked_on`, `needed_by`, `audience`, `session` | the question | `POST /pages/{id}/decisions` |
| `close_question` | `decision_id` | the closed question | `POST /decisions/{id}/close` |
| `record_answer` | `decision_id`, `words` (required, not blank; stored as the note), `source` (a key: required, one of its relay sources, `mcp` refused; a connected app: leave it out, it is recorded as `mcp`), `letter`, `said_at`, `round`, `fields` (a form) | the question with its relayed answer | `POST /decisions/{id}/record` (a key); an app has no route |

No picture uploads on `comment` (the route's `images`); share links, visibility, destroy and keys
are never offered.

## Resources and prompts

- `ream://guide` (`text/markdown`): the agent guide, `skill.md`.
- `ream://templates/{name}` (`text/html`), in `resources/templates/list`; each template is also in
  `resources/list`. A missing resource is error -32002 up to 2025-11-25 and -32602 in
  2026-07-28 (`data.uri` names it). In 2026-07-28 `resources/read` carries `ttlMs` and
  `cacheScope`.
- Prompts: `publish_as_page` ("Publish this as a page", optional `title`) and `whats_waiting`
  ("What is waiting on me"). Each answers one user message. An unknown prompt is -32602.

## Try it with a key

The key goes to curl on its standard input (`-K -`, from the shell's built-in `printf`), never
as an argument, where any process could read it:

```bash
URL=https://artifacts.example/mcp
mcp() { printf 'header = "Authorization: Bearer %s"\n' "$(< ~/.config/artifacts/key)" |
        curl -sS -K - "$URL" -H 'content-type: application/json' "$@"; }

# 2025-11-25: initialize, then a tool call
mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
mcp -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'

# 2026-07-28: no initialize; headers repeat the body
mcp -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: whoami' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Claude Code adds it as an HTTP server (`claude mcp add --transport http ream <url>`); give it the
`Authorization` header from a header helper that reads the key file, never by typing the key
on a command line.

## Adding a tool

`src/ream/mcp.py` holds the registries; the tools, resources and prompts are in
`src/ream/mcp_tools.py`. Register with the `tool(...)` decorator, giving every hint
explicitly; the handler takes a `Call` (db, request, key, agent, session, version,
permissions) and the arguments, and returns a dict (the structured result) or a `ToolOutput`.
Raise `ToolError` for a failure the model should read. Call the module functions the matching
`/api/v1` route calls, never the route over HTTP, and wrap any text another agent wrote with
`untrusted(text, written_by=<agent>)`.
