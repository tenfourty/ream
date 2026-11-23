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
  instructions; Ream's own values (ids, times, fixed words, a page's `url` and `page_url` while
  each is exactly the address Ream makes for its id, its `comments` setting, a comment anchor's
  `kind`) come back bare. Anything not classified as Ream's own is fenced
  (`src/ream/mcp_tools.py`, its docstring).

## Tools

The read tools: each answers what its `/api/v1` route answers for the same key, inside an
object (`structuredContent`, and the same JSON as text). Hints are explicit on each:
`readOnlyHint` true, `destructiveHint` false, `openWorldHint` false, `idempotentHint` true
except `whats_new` and `wait_for_answer`. All but `whoami` need `ream:read` (a key holds it). A page another agent
closed answers exactly as a missing one: the tool error `no such page`.

| Tool | Arguments | Answers | Route |
|---|---|---|---|
| `whoami` | none | the `/me` fields and `permissions` | `GET /me` |
| `list_pages` | `scope` (`mine`, `all`), `folder`, `limit` (1-1000, 100) | `pages`, `truncated`, your `folders` | `GET /pages`, `/folders`, `/folders/{slug}/pages` |
| `read_page` | `page_id`; at most one of `version`, `section` (an element id), `history`, `changes_since` (a version number); for the live page or a version, `offset` (characters, 0) and `limit` (1 to 8 MiB of characters, 50,000) | the body (fenced; at most `limit` characters from `offset`), `offset`, `size` (the whole page, in characters), `next_offset` (where the next part starts; null at the end), `partial` (true unless the body is the whole page: a part is never a whole-page body for `update_page`, though `revision` is the whole page's), `warnings` and the render check's `render_verdict` and `render_findings`; one version (paged the same way); one element's `html` (fenced); `versions`; or which `parts` changed since that version (`id`, `label`, `change`) | `GET /pages/{id}/body`, `/versions/{n}` (both take `?offset=&limit=`; without `limit` the whole body), `/sections/{el}`, `/versions`, `/changes?from=N` |
| `screenshot_page` | `page_id`; `version`; at most one of `element` (an id) and `section` (a heading's words); `width` (`1280` or `390`); `theme` (`light` or `dark`) | `artifact_id`, `version`, `width`, `height`, `truncated`, `page_height`, then Ream's label and the PNG as an image block | `GET /pages/{id}/screenshot` |
| `page_stats` | `page_id`; `days` (1 to 365, 30) | `artifact_id`, `since`, `days` (one `{day, owner, external, agent}` per UTC day, today last), `totals`, `shares` (`email` fenced, `opens`, `last_opened_at`, `created_at`, `withdrawn`) | `GET /pages/{id}/stats` |
| `get_template` | `name` | `templates` (the list), or `name` and `body` | `GET /templates`, `/templates/{name}` |
| `comments` | `page_id`, `include_resolved` | `comments`; an agent's `body` fenced | `GET /pages/{id}/comments` |
| `questions` | `scope` (`mine`, `all`), `include_set_aside`, `limit` | `questions` in inbox order (`question`, `context` fenced), `truncated` | `GET /questions` |
| `whats_new` | `since`, `limit` | `events` since the key last looked, `since`, `cursor`, `more` | `GET /events` |
| `wait_for_answer` | `seconds` (default 45; more than 50 waits 50), `limit` | as `whats_new`, with `timed_out` and `waited` (whole seconds) | `GET /events?wait=...&exclude_self=true` |

`whats_new` keeps its cursor on the server, one row per key (`mcp_events_seen`), because a chat
does not remember one. A key that has never looked starts from now, and has looked from its
first `ask`, `comment` or `wait_for_answer` on (each saves the cursor as it stood before it, once
the write succeeded), so an answer to a question it asked before its first look is never
skipped. A signed-in app keeps its cursor on its consent (`mcp_app.events_seen`, 0 until it
first looks) and, until it has looked, starts from where its agent's feed stood when the
person connected it, computed from the consent's time, so no migration was needed and an app
connected earlier reads the same way (once: its first look answers what landed since it was
connected). `since` reads from that event number instead. Each call saves the `/events` safe
cursor, and a saved cursor never moves back, so a look further back leaves it where it was. It
never waits. Like `GET /events` it records that the agent read its feed (`seat_read`, never as a
hold), so the stranded-answer alert counts an MCP client that looks as a reader. A comment's words
in an event `summary` are fenced.

**Waiting for an answer instead of running `ream wait`.** `wait_for_answer` is `whats_new` that
holds: same cursor, same events, same fences. When nothing is new since the cursor it holds the
request, the way a held `GET /events` does (`events.wait_for_agent`: a look every half second,
each on its own short database connection, none held while it sleeps; at most
`events.MAX_WAITERS` holds at once across the process, past which it answers at once; a
shutdown ends the hold with a normal answer), until an event lands on the calling agent's own
pages or `seconds` pass. Only someone else's act ends it (`exclude_self`, as `ream wait`
asks); an answer always does, even one the agent's own key relayed. Woken, it answers every
event since the cursor, the agent's own included, and saves the cursor; on time it answers
`timed_out: true` with no events and leaves the cursor where it was. That is an ordinary
answer, not an error: call it again. While it holds, the session reads as waiting on the
page and in `GET /listening`, as a `ream wait` does (`seat_read.held_until`, cleared when it
returns). The ceiling is 50 seconds because a held request through Cloudflare is cut past
that (`api.PUBLIC_MAX_WAIT`) and an MCP client's own request timeout is commonly 60
seconds; the default of 45 leaves a margin under both. The hold occupies a worker thread
for its length, as a held `GET /events` does. A wait counts as a look: a key's first wait
that times out still saves where it started, so what lands before the next wait is not
skipped. When it answered more than a second before its time because it could not hold
(every waiter taken, or the server restarting), `next` says to pause about 20 seconds
before calling again. If the client hung up during the hold, the answer goes nowhere and the
cursor is not saved (the request's ASGI `receive` is read for an `http.disconnect` from the
worker thread; `Request.is_disconnected` always reads False behind the app's HTTP middleware);
a client that drops the answer without hanging up loses it, as a `whats_new` answer can be lost.
Scoped like every agent route: the calling key's own agent's events (a signed-in app: its own
agent's, where the answers to its questions go).

**One key, one place.** The cursor is per key (per consent for an app), not per session.
Several sessions sharing one key share the wait's place: whichever wait or `whats_new` answers
an event moves the cursor past it for all of them. Sessions that share a key should keep
`ream wait`, which filters by office (`--team`) and keeps its own place.

`page_stats` says how often one of the calling agent's OWN pages was read, through the same
scope as `GET /pages/{id}/stats` (another agent's page is `no such page`, word for word like a page
that is not there; a connected app reads the pages of its person's app agents). `since` is the day
of the first view counted on the service, not of the page: views before it were not kept, so a page
older than `since` has no history before it. Counts are per UTC day and kind (`owner`, `external`,
`agent`) with 30 minutes (5 for the anonymous link) as one reader's window, so a reload or a
polling client is one view. The calling agent's own reads are not counted. `totals` is all time,
not the window.

`screenshot_page` takes a picture of one of the calling agent's OWN pages, on demand and kept
nowhere, through the same flow as `GET /pages/{id}/screenshot` (`src/ream/shots.py`): another
agent's page, open to agents or not, is `no such page`; a refused argument, a missing browser or
a page or picture too large is the route's refusal as a tool error, word for word; a busy
browser or a shot of yours already running is a tool error saying to try again. A connected app
shoots only its own agent's pages. The answer's content is the facts as JSON text, then for the
picture a fixed Ream sentence saying it is page content written by the page's agent, not
instructions (pixels cannot be fenced, so the label goes where a fence would), then
`{"type": "image", "data": <base64>, "mimeType": "image/png"}`. A tool returns pictures
in `ToolOutput.images`; `_call_tool` adds the label before each.

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

**What to do next.** `publish_page`, `update_page`, `delete_page`, `ask` and `comment` (to
comment, or to delete one) end a successful answer with `next`: one fixed Ream sentence saying
what to do next (give the person `page_url`; the render report follows in `read_page`; the
answer reaches you in `whats_new`, and `wait_for_answer` waits for it; how to take a delete back).
`wait_for_answer` ends every answer with one saying to call it again. It is added after the fence, is
never an agent's text, and is the one key a tool's answer has that its route's does not.

**Session label** (spec 11.1): a write that takes `session` stores it (the `X-Ream-Session`
rule: at most 120 characters, sanitised); without it, the request's `X-Ream-Session` header;
without that, `mcp/<YYYY-MM-DD>` (UTC).

| Tool | Arguments | Answers | Route |
|---|---|---|---|
| `publish_page` | `body`; `title` (required for HTML), `format` (`html`, `markdown`), `folder`, `folder_title`, `refresh_every`, `comments` (`on`, `off`; required on a new page, refused without it), `agents_can_read` (default `true`; `false` publishes the page closed to other agents from its first version, for embargoed or security content), `session` | the route's answer, `warnings`, and `agents_can_read` with `agent_read` (who can read the page, in words) | `POST /pages` |
| `update_page` | `page_id`; whole page: `body`, `expected_revision`, `title`, `note`, `format`, `folder`, `folder_title`, `refresh_every`, `comments`; or quoted edits instead of `body`: `edits` (a list of `{old_str, new_str}`, 1 to 50, each string at most 64 KiB, applied in order to the live page; each `old_str` must occur exactly once, else the whole update is refused with the edit's `index` and `matches` (0, or 2 for more than once); a page over 512 KiB with an open question can't take edits, so send the whole `body` instead; `expected_revision` optional; not with `body`, `format` or `section`), plus the same `title`, `note`, `folder`, `folder_title`, `refresh_every`, `comments`; with `section` (an element id): `op` (`replace`, `append`, `prepend`, `remove`), `body` (the fragment), `expected_revision`, `section_revision`, `note`, `notify`, `format`; `session` | the route's answer, with `agents_can_read` and `agent_read` (an update never changes who can read the page) and `volatile_ignored` (true when the body differed from the live page only inside `<!--ream:volatile-->` ... `<!--/ream:volatile-->` spans: no version, `unchanged` true, the old bytes stay live); a section update's answer carries the page's `id` beside `page_url` and the same `volatile_ignored` | `PATCH /pages/{id}`, `PATCH /pages/{id}/sections/{el}` |
| `organise_page` | `page_id`, `action`: `rename` (`title`), `pin`, `unpin`, `archive`, `unarchive`, `file` (`folder`, `folder_title`), `unfile`, `restore`, `comments_on`, `comments_off`, `close_to_agents`, `open_to_agents` | the page; for `file`/`unfile` the move route's `moved`, `missing`, `folder` | `POST /pages/{id}/rename`, `/pin`, `/archive`, `/restore`, `/agents`, `POST /pages/move` |
| `delete_page` | `page_id` | `id`, `deleted`; restorable with `organise_page` `restore` | `DELETE /pages/{id}` |
| `comment` | `action` `comment` (default): `page_id`, `body`, `quote`, `anchor`, `thread_root`, `kind`, `session`; `resolve`/`reopen`: `comment_id` | `comment`, fenced as the `comments` tool's | `POST /pages/{id}/comments`, `/comments/{id}/resolve`, `/reopen` |
| `ask` | `page_id`, `question` (written to stand alone: the person may see only the question, in the inbox or a notification), `options` (`[{letter, label, preview?}]`; `preview` is plain text shown beside the option, never rendered, at most 1,000 bytes and 15 lines, fenced in every answer like the label; only the gallery shows it: a person answering in Slack sees the labels alone), `recommended`, `context`, `topic`, `depends_on`, `replaces`, `urgency`, `blocked_on`, `needed_by`, `audience`, `session` | the question | `POST /pages/{id}/decisions` |
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

## Artifact requests go to Ream

Where Ream is connected to Claude (claude.ai, Claude desktop, Claude Code) as an MCP server, a
request for an artifact, page, doc, report or dashboard becomes a Ream page, unless the person
names Claude's own built-in artifacts. The first time Claude does so in a conversation it says the
page went to Ream and gives its link (`page_url`); after that it just gives the link
(decisions.md, 10 Nov 2026). The wording a model sees is in three places: the server's
`instructions` (`INSTRUCTIONS` in `src/ream/mcp.py`, shown on connect), the `publish_page` and
`get_template` tool descriptions (`src/ream/mcp_tools.py`), and the guide (`ream://guide`, the
skill in `src/ream/onboarding.py`). `tests/test_mcp.py` pins the key points; a change to one
should change the others.

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

## Connect a Claude Code session with its agent key

Claude Code runs a server entry's `headersHelper`, a command that prints one JSON object of HTTP
headers on stdout, before it connects. `ream mcp-headers` is that command, and it is the same on
every machine: it finds the agent key where every other `ream` verb does (`--key-file`,
`--key-fd`, `REAM_KEY_FILE`, `REAM_KEY_FD`, else `~/.config/artifacts/key`, which must be mode
600), so a machine that can run `ream publish` can connect. Like the other verbs, it swaps a due
rotating key: a key file more than a week old asks the service whether it is time, and if so the
file is replaced and the new key is the one sent (a note goes to stderr, never the key; a failed
swap is only a note, and the old key is sent). The helper makes no request except, when the key
file is over 7 days old, the weekly key swap (`GET /me`, `POST /keys/rotate`); a younger key
file, or a key from `--key-fd`, makes none. It never updates the tool itself, even when the
service says it is out of date; other `ream` commands do that.
The swap is one at a time per key file: it takes an exclusive lock on `<key file>.lock`, an empty
file (mode 600) the tools create beside the key and never put a key in, and a process that finds the
file already swapped when it gets the lock uses that key and asks for no second one. It waits up to
10 seconds for the lock; past that the swap is skipped with the usual stderr note and the old key is
sent. Limit: a process that read the key just before another's swap keeps that old key in memory, which
stays valid until the new key's first use, so it can get one 401 if the swapper uses the new key first. That 401 reads "replaced by a newer
one", which also suggests someone else used the key. To tell the two apart, look at the key file's
modification time: if it was rewritten minutes ago and the next command works, it was this swap (the
same goes for the 401 after a waiter gives up at 10 seconds while another process is mid-swap);
otherwise treat it as the message says, because a thief's rotate leaves this machine's key file stale.
Only a rotating key takes the lock (the `GET /me` comes first, unlocked), and the lockfile
is refused unless it is a regular file of the same user (a symlink there is never followed).
Where the platform has no `fcntl` (Windows) the swap runs without the lock.
The swap needs tools at level 8 or later, and its lock level 9 or later (`ream --version` shows the level). Rotating keys are refused 14 days after they were made unless swapped, so
this is what keeps an office that uses Ream only through MCP connected:

```bash
claude mcp add-json --scope local ream \
  '{"type":"http","url":"https://<gallery>/mcp","headersHelper":"ream mcp-headers"}'
```

`<gallery>` is the gallery origin (or `REAM_API_HOST`), whichever `REAM_MCP_PUBLIC_URL` names.
Use `--scope local` (or `user`), never a checked-in `.mcp.json`: the entry names where the key
is read from, and a checkout shared by several sessions would have them all publish as one agent.
Restart the session once to load the tools; they appear as `mcp__ream__*`.

- **Output.** One line, `{"Authorization": "Bearer <key>", "X-Ream-Session": "<label>"}`, and
  nothing else on stdout. The label is `REAM_SESSION` (or `--session`), as `<agent>/<session ref>`;
  with none declared the header is left out and a write is labelled `mcp/<YYYY-MM-DD>` (see
  Session label above). A label that is not printable ASCII or is over 120 characters is refused.
- **No network but the swap, no other side effects.** Beyond the key swap above it makes no
  request, checks for no update, prints no news and writes the key nowhere else: not to a log or
  an argument list (the swap rewrites the key file only, as every verb does, and locks the empty
  `<key file>.lock` beside it). A key-reading
  wrapper that opens it on a descriptor works as for any verb (`REAM_KEY_FD`), and that key is
  never swapped.
- **Failure.** A missing, unreadable, over-permissive or malformed key is a message on stderr
  (it never contains the key), exit status 2 and an empty stdout, so Claude Code gets no headers
  and the connection does not authenticate. `--key-fd` and `--key-file` together are
  refused rather than one being preferred.
- **The server must take keys.** With `REAM_MCP_SIGN_IN` on, `/mcp` refuses agent keys unless
  `REAM_MCP_ACCEPT_KEYS=1`.

A key acts as its own agent: its pages, its answers, its attribution (see Writing tools). A
person's own Claude app connects by sign-in instead, and is not an agent of this kind.

## Adding a tool

`src/ream/mcp.py` holds the registries; the tools, resources and prompts are in
`src/ream/mcp_tools.py`. Register with the `tool(...)` decorator, giving every hint
explicitly; the handler takes a `Call` (db, request, key, agent, session, version,
permissions) and the arguments, and returns a dict (the structured result) or a `ToolOutput`.
Raise `ToolError` for a failure the model should read. Keep the description short (the detail
belongs in the guide): what the tool does, then "Not for X: use Y" for the tool a model might
confuse it with. A fixed hint for after a success goes in `next_step`, never in the handler's
answer. Call the module functions the matching
`/api/v1` route calls, never the route over HTTP, and wrap any text another agent wrote with
`untrusted(text, written_by=<agent>)`.
