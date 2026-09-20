---
name: lab-artifacts
description: Use when publishing a page (status board, report, dashboard, write-up) for Jeremy or another agent to read in the homelab's own artifacts service — minting a key, publishing, updating, and the shared look every homelab page uses.
---

# Homelab artifacts

Self-hosted pages, in the house. An agent publishes a self-contained HTML
file and gets back a stable address; Jeremy reads it in a gallery behind his
own login at <https://artifacts.tenfourty.site/>. Nothing leaves the LAN, no
claude.ai account is involved, and any agent with a key can publish —
Claude or not.

## Publish

```bash
artifacts-publish page.html --title "Its title"        # new page
artifacts-publish page.html --update <id>              # same address, new version
artifacts-publish page.html --update <id> --title "Its real name"   # and rename it
artifacts-publish --list                               # what this key has published
```

`artifacts-publish` comes from `uv tool install ~/repos/artifacts`. It reads
its key from `~/.config/artifacts/key` (mode 600, it refuses anything
looser); `$ARTIFACTS_KEY_FILE` or `--key-file` points it elsewhere.

### Never type a date on a page

Write this where the stamp belongs, and the service fills it in when the page is
served:

```html
<span class="chip">Updated <!--artifacts:updated--></span>
```

It becomes the time THIS version was published, in the reader's zone --
"20 September 2026, 11:04 CEST". You never hold a clock, so you can never be
wrong about it, which matters more than it sounds: the reliable way to get a
date wrong is to write one from the flow of the conversation rather than from
`date`. Jeremy caught a board reading 08:53 UTC at 10:53 on his own wall, and
that was the *better* failure -- a stamp that is merely in the wrong zone is at
least the right instant.

Details worth knowing: the marker is substituted when the page is SERVED, not
when it is stored, so the stored body keeps the comment and an identical
republish is still identical. An older version at `/v/N` shows when *it* was
published, not when the live page was. And `artifacts-publish --list` now
reports `updated_at` per page, which is the same fact for a reader who is not a
browser.

**Update rather than republish.** A page keeps its history, so a status
board stays at one address while the work moves under it. A title can be
corrected the same way: `--title` on an update renames the page without
moving it, so a name that turned out wrong is never a reason to abandon an
address people already have.

## Get a key

The token is generated where it will be used and never crosses a terminal,
a transcript or the network — only its SHA-256 is registered:

```bash
artifacts-admin new-key --agent <your-name> --out ~/.config/artifacts/key
kubectl -n artifacts exec deploy/artifacts -- \
  artifacts-admin register-key --agent <your-name> --sha256 <the printed sha256>
```

Never print, cat or paste the key file itself. If a key is lost or leaks,
`artifacts-admin revoke-key <key_id>` in the pod, then mint a new one.

## Write the page

**Publish a complete document**, `<!doctype html>` first. Unlike claude.ai
artifacts, nothing here wraps what you send — the service stores and serves
your file as it is. An agent arriving from claude.ai, where authoring the
skeleton yourself is wrong, has to infer this from the template.

### Start from the template for the job you are doing

`templates/` in this skill's directory holds a complete worked page per kind of
job — real prose showing the register, a worked example of every component, and
a `REPLACE` comment on each region saying what belongs there. Copy one and write
over it. Do not start from a blank file, and do not start from a template for a
different job.

| Job | Template |
|---|---|
| Where an arc stands | `project-status.html` |

More are landing (a decision request, an incident, an implementation plan, a
comparison, an explainer, and the intent and design stages of a piece of work).
Until one exists for your job, start from `project-status.html` and cut what
does not apply — its masthead, tokens and decision block are the parts every
page shares.

Every template already obeys the constraints below, each commented with the
failure that bought it, so you inherit them rather than remembering them: no
typed date, no page-id placeholder, a gallery link that opens a new tab, and a
decision slot shipped commented out.

Follow `design-system.md` for anything the templates do not cover, and **keep
the token block between the `shared:` sentinels exactly as it is** — it is what
makes every homelab page look like one family, and a test in the artifacts repo
fails if a copy drifts.

The older `template.html` is still there as a bare skeleton. Prefer a real
template: `template.html` carries a literal `PAGE TITLE` in both `<title>` and
`<h1>`, and the first board published here shipped with the one in `<title>`
unedited, so the browser tab read `PAGE TITLE` for two hours. Nobody could see
it, because an agent cannot look at its own page.

Hard limits come from the page's own CSP, not taste: one self-contained
file, inline `<style>` and inline SVG, `data:` images, no web fonts, no
CDNs, no forms. Scripts run, but only inline and only inside the page.

Write in outcomes, never plan coordinates. Decisions for Jeremy are lettered
`(a)`, `(b)`, recommendation first — that is how he answers.

## Check what you published

Everything below, and everything in the next section, talks to the API
directly. The key goes to curl on **stdin** as a header file, never as an
argument: an argument is visible in `ps` to every process on the box. Define
this once, and read the trace warning in the next section before you put it in
a script:

```bash
BASE=https://artifacts.tenfourty.site/api/v1
akey() { printf 'Authorization: Bearer %s\n' "$(< ~/.config/artifacts/key)"; }
```

You cannot see your own page render — the gallery holds Jeremy's login and the
page itself only answers a framed request with a token you are never given. So
the service tells you instead, in two ways.

**Every publish and update returns `warnings`**: the constructs in your file
that this service's CSP will not run. A Google font, a CDN script, a hand-rolled
form, an `<iframe>`, `<video>`, `window.claude` — each comes back with its shape,
its line, and what the browser will actually do. `artifacts-publish` prints them
to stderr, so stdout stays the one-line JSON you can pipe. They are advisory: the
publish succeeded. An empty list means the page is clean.

Read them. Before they existed, a page with a Google Fonts link published 201
and looked fine until Jeremy opened it and half of it silently did not render.

**Read the page back** when you want to be sure what is stored:

```bash
akey | curl -sS -H @- "$BASE/artifacts/$ID/body"
```

Returns the current version's body exactly as stored, its version, the
`revision` a later update will accept, and the same warnings re-run on it — so
a page published before any of this existed still reports its own problems.
Only its own agent can read it; anyone else gets a 404.

This is the check that would have caught the first board here keeping the
`PAGE TITLE` placeholder in `<title>` for two hours: nobody could look.

**The whole API describes itself**, with your key:

```bash
akey | curl -sS -H @- "$BASE/openapi.json"
```

Every route, payload, field name and error code. Prefer it to reading this
skill twice — and to guessing.

## Ask Jeremy something on the page

He answers on the page itself, with real buttons. **Never hand-roll this.**
The first agent to publish a substantial page built clickable options, a
textarea and a paste-back block, because nothing told it this existed — a
day's work for an affordance that already ships.

`BASE` and `akey` are defined in the section above — on stdin, never in an
argument, because an argument is visible in `ps` to every process on the box.

**`ps` is only half the reason, and stdin only fixes that half.** `akey` is
not safe under a shell trace: `set -x` expands the command substitution and
prints the token, whatever you then pipe it into. On 19 Sep 2026 an agent ran
its own wait client under `bash -x` to see why it was returning nothing, and
the trace put the bearer token in its transcript — costing a rotation. Reading
"the reason is `ps`" and concluding that a trace is a local debugging aid is a
correct-sounding inference straight into that leak.

So: never run a script that touches the key under `bash -x` or with `set -x`,
and guard the script itself rather than trusting yourself to remember —
`case "$-" in *x*) exit 3;; esac` **before** any `set +x`, because `set +x`
clears the flag it would have detected. A trace goes to a transcript, which is
durable, survives compaction and is re-emitted in the summary; it is the
opposite of ephemeral.

1. Pose the question. Lettered options, because that is how he answers.

```bash
akey | curl -sS -H @- -H 'content-type: application/json' \
  -X POST "$BASE/artifacts/$ID/decisions" -d '{
    "question": "How should sharing work?",
    "options": [{"letter":"a","label":"Secret link"},
                {"letter":"b","label":"Named people only"}],
    "recommended": "a"}'
```

2. In your page, put an empty element where the buttons should appear:
   `<div data-artifact-decision="<the decision id>"></div>`.

3. **Arm the waiter, in the same breath.** This is a step of asking, not an
   optional extra afterwards: an agent that poses a question and stops will
   never learn the answer. Run it as a BACKGROUND task, because the harness
   wakes a session when a background task exits, so the script exiting IS the
   notification.

```bash
artifacts-wait                 # background task; re-arm it each turn
```

   It is on your PATH, so this works from whatever repository you happen to be
   in. It did not, until 20 Sep 2026: the command here was a path relative to
   the homelab repo, and a session working in another one read "does not exist"
   and wrote a stand-in of its own. If yours cannot find it, say so rather than
   improvising -- a hand-rolled waiter is how an answer gets read and thrown
   away.

   On 19 Sep 2026 Jeremy answered three decisions and the owning agent found
   out only when he asked, in a terminal, why it had not. Nothing is pushed at
   you by default.

The gallery renders the real control on **its own** origin and lifts it into
your page over that slot, and the slot reserves exactly the height the control
turns out to be. That is a trust boundary, not a layout choice: your page is
sandboxed on its own origin and holds whatever HTML you published, so a form
rendered inside it could forge an answer or imitate the real one. Only the
gallery origin holds Jeremy's session — which is why your page's CSP says
`form-action 'none'` and why that is correct rather than limiting.

A slot is optional. A decision with no slot appears in a band above the page,
which is the default and not a failure. An answered decision stops being
placeable and its slot releases the room it reserved, so a page with five
questions looks right at every stage.

**The control takes free text beside the letter.** He uses it constantly and
his note often qualifies the choice out of recognition — one "(a)" of his came
with "start with (b) but then (a) if we can". Read the note, always. Never add
your own note field beside the control.

### Reading his answer

```bash
akey | curl -sS -H @- "$BASE/artifacts/$ID/decisions?include_answered=true"
```

**`include_answered=true` is not optional.** The default returns open
decisions only, so a plain call after he has answered comes back empty — which
reads exactly like the answers went nowhere. Each row carries `answer`,
`answer_note`, `answered_at` and `answered_by` alongside the original question.

**Then say you have it.** Reading an answer is not collecting it:

```bash
akey | curl -sS -X POST -H @- "$BASE/decisions/<decision id>/acknowledge"
```

Until you do, the gallery shows Jeremy that question marked *not picked up yet*,
so one he answered correctly goes on sitting at the top of his page as though it
were still outstanding. Four of mine did exactly that for a day, and they are
what filled the top of his own status board with things he had already settled
(20 Sep 2026). Acknowledge each one as soon as you have read it and acted on it.

### Hearing about it

`GET $BASE/events?since=<cursor>&wait=<seconds>` holds the request open and
answers the moment he touches one of your pages. Nothing is pushed at you, so
this needs no inbound address and works the same for an agent that is not on
this machine.

Two ways to use it, and they are for different situations:

- **Idle, waiting on him:** run `artifacts-wait` as a BACKGROUND
  task. The harness wakes a session when a background task exits, so the
  script exiting is the notification. Re-arm it each turn. This is the only
  thing that reaches a session that is doing nothing — on 19 Sep 2026 he
  answered two decisions and the owning session did not find out until he
  asked why it had not started.
- **Busy, and you want the answer now:** a short inline `wait=20`. Never an
  unbounded hold in the foreground; that freezes the turn.

Whichever you use: **read the rows, never trust the wake alone**, and keep the
cursor durably (the script keeps it in a file, written only after the rows are
in hand, so a crash replays rather than loses). An empty answer means "nothing
yet, ask again" — never an error.

## Keep it current

Jeremy reads the gallery to see where work stands, so a page that is right
once and then drifts is worse than no page: it reads as current.

**Whenever you touch a bead for a piece of work, republish that work's page in
the same breath** — `artifacts-publish page.html --update <id>`. Same address,
new version, no new link for anyone to lose. One page per arc, not one per
update: the page is the arc's current state, and its history lives in the
versions behind it.

What goes on it is the outcome, never the plan's coordinates — "the photo
library is uploading off-site, 21 % done", not "T2.1 in progress". That is the
rule for everything he reads, and on a page it is the whole point of the page.

If nothing genuinely needs a decision from him, say so and leave the decisions
block out. A queue of manufactured questions costs him more than an empty
section does.

## After a compaction, do not trust what you remember

Compaction is where a session loses state, and the dangerous part is not the
loss — it is that the session does not know it has lost anything. It comes back
holding a summary that reads like knowledge.

That matters here more than almost anywhere, because **a page published through
this service is read as current**. Republishing a page from a compaction
summary puts a stale claim in front of its reader with a fresh timestamp on it,
which is the one failure this service exists to prevent.

So, after a compaction or on a fresh session, before you touch any page:

- **Re-read the tracker, not your memory.** Whatever tracks your work is the
  state of record. In this house that is beads (`bd prime`, then `bd ready`);
  wherever you are, it is the thing that survived while your context did not.
- **Re-read the page itself.** `GET $BASE/artifacts/$ID/body` gives you the
  body exactly as stored, so you can see what you actually published rather
  than what you recall publishing.
- **Separate what you VERIFIED this session from what you were TOLD.** Say
  which one you are relying on when you write the page. A summary flattens that
  distinction, and it is the distinction that keeps a page honest.
- **Re-arm the waiter.** A background `artifacts-wait` does not survive a
  compaction, and a question whose waiter died reads to you exactly like a
  question Jeremy has not answered.

Ruled by Jeremy, 2026-09-20: *"It's super important we don't lose state or
trust in our state."*

## Send feedback

This service is young and it is being built from what agents hit while using
it. If something is a sharp edge, is missing, or would be better another way,
say so — that is wanted, not noise, and Jeremy has asked for it explicitly.

Send it to the `artifacts` session (`SendMessage`, or leave a note on a page).
Useful feedback names what you were trying to do, what you expected, and what
actually happened; a thing you nearly reported and then found was fine is worth
sending too, because it says which parts of the surface look wrong even when
they are right.

Everything received becomes a bead under `homelab-ij8`, and the sender is told
what was filed or why not.

## What a reader sees

A page opens only inside the gallery frame, with a capability token the
gallery holds. The bare address redirects to the gallery, an unframed
request gets nothing, and a wrong token looks exactly like a page that does
not exist. Page requests are never written to the access log.

Today the service answers on the LAN only; reaching it from outside the
house is Jeremy's decision and is tracked in beads (`homelab-ij8`).
