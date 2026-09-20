# Artifacts of our own — a publisher any agent can use (design)

**Status:** v3, 19 Sep 2026. **Design approved by Jeremy 19 Sep** ("LGTM"); revised for the
operator session's review (nine findings) and then for the adversarial security review, which
returned **"not as written"** with six blocking findings — all six accepted and folded in (triage
in §11). Jeremy's post-approval decisions are also in: **no model access in v1** (D6), **ggshield
scans page bodies** (D12), CNPG rather than self-hosted Supabase (D13), PocketBase as the later
route to real mini-app backends (§9).

**Outcome in one line:** any agent Jeremy runs — Claude on any account, Codex, Gemini, a cron
script — publishes a page with one call and gets back a URL that keeps working, that only he can
open, and whose pages can show live lab data.

**Fact base:** `~/.local/state/homelab-ops/evidence/2026-09-18/agent-artifacts/`
(`00-claude-artifacts-surface.md` — Claude's control plane and runtime contract 0.2.52 with its
type definitions; `01-prior-art.md` — prior art, threat model, sandboxing, header rule set;
`02-lab-fit.md` — front door, Authelia, cluster fit, live data; `03-origin-isolation.md` —
wildcards, zones, cookie scope, ingress; `04-security-review.md` — the adversarial review).
Durable facts move to `docs/services/artifacts/` once it exists. Tracked as bead `homelab-ij8`.

## 1. What Jeremy asked, and what this design does about each

| Ask (Jeremy, 18–19 Sep) | What is true | This design |
|---|---|---|
| "artefacts are tied to a claude account… host artefacts via a new app in my homelab" | `cswap` rotates accounts to keep them in tokens, so published pages appear and disappear — two sets, same titles, different ids, within one hour on 18 Sep | The URL and the page's data belong to the lab: our Postgres, our backups, our domain. Nothing about publishing touches a claude.ai login (D3) |
| "any of my agents… or even possibly agents that are not claude agents" | Claude's publish path is a first-party tool; nothing else can call it | A REST API with per-agent keys is the contract; a CLI and an MCP server are thin clients over it (D3, §4.3) |
| "publicly exposed via our new warpgate setup" | The front door serves only its own sign-in page today, and Warpgate **refuses API tokens on proxied targets** (our fork, `warpgate-protocol-http/src/catchall.rs`, the `decide_public_target_access` match) — so agents could never publish through it | Two doors (D1): people through the Cloudflare tunnel + Authelia today, moving behind the front door when it carries apps; agents through `/api/v1`, which skips Authelia and checks our own key |
| "authed via authelia and tied to only my jeremy user" | Authelia's session cookie is scoped `domain: tenfourty.site`, the whole registrable domain. **And there is no `subject:` rule anywhere in the lab's access control**, so every hostname described as "Jeremy only" is in fact open to any two-factor LDAP user (security review, verified) | Gallery stays on `tenfourty.site` where that cookie already belongs (D2), gated by an **explicit `subject: 'user:jeremy'` rule** we add. Page content lives on a different registrable domain, so the cookie never reaches agent-written JavaScript |
| "cool stuff… showing live data or beads state, lots of nice little mini-apps that are not full apps" | A claude.ai page cannot reach the lab at all | Named read-only feeds, scanned before they leave the feed service, granted per artifact *and per version* (D4, D11, §4.5) |
| "make sure everything is fully tested and robust and securely reviewed" | The known attacks on Claude's own artifacts are documented and reproducible | Tests first, a browser-driven escape suite of twelve cases, every body scanned, CI gates, and this review repeated before exposure (§6) |
| "default always private… make some artifacts public if we want… share via email with a magic link" | — | Three visibility levels (D8), changed **only by Jeremy in the gallery**, never by an agent key (D14) |

## 2. Goal, non-goals, success criteria

**Goal.** A small in-cluster service that accepts an HTML page (plus supporting files) from any
authenticated agent, serves it at a stable per-artifact URL only Jeremy can open, gives the page a
pinned subset of Claude's own runtime API plus read-only lab feeds, and keeps every version, all
page state and all assets inside the lab's storage and backups.

**Non-goals (v1).** Asking a model from a page (D6); multiple human accounts; realtime co-presence
(Claude's `room`); comment threads; the `mcp` capability; artifact types and design systems; a
public gallery; replacing Grafana or Homepage.

**Success criteria** — each is a measured check, not a judgement:

- **S1 — the decisions page stops moving.** `scripts/ops-decisions-page.sh` publishes here; Jeremy
  answers on it; the operator reads the answers back over the API and closes the beads. The URL is
  unchanged after a republish, and after an account rotation.
- **S2 — an agent with no Claude login publishes.** A shell script holding only an API key creates
  a page, patches it, reads it back and deletes it — and **cannot** touch an artifact another key
  created.
- **S3 — a page shows live lab state.** A page renders current beads issues from a feed and
  refreshes without being republished.
- **S4 — the escape suite passes**, all twelve cases in §6.2.
- **S5 — it survives a restore.** Metadata, page state and bodies restore from the off-site backup
  into an empty namespace, and a restored page serves.

## 3. Decisions

- **D1 — two doors.** People: Cloudflare tunnel → ingress → Authelia ForwardAuth. Agents:
  `/api/v1` on the gallery host, bypassed in Authelia's access-control rules, authenticated by our
  own key. The viewing hostname moves behind the Warpgate front door when it carries apps.
- **D2 *(revised by security review B1)* — the gallery lives on `tenfourty.site`; only page
  content lives on `tenfourty.com`.** Jeremy chose reuse over buying a name, and that still holds.
  The v2 plan put the gallery on `tenfourty.com` with a host-pinned cookie; **that is not
  achievable**: the Authelia chart builds its portal from `subdomain` + `domain` and always sets a
  `Domain` attribute, so on `tenfourty.com` it would be either `Domain=tenfourty.com` (reaching
  every page — the exact bug this design exists to avoid, moved one domain sideways) or a
  two-label portal name outside the free certificate D2 relied on.
  **Layout:** gallery `artifacts.tenfourty.site` (covered by the existing `*.tenfourty.site`
  Authelia rule and certificate); page content `<id>.tenfourty.com`, one label deep so
  Cloudflare's free certificate covers it; private pages carry a **capability token as a path
  segment** with `Referrer-Policy: no-referrer`, never a cookie. Nothing on `tenfourty.com` is ever
  Authelia-gated, so no session cookie exists on that domain to steal, plant or fix.
  `links.tenfourty.com` still shares the domain with page content, so its cookie-fixation exposure
  is in the escape suite (§6.2 case 8).
- **D3 — the API is the contract.** REST first; the CLI, the MCP server and the Claude Code skill
  are clients of it.
- **D4 — live data by named read-only feeds.** The host fetches with its own read-only
  credentials; a page calls `/feeds/<name>` on its own origin; a new feed is a reviewed commit.
- **D5 — the page API copies Claude's.** A pinned subset of contract 0.2.52 (`db`, `user`,
  `downloads`, `assets`, `artifact`). Unimplemented names resolve `null`, which that contract is
  designed for.
- **D6 — no model access in v1 (Jeremy, option (c)).** `sample` is dropped: it would have run on a
  subscription token, and **cswap rotates the account that token belongs to**. In v1 no page can
  reach any model, so a prompt-injection chain ends at the page and its feeds. Returns as a
  follow-up (§9) once the account question is settled.
- **D7 — withdrawn**, superseded by D6.
- **D8 — three visibility levels.** Private (default) · link-shared (expiring, revocable
  capability token) · public. Email magic-links later reuse the link-share path.
- **D9 — build our own, take everything we can from `artifact-server`** (Jeremy's words). Its
  design is the reference; its code is not adopted (AGPL, 3½ weeks old with its own open
  pre-publication gates, needs Postgres **and** S3 which the lab lacks, content hosts that serve
  only plain reads, and a bootstrap-cookie delivery Safari is likely to block on his phone).
- **D10 — a third ingress controller** for page content, separate from the one serving gated apps.
  Defence in depth only: cookies attach in the browser before any request reaches an ingress.
- **D11 *(revised by B4)* — one origin per artifact, versions by path, old versions inert, and
  grants bound to a version.** Per-*version* origins would change the URL on every publish, which
  defeats S1, so versions are paths under the artifact's own origin, and **only the current version
  gets the runtime shim and the feeds** (on a version path every `claude.use` resolves `null`,
  `/feeds` refuses, CSP is `connect-src 'none'`). That alone does not stop the attack that matters,
  which is driven by the *current* version: an agent publishes a benign page, Jeremy approves its
  feed grants, and `artifact.publish()` then swaps the body while keeping them — Bloom's
  bait-and-switch. So **a grant is bound to a version hash**; a republish that adds or widens a
  grant produces a **pending** version that serves the old body until Jeremy approves it. A page
  publishing itself with an unchanged grant set goes live as normal.
- **D12 — every body is secret-scanned before it goes live; ggshield is the scanner (Jeremy).**
  Pluto found Claude does no content review and leaked environment variables in 10 of 85 runs.
  ggshield sends content to GitGuardian's API, so page bodies leave the homelab: **Jeremy accepted
  that explicitly**, recorded as a deliberate exception to "lab data stays in the lab". The gate
  cannot fail open — the repo's local patterns (`scripts/log-secret-shapes.txt`) run first and
  always, ggshield is the authoritative pass, and a publish is **refused** if GitGuardian is
  unreachable. Feed responses and state read-backs are scanned by the **local scanner only** (D15):
  they are lab data on a hot path and must never be shipped anywhere.
  **ggshield must be handed the ORIGINAL submitted body, exactly as the agent sent it** — never the
  local scanner's detagged, entity-unescaped or otherwise normalised view. Feeding it a processed
  view makes it inherit our transformations and therefore our blind spots, and the second opinion
  stops being independent, which is the only property that justifies shipping page bodies out of the
  lab at all. This is not hypothetical: on 19 Sep 2026 the local scanner produced two security
  regressions in one night, both inside the transformation layer and both invisible to their own
  green tests — a placeholder rule that let 21 credential shapes through, and a crash fix that
  blinded it to zero-padded character references (24 lost detections across 8 credential kinds, a
  real AWS key published and recoverable from the stored body). Both were caught by review, neither
  by a test, and **both would have survived a ggshield wired to the processed view.**
- **D13 — CNPG, not a self-hosted Supabase.** Supabase's headline is PostgREST and Realtime
  exposing tables straight to the browser under row-level security; here that would route page
  traffic *around* the content server, which is where every guard lives. It also adds ~8
  containers, a community chart, its own bundled Postgres outside CNPG, and GoTrue as a second
  identity system beside Authelia. PocketBase is the follow-up if real per-app backends are ever
  wanted (§9).
- **D14 *(from B3)* — agent keys are scoped and cannot widen exposure.** A key reaches only the
  artifacts its own agent created. Changing visibility and minting a share token are **human
  actions in the gallery session**; no agent key can do either. A key presented in a query
  parameter is rejected outright.
- **D15 *(from B6)* — the transcript path is scanned too.** Feed responses are scanned by the local
  scanner before they leave the feed service, and page state is scanned before it returns to an
  agent over `GET /state/*`. A hit **refuses the read loudly, naming the artifact** — never returns
  redacted content, because a silent redaction teaches nobody. This closes `feed → db → agent
  transcript`, which is how injected beads free text would otherwise walk into a transcript.

## 4. Architecture

### 4.1 Addresses

| Address | What it is | Auth |
|---|---|---|
| `artifacts.tenfourty.site` | The gallery: list, open, versions and diffs, agent keys, feed grants, visibility, pending-version approvals | Authelia ForwardAuth **with an explicit `subject: 'user:jeremy'` rule** |
| `artifacts.tenfourty.site/api/v1/*` | The agents' door | Authelia `bypass` on the path; `Authorization: Bearer <scoped agent key>`; header identity (`Remote-User`) from a non-Authelia caller is never trusted |
| `<id>.tenfourty.com` | One origin per artifact: the current page, its files, `/feeds/*`, `/_blob/*`. Version paths serve inert HTML | Private: capability token in the path, `Referrer-Policy: no-referrer`. Never Authelia, never a cookie |

Nothing on `tenfourty.com` is Authelia-gated — CI-enforced (§6.3). `links.tenfourty.com` keeps
working, and its exposure to cookie planting from a sibling page is a standing escape-suite case.

### 4.2 Components

- **gallery** — the only service with write routes: REST API, the human UI, scoped key management,
  version store, the publish gate, feed grants and pending-version approvals. **It renders nothing
  an agent authored as markup**: titles, descriptions and diffs are escaped text, favicons are
  emoji only, and `_blob` bytes are served exclusively from `<id>.tenfourty.com` (B2).
- **content server** — serves a version's body and files for `<id>.tenfourty.com`, injects the
  runtime shim on the current version only, enforces `Sec-Fetch-Site` on `/feeds`, sets the header
  set in §5, and **serves a page only for `Sec-Fetch-Dest: iframe` from the gallery** — a top-level
  request is redirected to the gallery, which frames it (B5). No write routes.
- **thumbnailer** — a browser pod that loads the *public* content URL with no service-account token
  and a deny-all egress policy, so a page cannot use it as a way into the cluster (B2).
- **feeds** — a fetcher per named source, cached with an explicit TTL, holding the only credentials
  that touch lab services, scanning every response before it leaves (D15). Read-only by
  construction: each feed is a fixed query, not a proxy.
- **Postgres (CNPG)** — artifacts, versions, keys, grants, page state, answers.
- **PVC** — bodies, supporting files, uploaded assets, thumbnails.

### 4.3 What an agent can do (`/api/v1`)

`POST /artifacts` · `PUT|PATCH /artifacts/{id}` (patch carries an expected revision, so an agent
sends an edit, not 200 KB) · `GET /artifacts/{id}` · `GET /artifacts` · `DELETE /artifacts/{id}` ·
`POST /artifacts/{id}/assets` · `GET|PUT /artifacts/{id}/state/*` · `GET
/artifacts/{id}/versions/{n}` and a diff · `POST /feeds/{name}/push` (authenticated inbound, for
the two feeds that originate on baba — §4.5).

Every route is scoped to the calling key's own artifacts (D14). Visibility changes and share tokens
are **not** in this API. Every response carries the artifact's URL; every write records which key,
which agent, which session, surfaced as a colophon.

**The state API keeps the decisions page's existing shape:** one document per bead id, fields
`choice` / `note` / `answeredAt`, so `scripts/ops-decisions-page.sh` needs only a `--publisher`
flag and the operator keeps reading answers by bead id. Migration is checked by bead id against
`bd`, never by remembered wording.

### 4.4 What a page can do

`window.claude.use(name)` resolving the pinned subset of contract 0.2.52 — `db`, `user`,
`downloads`, `assets`, `artifact` — and `fetch('/feeds/<name>')`, which needs no Claude-specific
code, so a Codex- or cron-written page works the same. Everything else, `sample` included, resolves
`null`. Chat-era `window.storage` gets the same shim. On a version path, every name resolves
`null`.

`artifact.publish()` from page JavaScript produces a live version when the grant set is unchanged,
and a **pending** version when it widens (D11). Service workers are blocked (`worker-src 'none'`),
and the first load after a version change carries
`Clear-Site-Data: "storage","cache","executionContexts"`, so nothing a previous version registered
outlives it (B4).

### 4.5 Feeds on day one

| Feed | Source | Path | Note |
|---|---|---|---|
| `beads` (homelab) | `bd list --json` on baba | **push**: a timer on baba posts to `/feeds/beads/push` | The homelab tracker is an embedded Dolt database inside the repo on baba (`.beads/embeddeddolt`, verified) — not on the in-cluster server, so no in-cluster feed can read it |
| `ops-status` | `ops-board.sh --json`, `ops-status.sh --json` on baba | **push**, same endpoint pattern | Snapshot with a timestamp, not live |
| `metrics` | VictoriaMetrics in-cluster | pull | Allow-listed queries only |
| `uptime` | Uptime Kuma in-cluster | pull | Status summary only |
| `beads` (runcove, optional, later) | `beads-dolt`, namespace `ci` | pull | Needs a `beads_reader` user with `SELECT` on `runcove.*` (the operator adds it on request) and an explicit CiliumNetworkPolicy rule for our pod — selector `app.kubernetes.io/name: beads-dolt`. **Do not label our pod into the beads group**; ask for a rule |

Rollout order for grants: `ops-status` and `uptime` first; **the beads feed is held until a real
browser has answered the top-level-navigation question** (§10), because beads free text is the most
likely carrier of injected content.

### 4.6 Storage and backups

CNPG on `linstor-thin` for metadata and page state; a PVC for bodies and assets. Neither is backed
up automatically — VolSync is opt-in per app — so both get a `ReplicationSource` in the same commit
that creates them, and S5 proves a restore.

### 4.7 Surviving a rotation, which is the whole point

Three times in two days a Claude artifact became unreachable after an account rotation, and each
time the recoverable part was the HTML — which lives on disk anyway — while the unrecoverable part
was the answers Jeremy had typed into the page, which existed only in the artifact's own store.
So: **answers live in our `state` table, never in a store we do not own.** A page reaches them over
`GET|PUT /_state/{path}` on our origin, and the shim maps `claude.use('db')` onto those routes, so
a page written against Claude's db capability stores its answers in our Postgres without knowing it.

The same failure has a subtler shape here, and the defences against it are easy to mistake for
untidiness, so they are written down:

- **A listing is not a reliable negative.** Every agent route is scoped to the calling key's agent,
  so an agent's listing is per-seat exactly as Claude's is per-account. An empty listing means
  "nothing under this seat", never "nothing exists" — and an agent that reads it as the latter
  republishes a duplicate, which is precisely the failure being replaced.
- **`artifact.agent` has no foreign key to `agent_key`, deliberately.** A key can be revoked and
  reissued while its artifacts remain, so re-minting a key for the same seat keeps the whole
  history. Do not "fix" the missing constraint.
- **The seat name is the thing that must not vary.** Mint the replacement key for `ops-seat`, not
  `ops-seat-2`; a new name is a new world with an empty listing.
- **The gallery lists every artifact regardless of seat**, so there is always one view that cannot
  lie by omission. Its query carries no `where agent = …` and must not grow one.

## 5. Security

Two facts carry the design. **A browser attaches a cookie by domain match, before any request
reaches any server** — so agent-written pages cannot live under `tenfourty.site`, where Authelia's
session cookie is scoped. And **an iframe sandbox stops existing the moment the page is opened
top-level** — which is exactly what happens when Jeremy taps a link on his phone, so containment
cannot rest on it.

Rules, each a server-enforced control rather than an instruction to an agent:

1. **Page content only on `tenfourty.com`; nothing Authelia-gated there** — CI-enforced.
2. **The gallery renders no agent-authored markup and serves no agent-authored bytes** (B2).
3. **One origin per artifact; old versions inert; grants bound to a version hash** (D11).
4. **Pages are served framed only.** `Sec-Fetch-Dest: iframe`, else redirect to the gallery, which
   frames it. **Correction found while planning (v3.1):** the security review's wording was
   "iframe **plus same-site**", which was right while the gallery lived on `tenfourty.com`; after
   B1 moved it to `tenfourty.site` the gallery's own frame request is **cross-site**, so a
   same-site condition would refuse the only legitimate caller. The site condition is therefore
   dropped and its job is done by two other controls that do not depend on it: `frame-ancestors`
   naming the gallery alone (the browser refuses to frame the page anywhere else) and the path
   capability token (a direct request without it gets nothing). `/feeds` keeps its stricter
   `Sec-Fetch-Site: same-origin` requirement, which is unaffected — that is a page calling its own
   origin. The gallery registers no `postMessage` listener reachable by page content.
5. **The full header set from `01-prior-art.md` Rule 2, verbatim** — `default-src 'none'`,
   `connect-src 'self'` (current version) or `'none'` (version paths), `form-action 'none'`,
   `base-uri 'none'`, `worker-src 'none'`, `webrtc 'block'`, COOP/CORP, `X-DNS-Prefetch-Control:
   off`, `frame-ancestors` the gallery only. Libraries are vendored, so `script-src` never needs a
   CDN.
6. **`Sec-Fetch-Site: same-origin` required on `/feeds`** — a missing header (curl) or `none` (a
   typed URL) fails too; sibling artifact origins are same-site, so this is what separates them.
7. **Private pages use a path capability token** with `Referrer-Policy: no-referrer`, never a
   cookie (D2).
8. **Agent keys are scoped; visibility and sharing are human-only** (D14).
9. **Everything is scanned**: bodies before publish, feed responses before they leave, state before
   it reaches an agent (D12, D15).

Known attacks this must survive: Claude Desktop's bait-and-switch RCE and shared-origin XSS through
the `window.cowork` bridge (Bloom); the Android shared-origin cross-account read (Zero-Lab);
env-var leakage through unreviewed bodies (Pluto). The 15-row table in `01-prior-art.md` and the
six findings in `04-security-review.md` become test cases in the implementation plan.

## 6. Testing, robustness and review

### 6.1 Tests first
The publish path, key scoping, revision-guarded patches, visibility rules, the inert-version rule,
pending versions and the feed guards are written test-first, against fixtures.

### 6.2 The escape suite (browser-driven, in CI) — twelve cases, each a *must fail*
1. A page opened top-level reaching an outside address via `location =`, `window.open`,
   `<meta refresh>` or a form POST carrying feed data — the content server must have refused to
   serve it top-level at all.
2. A title, description or version diff containing `"><img src=x onerror=…>` executing when the
   gallery renders the list, the detail page or a diff.
3. A page loaded by the thumbnailer reaching VictoriaMetrics, the beads source, or any cluster
   address.
4. A request to `/api/v1/*` carrying `Remote-User: jeremy` being treated as Jeremy.
5. Agent key B listing, reading the body or state of, or minting a share token for, an artifact
   created by key A.
6. `/feeds/*` succeeding with no `Sec-Fetch-Site` header (curl) or with `Sec-Fetch-Site: none`.
7. A beads fixture carrying a known test credential coming back out through `GET /state/*`, or
   reaching a page through `/feeds/beads`.
8. A page planting a `Domain=tenfourty.com` cookie that the gallery or LinkStack honours, including
   the cookie-bomb variant that persists across republish and delete.
9. A framed page navigating itself out with data attached.
10. Reading another artifact's `db`.
11. A service worker registered by the current version surviving a republish.
12. Publishing a body containing a known test credential.

### 6.3 CI gates
No Authelia-gated Ingress on `tenfourty.com` · the gallery's Authelia rule carries the `subject`
rule · no agent-authored value rendered as markup in a gallery template · the §5 header set present
on every content route · the runtime shim absent from version paths · the thumbnailer's egress
policy deny-all and its service-account token disabled · every `/api/v1` route covered by a
key-scope test.

### 6.4 Review
This adversarial review runs again on the implementation, not just the design; the operator session
reviews the implementation plan and that review before anything is exposed; `intent-review` at the
end against what Jeremy actually asked.

### 6.5 How this arc runs
Jeremy started it himself, so it sits outside the operator board's WIP limit, as the front-door arc
does. It displaces nothing there and borrows no workers from that seat. Progress is reported to the
operator at each build-order milestone.

## 7. Build order (each proven before the next)

1. **It publishes and it comes back** — API, scoped keys, storage, one origin per artifact, the
   gallery list. Proves S2.
2. **The decisions page moves** — the runtime shim, page state in the existing answer shape, the
   read-back path with its scanner, then `scripts/ops-decisions-page.sh --publisher`. Proves S1.
3. **Pages show the lab** — feeds, version-bound grants, the pushed `ops-status` feed first, beads
   held per §4.5. Proves S3.
4. **It is safe to leave running** — the twelve-case escape suite, CI gates, the publish gate, the
   review repeat. Proves S4.
5. **It survives losing the volume** — VolSync, restore drill. Proves S5.

## 8. What this costs, honestly

No new money: the domain is owned, the certificate is free at one label, and the cluster has room —
the three Talos nodes sit at 59% / 45% / 62% memory (≈15 GiB each in use, measured 19 Sep), so a
CNPG instance, a thumbnailer and a third ingress-nginx fit without crowding the operator's board.
The real costs are a new public surface on a second domain; a service holding read-only credentials
for four lab sources; page bodies leaving the lab to be scanned by ggshield (D12, accepted); and
the maintenance of a runtime shim tracking a pre-1.0 contract we do not control — pinned at 0.2.52,
so it stalls rather than breaks if Anthropic moves.

## 9. Deliberately not in this design

`sample` (D6) — returns once the account question is settled; the verified CLI facts for that day
are in `04-security-review.md` (F1), including that `--max-turns` does not exist on 2.1.277 and
that unknown flags are accepted silently, so any cap must be asserted by behaviour. A
**PocketBase-backed tier** for real mini-app backends (Jeremy: "a future edition to allow more
mini-app like functionality"). Comment threads dispatched to the agent that wrote the page. The MCP
Apps protocol as a second runtime. Scheduled refresh and webhooks-in. Email magic links.

## 10. To verify at build time (facts that can go stale)

- **Top-level navigation:** whether the framed-only rule plus the header set actually stops a page
  navigating itself out with data — a real browser answers this, and **the beads feed is held until
  it does**.
- The cert-manager Cloudflare token's zone scope (recorded 12 Sep 2026 as all 38 zones, **not
  re-measured**) still covers `tenfourty.com`, and a `*.tenfourty.com` wildcard issues.
- Safari on Jeremy's phone: the gallery framing a page on the other domain, with the path
  capability token, works.
- **Closed 19 Sep:** ingress-nginx is v1.15.1 (measured), so wildcard hosts are supported; the
  `beads-dolt` policy selector and the absence of a read-only user are known (§4.5); the Authelia
  chart cannot host-pin its cookie (B1).

## 11. Review triage

**Operator session — REVISE, nine findings, all accepted:** `__Host-` cookies and LinkStack
collateral (superseded by B1, LinkStack case retained); ggshield escalated to Jeremy, who accepted
it (D12); old versions inert (D11); the beads source corrected and verified (§4.5); the rotating
`setup-token` (superseded by D6); the decisions page's answer shape (§4.3); known answers folded
in (§10); wording and memory figures (§4.2, §8); WIP stance (§6.5).

**Adversarial security review — "not as written", six blocking findings, all accepted:**
- **B1** — the Authelia chart always sets a cookie `Domain`, so a host-pinned gallery cookie on
  `tenfourty.com` is impossible → gallery moved to `artifacts.tenfourty.site`, page content alone
  on `tenfourty.com`, path capability tokens (D2).
- **B2** — the gallery rendering agent-written bytes on the origin that holds the session and the
  key admin, and the thumbnailer as an SSRF into the cluster → escaped text only, emoji favicons,
  blobs only from the content domain, thumbnailer with no token and deny-all egress (§4.2).
- **B3** — unscoped agent keys and agent-callable sharing → keys scoped to their own artifacts,
  visibility and share tokens human-only, keys rejected in query parameters (D14).
- **B4** — `artifact.publish()` inheriting approved grants (bait-and-switch) and service workers
  outliving a republish → grants bound to a version hash with pending versions, `worker-src 'none'`,
  `Clear-Site-Data` on version change (D11, §4.4).
- **B5** — an iframe sandbox that ceases to exist when the URL is opened top-level, and twelve
  channels leaking past `connect-src` → framed-only serving, the full header set verbatim,
  vendored libraries, no reachable `postMessage` listener (§5.4, §5.5).
- **B6** — feed content and page state never scanned, while `GET /state/*` is an agent's read-back
  path → local scanner on both, refusing loudly by artifact name (D15).

Also corrected by that review: "Jeremy only" was never true anywhere in the lab — no `subject:`
rule exists, so gating today is any two-factor LDAP user. The gallery adds one, and the finding
went to the operator session as a lab-wide issue.
