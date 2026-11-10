# Offices' Claude sessions on MCP (plan, 10 Nov 2026)

Status: the pilot passed on 10 Nov 2026 (evidence in sections 4 and 7), and the other offices now
move one at a time, each announced first. Jeremy confirmed the plan on 10 Nov 2026 (decision
d_01M71ARX2FGFQ28G9Q06804QGW in `docs/decisions.md`). The rest is the plan as first written, apart
from section 4, steps B and C of section 5, and the ledger at the end of section 7. Where the build
differs, section 4 says so: the helper is `artifacts-key --mcp-headers <seat>`, a mode of the
existing wrapper, not a new `ream-mcp-headers` script.

Direction (decision d_01M6ZWA1YH391DJN6RQVM93B3J, 9 Nov 2026), as I understand it: agents stop
running out-of-date Ream tools in two steps; step 1, the self-updating `ream` tool, is done; step 2
moves each office's Claude session onto Ream's MCP server, while scripts keep the self-updating
CLI. This plan covers step 2.

Sections 1 to 3 were verified by reading (code, docs, homelab config); the live call is the pilot
in section 7.

## 1. What was found

- Ream serves MCP at `https://ream.tenfourty.site/mcp` (homelab `artifacts-config.env`:
  `REAM_MCP_PUBLIC_URL=https://ream.tenfourty.site`, `REAM_MCP_SIGN_IN=on`).
- **`REAM_MCP_ACCEPT_KEYS` is unset there, and with sign-in on that means agent keys are refused
  at `/mcp`** (`docs/mcp.md`: "keys are then refused there unless `REAM_MCP_ACCEPT_KEYS=1`").
  The config comment says the same ("only sign-in tokens, never agent keys"). Today only an
  OAuth-signed app can use `/mcp`.
- An OAuth-signed app acts as **Jeremy's app agent** ("Jeremy, via claude.ai"): its questions are
  stored as asked by the app, answers go to the app's `whats_new`, and it may ask or relay on any
  page he can read. A key acts as **its own seat** (scoped to that agent's pages, answers come to
  that agent). An office needs the second: its pages, its answers, its attribution. So offices
  should use keys over MCP, not OAuth. OAuth stays for Jeremy's own Claude apps.
- Offices today publish through `ARTIFACTS_SEAT=<seat> artifacts-key -- ream ...`
  (`homelab/scripts/artifacts-key.sh`: sops `--extract` of one seat's key into a variable, handed
  over on a descriptor; never on disk, argv or the environment). Seats: `homelab_maintainer`,
  `cove_maintainer`, `warpgate_maintainer`, `artifacts_maintainer`, `qd_maintainer`,
  `ch_maintainer`, `retrobrew_maintainer`. Other seats named in office docs (gg-ops, harland,
  wolff, runcove-pmo, security, jeremys-mac) have no vaulted key listed in `artifacts-key`;
  which hold one is an open question (Q4). Keys are minted and vaulted by the homelab maintainer
  on Jeremy's yes. I read docs and scripts only, never a key or the vault.
- The house skill is `lab-artifacts` (homelab `.claude/skills/lab-artifacts/`, symlinked into
  `~/.claude/skills/`), a house layer on the shared `ream` skill (runcove `skills/ream/SKILL.md`,
  and the service's own `/api/v1/skill.md`). Both teach `ream <verb>` commands and `ream wait`.
- Offices run Claude Code under quarterdeck. quarterdeck's plugin ships its own `.mcp.json`
  (`qd mcp --channel`); the box's `~/.claude.json` has one user-level server (`superlogical`).
  Nothing configures Ream today. Scripts that call `claude -p` pass
  `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (ops-triage, image-updates): they must
  stay free of it.
- MCP server entries in Claude Code accept `type: http`, `url`, `headers`, and `headersHelper`
  (a command Claude Code runs, which prints a JSON object of headers to stdout). That is the only
  hook that lets a key reach the server with no secret in the config file or on a command line.
  Re-check this against the installed Claude Code version before the pilot (step A below).

## 2. What an office's session gets

**Server entry** (name `ream`, so tools are `mcp__ream__publish_page` etc.):

```json
{"mcpServers": {"ream": {
  "type": "http",
  "url": "https://ream.tenfourty.site/mcp",
  "headersHelper": "ream-mcp-headers <seat>"
}}}
```

Placed with `claude mcp add --scope local` (or `--scope user` plus a per-office launch value),
never in a repo's checked-in `.mcp.json`: a checkout is shared by sessions of several offices
(baba's shared-checkout rule), and a seat name there would make one office publish as another.
The entry holds a seat name, which is not a secret.

**Authentication without a secret in a file or on a command line.** A new small helper,
`ream-mcp-headers <seat>` (one script, beside `artifacts-key.sh` in the homelab repo, or as a
mode `artifacts-key --headers`), does what `artifacts-key` does (one `sops -d --extract`, shape
check, no `-d` of the whole vault) and prints
`{"Authorization":"Bearer <key>","X-Ream-Session":"<seat>/<ref>"}` on stdout **to Claude Code
only**. Claude Code consumes stdout; it never enters the transcript. Constraints I would hold it
to:
- The seat is a required argument, no default (the same rule as `artifacts-key`); unknown seat
  exits non-zero with nothing on stdout.
- The key never goes to stderr, a log or a temp file; stderr says only the seat and a count.
- The session label: `X-Ream-Session` from the Claude Code session id when the helper can see
  it, else the seat name alone (`mcp/<date>` is the server's fallback and says nothing).
- A Claude Code that does not run `headersHelper` for http servers (checked in step A) makes
  this plan wrong, and I would stop and say so, not fall back to a key in a header literal.

**Not chosen:** a `headers` value with the key (a secret in a file); `claude mcp add --header
"Authorization: Bearer ..."` (a secret on a command line); OAuth per office (every office would
become Jeremy's app and lose seat attribution and scoping; also a browser consent per office).

**Server side, one prerequisite:** `REAM_MCP_ACCEPT_KEYS=1` in the homelab config
(`k8s/apps/artifacts/artifacts/app/artifacts-config.env`, the homelab maintainer's file; I ask,
I do not edit). Effect: `/mcp` takes agent keys as well as sign-in tokens. `/api/v1` is unchanged.

## 3. Coverage: MCP tools against the `ream` CLI

MCP already covers (tool: CLI equivalent): `whoami`; `list_pages` (`ream publish --list`);
`read_page` incl. version, section, history; `get_template`; `publish_page` (`ream publish`,
with `comments`, folder, `refresh_every`, session); `update_page` whole or by section with
`replace|append|prepend|remove` and revision checks (`--update`, `--replace/--append/--prepend/
--remove`); `organise_page` rename, pin, archive, restore, file, comments on/off (`--pin`,
`--archive`, `--restore`, ...); `delete_page`; `comments`; `comment` incl. reply (`thread_root`),
resolve, reopen (`ream comment`, `reply`, `settle`, `reopen`); `ask` (`ream ask`, with options,
recommended, depends_on, replaces, urgency, audience); `questions` (`ream list`); `close_question`
(`ream close`); `record_answer` (`ream record`); `whats_new` (`ream answers` and the events feed).
Plus the `ream://guide` and `ream://templates/{name}` resources and two prompts.

**Gap list** (the CLI does it, MCP does not):

1. **`ream wait`, the blocking wait.** MCP is stateless and `whats_new` never waits (by design).
   So a session cannot be *woken* by an answer over MCP. This is the one that matters: the rule
   "after you ask, run `ream wait` in the background" has no MCP form. Interim: poll `whats_new`
   at the start of every turn, or keep `ream wait` (CLI) for the waiting only (Q2).
2. `listening` / `listen` / `pickup` / `release` (a session claiming whose answers it hears).
   Only the session label on writes exists; no claim.
3. `ack`, `receipt` (working/done receipts), `outcome`, `reply-note`: no tool. `comment` with
   `thread_root` covers a plain reply but not Explain/Shorten/Fix acknowledgements or receipts, which
   the `lab-artifacts` skill says each owe a reply.
4. `ask-review` (a review's first-verdict question) and `review-release`: no tool. `ask` has no
   review mode or `fields` (forms) on the asking side; `record_answer` takes `fields` only.
5. `threads`, `answers` as separate views: partly `comments` / `whats_new`; no per-page unanswered
   thread list.
6. `delete-comment`, `restore-comment`: none.
7. `publish --duplicate`, `--replace` whole-page by id with a file: `update_page` covers it by
   `body`; duplicate has no tool.
8. **Files.** MCP tools take the page `body` as an argument; a large HTML page therefore passes
   through the model's output, where `ream publish page.html` read a file. Costly and error-prone
   for the 20-50 KB templates; no image uploads on `comment` either. A `body_from` is not offered.
9. `--idempotency-key` on comment/ack/reply: none; a retried tool call can double-post.
10. `connect`, `admin`, `republish`, `beads-page`, `bead-metrics`, `version`: deliberately CLI
    (machine setup, operator and script jobs). Not gaps to close.
11. Sharing, visibility, destroy and keys are never offered over MCP (by design).

Headline: **MCP covers publish, update, comment, ask, record and read; it lacks the blocking
wait, receipts/acks, the review-ask, claims, and file-based publishing.** Gaps 1, 3 and 8 are the
ones to close (or to decide to live with) before the rest of the offices move.

## 4. The pilot, and what changes in the shared skill

**The pilot passed (10 Nov 2026, 00:12 UTC).** This office's own Claude session, signed in to
`https://ream.tenfourty.site/mcp` as the artifacts maintainer's seat with its own agent key, did the
whole round over MCP: `whoami`, `publish_page` (comments off), `update_page`, `read_page` with its
history, and archive (`organise_page`) all worked. Afterwards the key was found in 0 local files and
in 0 lines of the server pod's logs. Not exercised by the pilot, and left for each office's own
move (section 7): a question and its answer through `ask` and `whats_new`, and a refused call
reading as a refusal.

**How an office moves.** Offices now move one at a time, each announced before it is made, with the
order of section 5 (batch 1, then batch 2, off-baba later). Each office sets up its own seat, once,
in its local scope (not a checked-in file, so no office can publish as another):

```bash
claude mcp add-json --scope local ream '{"type":"http","url":"https://ream.tenfourty.site/mcp","headersHelper":"artifacts-key --mcp-headers <seat>"}'
```

`<seat>` is the office's own seat name, for example `artifacts_maintainer` (`artifacts-key --help`
lists them). As built, the helper is a mode of the existing `artifacts-key` wrapper rather than the
separate `ream-mcp-headers` script of section 2: it prints the authentication headers itself, so
the key is on no command line and in no file. The session restarts once to load the tools. Scripts
and timers keep the self-updating `ream` tool (section 6).

Changes to the shared skill (the `ream` skill is served by the service and in runcove;
`lab-artifacts` is the homelab maintainer's):

- `ream` skill (service `skill.md` and runcove `skills/ream/SKILL.md`): not changed yet; to propose.
  Lead with "if the session has `mcp__ream__*` tools, use them"; keep the `ream <verb>` form as the
  path for scripts and for a session with no MCP. State the tool names next to each CLI verb once,
  in a table.
- `lab-artifacts` skill: written, not yet landed. A short section says that in a Claude session the
  `mcp__ream__*` tools come first once the seat's entry is set up (the command above), that a new
  page always says whether comments start on or off, and that the session is passed as
  `<agent>/<session ref>`; scripts keep the CLI. The "Keys and seats" section, the seat table and
  the Telegram and look sections stay as they are. Templates: `get_template` replaces copying
  `templates/<name>.html` where the page is written in the model anyway.
- The rule "run `ream wait` in the background" becomes: wait with the CLI (kept) or poll
  `whats_new` each turn (Q2); the rule's reason (an unread answer is a question never asked) is
  unchanged.
- Every doc change lands with its code change in the same PR (house rule: docs are part of done).

## 5. Rollout

Order: prerequisites, one pilot, then the rest in batches. I propose the **artifacts maintainer
(this office) as pilot**: it owns the server, can read both sides, and its misattribution risk
is contained. Alternative in Q3.

A. **Prerequisites (before any office moves).**
   1. Check the installed Claude Code supports `headersHelper` on `type: http` servers and what
      it does on a non-zero exit (a quick, key-free test against a dummy server).
   2. Ask the homelab maintainer: `REAM_MCP_ACCEPT_KEYS=1` (config), the helper script, and the
      `ream-mcp-headers` seat list. Verify after the pin PR lands, not before.
   3. Decide gaps 1, 3, 8 (Q1, Q2).
B. **Pilot (artifacts maintainer).** Done: passed on 10 Nov 2026 (section 4, ledger in section 7).
   The entry stays in its own scope, with `ream` kept for waiting.
C. **Batch 1:** the other seats that already have a vaulted key and use pages often
   (`homelab_maintainer`, `cove_maintainer`, `warpgate_maintainer`), one at a time, each announced
   first and done by that office's own session or by the homelab maintainer; I send each a short
   how-to message (the setup command in section 4).
D. **Batch 2:** `qd_maintainer`, `ch_maintainer`, `retrobrew_maintainer`, then offices with no
   key (Q4).
E. **Off-baba** (jeremys-mac, jbrown-dev): later; `ream connect` keys live in
   `seat-secret` there and the helper would read from it, not sops. Out of scope until B-D hold.

Rollback per office is removing the entry (`claude mcp remove ream`); the CLI path never goes
away, so a failed move costs nothing. Each office's doc (runcove `docs/offices/<office>.md`) gets
one line when it moves, by PR to runcove, landed by the homelab maintainer.

## 6. Scripts keep the CLI

Scripts (timers, CI, `ops-triage`, `image-updates`, the republish and beads-page jobs, anything
under `artifacts-key -- ream ...`) keep `ream <verb>` with the self-updating tool: no change.
They give it a seat through `artifacts-key` as now. Scripts that run `claude -p` keep
`--strict-mcp-config` with an empty map, so no MCP server (this one included) appears in a
script's session. The line the plan draws: a session with a model uses MCP; a program uses the
CLI. A session may still run the CLI for gaps 1, 3, 8 until they close.

## 7. How each office is verified

For each office after it moves (it does these in its own session; I check the result in the
gallery, never a key):
1. `claude mcp list` shows `ream` connected (a tool count above zero).
2. `whoami` names the **office's own seat agent**, not another and not the app. This is the one
   check that catches a wrong seat in the entry.
3. `publish_page` a small throwaway page with `comments: off`, then `read_page` it, then
   `delete_page`; the gallery shows the page under the seat with the session label
   `<seat>/<ref>` (not `mcp/<date>`).
4. `ask` a one-line question on it; Jeremy or I answer; `whats_new` returns the answer to that
   seat. (For waiting, whichever of Q2 is chosen.)
5. A refusal reads as a refusal: a call with a wrong seat or revoked key gives a 401/403 error,
   never an empty list.
6. No key in the transcript, process args or any file: `ps` and the config show only a seat name;
   the helper's stderr shows no value.

Pilot-only additional check: the same flow against the SQLite and Postgres test suites is already
CI's; the live check is the one in the gallery.

**Pilot ledger (this office, 10 Nov 2026, 00:12 UTC).**
- Check 2 (`whoami`) and the working part of check 3: passed. The session ran `whoami`, published
  a page with comments off, updated it, read its history and archived it, all over MCP. The pilot
  archived the page rather than deleting it.
- Check 6 (no key anywhere): passed. The key was found in 0 local files and 0 server pod log lines.
- Check 1: passed (00:20 UTC): `claude mcp list` shows `ream` connected.
- Check 5, in part: passed (00:20 UTC): a made-up bearer key on `POST /mcp` answers 401. A
  revoked key and a wrong seat are checked on each office's own move.
- Check 4 (an answer reaching the seat through `whats_new`): not exercised, because it needs
  Jeremy to answer a throwaway question. Each office does it on its own move, announced first.

## 8. Risks

- A helper that prints a key is a new place a key passes. It goes to one parent process over a
  pipe, as `artifacts-key` hands it over a descriptor today; if `headersHelper` is also run by
  Claude Code at connection time with the output logged under `--debug`, a debug log holds the
  key: check in step A, and if so, vault and redact that log per the leak policy.
- A wrong seat in the entry publishes as another office (`whoami` check, §7.2).
- Statelessness: no push, no notifications. Nothing wakes a session; Q2.
- Token cost: page bodies pass through model output (gap 8).

## 9. Open questions for Jeremy (recommendation first)

**Q1. Should keys be accepted at `/mcp` so offices can use it?** (Needed; sign-in alone makes
every office "Jeremy's app".)
- (a) Yes: set `REAM_MCP_ACCEPT_KEYS=1`; offices use their seat keys; OAuth stays for your
  own Claude apps. Recommended.
- (b) No: offices sign in with OAuth as an app each; they lose per-office attribution and
  scoping, and each needs a browser consent from you.
- (c) Wait until a separate per-office token (not the long-lived key) exists.

**Q2. How does an office wait for an answer once on MCP?**
- (a) Keep `ream wait` (CLI) in the background for waiting only; use MCP for everything else.
  Works today, no server change. Recommended.
- (b) Poll `whats_new` at the start of each turn, no CLI at all; an answer is only seen when the
  session next acts.
- (c) Build a wake-up path first (quarterdeck or a long-poll tool) and delay the rollout.

**Q3. Which office pilots?**
- (a) artifacts maintainer (this office). Recommended.
- (b) homelab maintainer: heaviest publisher, most real-world signal, but its mistakes cost most.
- (c) a quieter office, e.g. retrobrew maintainer.

**Q4. Offices with no vaulted seat key (gg-ops, harland, wolff, runcove-pmo, security)?**
- (a) Mint keys only as each office asks to move; the homelab maintainer does it on your yes.
  Recommended.
- (b) Mint all now so the rollout does not stall.
- (c) Those offices stay on the CLI for good.

**Q5. File-based publishing (gap 8): close it?**
- (a) Live with it for the pilot; if page bodies cost too much, add a "publish from a path" tool
  later. Recommended.
- (b) Keep `ream publish page.html` (CLI) for pages and MCP for the rest, as a stated rule.
- (c) Build the tool before the pilot.
