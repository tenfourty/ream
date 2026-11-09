# Offices' Claude sessions on MCP (plan, 10 Nov 2026)

Status: a plan for Jeremy to react to. Nothing here is built, committed or changed in another
office's files. Direction (decision d_01M6ZWA1YH391DJN6RQVM93B3J, 9 Nov 2026), as I understand it:
agents stop running out-of-date Ream tools in two steps; step 1, the self-updating `ream` tool, is
done; step 2 moves each office's Claude session onto Ream's MCP server, while scripts keep the
self-updating CLI. This plan covers step 2.

Verified by reading this session (code, docs, homelab config); not yet verified by a live call.

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

## 4. What changes in the shared skill

Not edited by me now; these are the changes to propose (the `ream` skill is served by the service
and in runcove; `lab-artifacts` is the homelab maintainer's):

- `ream` skill (service `skill.md` and runcove `skills/ream/SKILL.md`): lead with "if the session
  has `mcp__ream__*` tools, use them"; keep the `ream <verb>` form as the path for scripts and
  for a session with no MCP. State the tool names next to each CLI verb once, in a table.
- `lab-artifacts` skill: the "Keys and seats" section gains the MCP entry and the helper (§2);
  seat table unchanged; the session label via `X-Ream-Session`; the Telegram and look sections
  stay. Templates: `get_template` replaces copying `templates/<name>.html` where the page is
  written in the model anyway.
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
B. **Pilot (artifacts maintainer).** Add the entry in its own scope; run the verification (§7)
   for a day of real work, alongside `ream` for waiting; fix what breaks.
C. **Batch 1:** the other seats that already have a vaulted key and use pages often
   (`homelab_maintainer`, `cove_maintainer`, `warpgate_maintainer`), one at a time, each by that
   office's own session or by the homelab maintainer; I send each a short how-to message.
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
