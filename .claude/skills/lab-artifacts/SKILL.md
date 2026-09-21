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

## Which machine are you on?

**Everything here works from any machine with a key and a route to the
service.** That is the design: Jeremy's brief was *"ultimately it needs to work
for agents anywhere that I give access to"*, so the commands, the page rules,
the decisions and the event feed make no assumption about where you are.

A few things are true only on **baba**, the homelab box where the operator seat
and today's other seats run. Those are marked like this:

> **On baba.** Whatever only applies there.
>
> **Anywhere else.** What to do instead.

**If you are not on baba, skip the baba blocks entirely.** They describe a
shared key file, a sops vault and an operator you can hand a message to, and
none of those exist for you. Nothing in them is required to use the service.

**Can you reach it?** The service is on the LAN only today, so an agent outside
the house cannot reach `artifacts.tenfourty.site` at all. That is expected, not
a fault of your setup — public exposure is deliberately gated on a security
pass that is not finished (homelab-ij8.2). If the address does not resolve or
does not answer, you are outside; say so rather than working around it.

## Publish

```bash
artifacts-publish page.html --title "Its title"        # new page
artifacts-publish page.html --update <id>              # same address, new version
artifacts-publish page.html --update <id> --title "Its real name"   # and rename it
artifacts-publish --list                               # what this key has published
```

## Read back what you published — and keep the source somewhere that survives

```bash
artifacts-publish --fetch <id> page.html     # the page's own HTML, back on disk
artifacts-publish --fetch <id> | head -40    # or just look at it
artifacts-publish --versions <id>            # what versions exist, and which is live
artifacts-publish --fetch <id> --at-version 3 old.html
```

**Rolling back is a read and an ordinary update**, not a verb:

```bash
artifacts-publish --fetch <id> --at-version 3 old.html
artifacts-publish old.html --update <id>
```

The restored bytes land as a NEW version. History here is appended to, never
rewritten — a service whose argument is that the record is not edited cannot
offer a command that edits it.

**Edit what is there; do not regenerate from memory.** A republish replaces the
whole document, so rewriting from scratch silently drops whatever the last
version promised — a row someone added, a caveat, a number you no longer
remember. Fetch, edit, publish.

**Keep the source somewhere that outlives the session** — `~/.local/state` or
wherever your machine keeps durable per-user state — and never in the session
scratchpad. A scratchpad is temporary by design: the page outlives your session
and its source does not.
On 21 Sep 2026 a seat lost a page's source that way, guessed six URLs for a
read-back route, got 404 from all of them and concluded a live page it owned
could never be updated again. Nothing was actually lost — `--fetch` recovered
it — but the page nearly went stale for good.

Over HTTP it is `GET /api/v1/artifacts/<id>/body`, scoped to the owning key,
returning the HTML with its `version`, `revision` and CSP `warnings`. The names
that do NOT work are `/source`, `/content`, `/html`, `/versions`, `/revisions`
and `/history`; ask for any of them and the 404 now tells you the real ones.

Every version's bytes are kept — `current_version: 8` means eight real versions
exist — but only the current one is reachable through the API today.

## Housekeeping: everything Jeremy can do to a page, you can do to yours

```bash
artifacts-publish --rename <id> --title "Its real name"   # no FILE: the body is not sent
artifacts-publish --archive <id>                          # off the wall, nothing deleted
artifacts-publish --unarchive <id>                        # back on it
artifacts-publish --pin <id>                              # leads the gallery
artifacts-publish --unpin <id>
artifacts-publish --duplicate <id>                        # a copy at a new address
```

Deleting is its own thing, over the API rather than the CLI, because it is the
one action here with a clock on it:

```bash
akey | curl -sS -X DELETE -H @- "$BASE/artifacts/<id>"        # 30 days to change your mind
akey | curl -sS -H @- "$BASE/artifacts/deleted"               # what you have deleted, soonest to go first
akey | curl -sS -X POST -H @- "$BASE/artifacts/<id>/restore"  # put it back, address and history intact
```

**A delete destroys nothing.** The body, every version, and every decision with
his answers and his notes are all still there for the window — this is the cheap
way to tidy, never the way a record of what he decided is destroyed. You can
recover your own pages and so can he; that is deliberate, because an agent
deletes a page in error as readily as a human does and it is usually the agent
that notices, within seconds and while he is asleep.

After thirty days a nightly sweep destroys it for good. `POST
/artifacts/<id>/destroy` with `{"confirm": "destroy"}` does it now instead —
separate from delete on purpose, because "tidy this away" and "this must not
exist anywhere" are different intentions.

These exist because of a rule of his (20 Sep 2026): *"ideally agents can take
all the same actions a human can."* Archive, rename and pin started as controls
in his gallery; an agent that had published a page months ago and no longer had
the file could not so much as fix its name. None of these send a body, none of
them makes a new version, and each is scoped to your own key -- another seat's
page answers 404, exactly as a page that does not exist does.

`--list` gains a fourth tab-separated column, `pinned` or `archived`, only for
the pages that have one. **Archiving is not deleting.** The address still works,
the versions are all there, and the decisions and answers on the page are
untouched -- it is off the wall, not gone.

**A duplicate copies the body and nothing else** -- no versions, no comments,
and no decisions. That last one is the point: an answered decision is a record
of something *Jeremy* said about the original, and copying it would manufacture
a record of him answering a question he has never seen. The copy is owned by the
original's agent, whoever pressed the button, so the seat that wrote it can
still update it.

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

`new-key --out` **refuses to overwrite an existing key file** — it exits 1 and
leaves the file untouched, so you cannot destroy a working key by re-running
the command. If the file already exists, find out whose key it is before
publishing under it: `GET /api/v1/me` answers in one call, and a page published
under somebody else's identity is somebody else's page.

The second command needs `kubectl` against the cluster. If you have no cluster
access, mint the key locally, send the **SHA-256 only** to whoever does, and
have them register it — the token itself never has to travel, which is the
whole point of registering a hash.

> **On baba.** `~/.config/artifacts/key` already exists and belongs to the
> shared `baba-operator` seat, not to you. See "which key you are using" below.
> The keys there are also vaulted — see the sops block further down.
>
> **Anywhere else.** There is no shared key and no vault. Mint your own with
> the command above, keep it mode 600, and that is the whole story.

Never print, cat or paste the key file itself. If a key is lost or leaks,
`artifacts-admin revoke-key <key_id>` in the pod, then mint a new one.

## Write the page

**Publish a complete document**, `<!doctype html>` first. Unlike claude.ai
artifacts, nothing here wraps what you send — the service stores and serves
your file as it is. An agent arriving from claude.ai, where authoring the
skeleton yourself is wrong, has to infer this from the template.

### Start from the template for the job you are doing

Eight complete worked pages, one per kind of job. Each is a real page with
prose in place and a `REPLACE` comment on every region saying what belongs
there. Copy one and write over it. Do not start from a blank file, and do not
start from a template for a different job.

| Your job | Template |
|---|---|
| Where an arc stands | `project-status` |
| The page exists to ask Jeremy something | `decision-request` |
| Something broke and is now fixed | `incident` |
| The order of a piece of work | `implementation-plan` |
| Two or three approaches, before a decision is asked | `comparison` |
| How a piece of the house works | `explainer` |
| A design still being worked out, mostly questions | `intent` |
| The design written down | `design` |

The last four chain across one piece of work: **intent → design →
implementation-plan → project-status**. If you are running a design process,
publish the intent page while the questions are still open — that is the page
Jeremy's answers change the most, and it is the one the whole service exists for.

**Two ways to get one.** From this skill's directory, `templates/<name>.html`.
Or from the service, which works on any machine and for an agent that has no
skill directory at all:

```bash
akey | curl -sS -H @- "$BASE/templates"                      # the list, with what each is for
akey | curl -sS -H @- "$BASE/templates/project-status" -o page.html
```

(`BASE` and `akey` are defined under "Check what you published" below — the key
goes to curl on stdin, never in an argument.)

Every template already obeys the constraints that have caught agents out, each
commented with the failure that bought it, so you inherit them instead of
remembering them: no typed date anywhere, no page-id placeholder, a gallery link
that opens a new tab, and a decision slot shipped commented out so a made-up id
can never render as a slot that spins for ever.

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

The same list carries `unresolved_decision_slot`: a `data-artifact-decision`
slot whose id names no decision on this page — a typo, an id from another page,
a template's `the-decision-id` left in. It comes with the slot's line and id.

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
artifacts-decide ask "$ID" \
  --question "How should sharing work?" \
  --option a="Secret link" \
  --option b="Named people only"
```

   **The first option is the recommendation** unless you pass `--recommend`.
   That is his house style — the recommendation first, as (a), because that is
   how he answers — so the common case needs no flag.

   The key is read from the file exactly as `artifacts-publish` reads it, so it
   never crosses a command line. Prefer this to curl: the curl form below still
   works and is what this command sends, but every hand-rolled variant of it is
   a fresh chance to put a token in a transcript.

<details><summary>the same call as curl, if you need to vary something</summary>

```bash
akey | curl -sS -H @- -H 'content-type: application/json' \
  -X POST "$BASE/artifacts/$ID/decisions" -d '{
    "question": "How should sharing work?",
    "options": [{"letter":"a","label":"Secret link"},
                {"letter":"b","label":"Named people only"}],
    "recommended": "a"}'
```

</details>

2. In your page, put an empty element where the buttons should appear:
   `<div data-artifact-decision="<the decision id>"></div>`.
   The id must be one step 1 returned **for this page**. Anything else comes
   back as an `unresolved_decision_slot` warning, and Jeremy sees a sentence
   in the slot saying the question does not exist. Before 21 Sep 2026 he saw
   blank space, and read it as a question that had lost its options.

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

**The order is forced, and getting it wrong looks like nothing at all.** A slot
needs a decision id, a decision needs an artifact id, so the only sequence that
works is:

1. `artifacts-publish page.html --title "..."` — get the page's id
2. `artifacts-decide ask <id> --question ... --option a=...` — get each decision's id
3. put the slots in the page and `artifacts-publish page.html --update <id>`

Write the slots first and you publish a page of empty divs with no error and no
obvious cause. Both halves are documented above; that they are *sequential* is
the part that caught two agents on 19 Sep 2026. This is also why the templates
ship their decision slot commented out.

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
artifacts-decide answers "$ID"     # one page, answered ones included
artifacts-decide list              # every page this key owns, open only
artifacts-decide list --answered   # ...including the ones he has answered
```

An answered row leads with the letter, says whether he **tapped it** or a seat
**relayed** it, and prints his note in full. An answer you have not collected
yet says so and gives you the `ack` command. Nothing to report prints a
sentence saying so rather than printing nothing — an empty result and a broken
call look identical when both are silent, and this service has already lost a
shift to that.

```bash
artifacts-decide ack <decision id>    # "I have seen it"
artifacts-decide close <decision id>  # withdraw a question you no longer need
```

**Say what it caused, and what moved under it.**

```bash
artifacts-decide outcome <decision id> "PR #14 opened; the job was smaller than I claimed"
```

Append-only, and allowed **while the question is still open** &mdash; which is
the case that matters most. If you correct the framing of a page after posing a
question on it, say so here: a question whose premise moved after he read it is
one he can answer wrongly through no fault of his own. On an open question it
appears above the buttons, headed *Since you were asked*, so he reads it before
choosing. On an answered one it is *What it led to*.

There is no edit and no delete. Several accumulate and none replaces another,
because the thing this exists to end is a summary somebody keeps rewriting until
what actually happened is gone. If you were wrong, append the correction.

<details><summary>the same reads as curl</summary>

```bash
akey | curl -sS -H @- "$BASE/artifacts/$ID/decisions"
```

Each row carries `answer`, `answer_note`, `answered_at` and `answered_by`
alongside the original question. **Answered decisions come back by default**
since 21 Sep 2026 — before that a plain call after he had answered came back
empty, which reads exactly like the answers went nowhere. Add
`?open_only=true` for what still needs him, and `?include_closed=true` to
include a question that was withdrawn.

```bash
akey | curl -sS -X POST -H @- "$BASE/decisions/<decision id>/acknowledge"
```

</details>

**Then say you have it.** Reading an answer is not collecting it —
`artifacts-decide ack <decision id>`, or the curl above.

Until you do, the gallery shows Jeremy that question marked *not picked up yet*,
so one he answered correctly goes on sitting at the top of his page as though it
were still outstanding. Four of mine did exactly that for a day, and they are
what filled the top of his own status board with things he had already settled
(20 Sep 2026). Acknowledge each one as soon as you have read it and acted on it.

### Say which session you are

A key identifies a SEAT, not a session — so without this, nothing says which
run of which agent did a thing, and two sessions sharing one key are
indistinguishable in the record. Set it once, at the top of your session, and
every command carries it:

```bash
export ARTIFACTS_SESSION="artifacts-seat/$(date +%s | tail -c 7)"   # <seat>/<short id>
```

Or pass `--session LABEL` to a single command. `GET /api/v1/me` echoes back what
you declared, so one call tells you whether your plumbing works; `null` means
your writes carry no session at all and nothing later can say which run made
them.

> **On baba.** Four seats share one key file, so this is the ONLY thing
> separating them: without it the service can say which machine published a
> page and nothing can say which of us.
>
> **Anywhere else.** Your key is probably yours alone, so the seat name already
> identifies you — but set it anyway. It is what lets you filter your own
> answers out of a shared feed later, and it costs one line.

It rides on the page, on each version, on the decision you ask and on the events
your work produces &mdash; which is what makes this possible:

```bash
artifacts-wait --session "$ARTIFACTS_SESSION"   # only YOUR answers wake you
akey | curl -sS -H @- "$BASE/events?session=$ARTIFACTS_SESSION&wait=280"
```

**It is a label, not a credential and not a permission.** It travels in the
clear, anyone with the key can send any value, and nothing verifies it. Never
put anything in it that would matter if read, and never build a check on it
&mdash; Jeremy ruled that seats sharing a key may read and edit each other's
pages, and that is deliberate. Filtering the feed hides rows you were always
allowed to see; it does not protect anything.

Events written before you set it have no session, so a filtered feed leaves them
out. That is the cautious direction and it is why the filter is worth having.

### Where the key lives at rest

A key on disk is a bearer token on disk. Mode 600 protects it from another
user and from nothing else — not a stray read, not a shell trace, not a
process that logs its own config.

> **On baba.** The four keys live in sops and a wrapper hands one to a command
> on a file descriptor, so the plaintext never touches disk:
>
> ```bash
> ARTIFACTS_SEAT=homelab_ops artifacts-key -- artifacts-publish page.html
> ARTIFACTS_SEAT=homelab_ops artifacts-key -- artifacts-wait
> ```
>
> Seats there are `baba_operator`, `homelab_ops`, `warpgate_arc`, `cove_seat`,
> and there is **no default**: on a box four seats share, a forgotten seat
> publishes as somebody else's work.
>
> **Anywhere else.** There is no vault and no wrapper. Keep the key in a 0600
> file, or hand it to the tools the same way the wrapper does if your machine
> has a secret store of its own:
>
> ```bash
> artifacts-publish --key-fd 3 page.html 3< <(your-secret-store read artifacts-key)
> ```
>
> `--key-fd` is part of the tool, not of baba. Anything that can write a key to
> a descriptor can use it, and then you have no plaintext file either.

**Never write a relative path to a helper.** `artifacts-key` and
`artifacts-wait` are command names on your PATH. `scripts/artifacts-key.sh`
resolves only inside one repo on one machine, and an agent working anywhere
else reads "does not exist", concludes the tool is missing, and writes a
stand-in of its own — which is exactly what happened to the waiter
(homelab-ij8.67), and then again to the key wrapper six hours after that bead
was read.

### Check the page before you publish it

```bash
artifacts-publish --check page.html
```

A page that breaks the serving policy does not error &mdash; it publishes
happily and then silently loses its styling, its images or its script, and
**you cannot see that**: the content origin only answers a framed request
holding a token no agent is given. So check first. It reads a file, needs no
key, makes no request, and exits non-zero on anything the policy will refuse:

```
page.html:2: remote_stylesheet -- style-src is 'self' ... Google Fonts is blocked
page.html:4: form_element -- form-action is 'none': a form cannot submit anywhere
page.html: inline script at line 5: SyntaxError: Unexpected token ','
```

It uses the service's own shape table, so it cannot drift from what the browser
will actually do. Inline scripts also go through `node --check` when node is
present, and it says so when it is not, rather than passing quietly.

A publish still only WARNS about the same shapes, on purpose: the server cannot
refuse a page that is 95 % right over a literal `<form` in an escaped code
sample. `--check` refuses because you are the one looking at the file, and you
may publish anyway.

**Which key you are using is not obvious, and getting it wrong is silent.**
The default key file is a path, not an identity: whose key is in it is a
separate question, and a seat grant made to the seat you *meant* is applied to
an identity nobody is using. Ask, rather than assume — one call, from anywhere:

```bash
akey | curl -sS -H @- "$BASE/me"      # {"agent": ..., "key_id": ..., "scopes": [...]}
```

> **On baba.** Every agent reads `~/.config/artifacts/key` and it belongs to
> ONE seat, `baba-operator`. The operator's own calls use a different file,
> `~/.config/artifacts/key-homelab-ops`, which is the seat holding
> `decisions:read-all`. On 20 Sep 2026 a `?scope=all` call returned 403, two
> sessions went looking for a missing grant, and the grant existed — on a seat
> whose key nobody was passing.
>
> **Anywhere else.** Your key file is probably yours alone, which makes this
> cheap rather than unnecessary: run `/me` once when you set the seat up and
> you never wonder again.

### When he answers somewhere else

He answers in a terminal or on Telegram as often as on a page. **Never press the
button for him.** That mints an answer attributed to him, on the page,
indistinguishable from one he tapped — the kind of record a later agent believes.
Record it as what it is:

```bash
artifacts-decide record <decision id> \
  --letter b \
  --note "his exact words, or the exact label he tapped" \
  --source telegram \
  --said-at 2026-09-20T11:30:18Z
```

The command does not offer `--source page` at all, so the one thing you must
never claim cannot be typed by accident.

<details><summary>the same call as curl</summary>

```bash
akey | curl -sS -H @- -H 'content-type: application/json' \
  -X POST "$BASE/decisions/<decision id>/record" -d '{
    "letter": "b",
    "note": "his exact words, or the exact label he tapped",
    "source": "telegram",
    "said_at": "2026-09-20T11:30:18Z"}'
```

</details>

`source` is `terminal`, `telegram`, `in-person` or `other` — never `page`, which
means he tapped it himself. The recording seat comes from your key, not the body.
`note` is verbatim: his notes routinely qualify the letter they came with, so a
paraphrase is a paraphrase of a decision.

**Recording is not collecting.** The gallery shows the answer as relayed, and the
OWNING seat must still acknowledge it with its own key — acknowledgement means
"the agent that asked has seen this", and your having written it down says
nothing about whether that agent knows.

### Telling him — and Telegram is not yours

**Jeremy, 19 Sep 2026, verbatim:** *"telegram is only for the ops agent, it is
not for all agents. so the agents should communicate with me in their session,
but if they are on the same machine as ops (baba) they can ask ops to send me a
telegram message to notify me if I'm not in the loop."*

So:

- **Surface his answer in your own session.** That is what the waiter exiting
  is for — the harness wakes you, you read the rows, and you tell him in the
  conversation he is already having with you. It needs no address and works
  for an agent on any machine.
- **Never send him a Telegram.** Not as a convenience, not "just this once",
  not through a script you found. It is one channel belonging to one seat, and
  four agents each deciding their thing is urgent is how a channel becomes
  noise he stops reading.
- **If he genuinely needs to know while he is away**, and there is an operator
  seat you can reach, ask it to relay — a request to a colleague with a reason
  attached, not a mechanism you invoke. If there is no such seat, there is no
  route, and the honest thing is to leave the answer where he will find it and
  say in your session that you are waiting.

> **On baba.** The operator seat is the one holding Telegram; ask it.
>
> **Anywhere else.** You have no relay. Do not go looking for one.

**Nothing you publish here can message him.** The service has no Telegram code
at all; its one outbound push goes to a single seat's own mailbox for that
seat's own pages, and what the operator does with it is the operator's
decision. If you think you have found a way to make a page reach him directly,
you have found a bug — say so.

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

**An event can also tell you a question has gone quiet.** Since 21 Sep 2026 a
fourth kind, `decision_shelved`, fires when the page holding one of your open
questions is archived or deleted. It means Jeremy is no longer being shown that
question — not that it is dead: nothing is destroyed, and it comes back if the
page does. **Stop waiting on it and say so**, rather than holding a waiter that
will now only ever time out. The `note` on the event says all of this, and
`ref_id` is the decision it concerns.

**The event carries his note, but the decision row is still the authority.**
Since 20 Sep 2026 each event has its own `note` field holding what he actually
typed, verbatim, up to 1000 characters — before that the feed carried only a
one-line `summary` and his words were simply not in it, which is how the
operator session missed a note of his that afternoon. `note` is a separate
field from `summary` on purpose: do not read one expecting the other.

Even so, the event is a nudge, not the record. Go and read the decision
(the plain read above) before you act: the row is what the gallery
shows him, it is what a later agent will find, and `summary` is flattened for
safe printing in a terminal. Treat the event as "something happened on page X",
then go and look at X.

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
