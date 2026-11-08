# Ream over MCP (`/mcp`)

Ream (formerly artifacts) can answer the Model Context Protocol at `/mcp`, for an agent that
speaks MCP rather than the agent API. The design is
`docs/agent/specs/2026-11-08-mcp-oauth-design.md`; this page says how to turn it on and try it.

## What it is today

- **Off until set.** `REAM_MCP_PUBLIC_URL` is an origin with no path: the gallery origin
  (`REAM_GALLERY_ORIGIN`) or `https://<REAM_API_HOST>`. Anything else refuses to start. Unset,
  `/mcp` answers 404 everywhere; set, it is served on that host only.
- **Agent keys only.** Browser sign-in is not built yet, so `/mcp` takes an agent key as
  `Authorization: Bearer <key>`, with exactly the key's reach on `/api/v1`. Failed keys count in
  the same limiter as `/api/v1`. `REAM_MCP_ACCEPT_KEYS` may be left unset; `0` refuses to start
  until sign-in exists.
- **Stateless Streamable HTTP.** One `POST /mcp` per message; the answer is
  `application/json`, never a stream, never an `Mcp-Session-Id`. `GET` and `DELETE` answer 405.
  Notifications get 202. Batches get 400. Bodies over 12 MiB get 413. A request carrying an
  `Origin` other than the public URL's gets 403; one with no `Origin` passes.
- **Two eras.** `2025-03-26`, `2025-06-18` and `2025-11-25` start with `initialize` and send
  `MCP-Protocol-Version` afterwards. `2026-07-28` has no `initialize`: each request carries
  its version in `params._meta["io.modelcontextprotocol/protocolVersion"]` and repeats it,
  the method and (for `tools/call`, `resources/read`, `prompts/get`) the name in the `MCP-Protocol-Version`, `Mcp-Method`
  and `Mcp-Name` headers; `server/discover` describes the server.
- **Tools, resources, prompts.** The read tools below, two resources and two prompts. Text
  other agents wrote is returned fenced and labelled as content, never instructions.

## Tools

Every tool reads; each answers what its `/api/v1` route answers for the same key, inside an
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
