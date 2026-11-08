---
name: lab-artifacts
description: Use when publishing a page (status board, report, dashboard, write-up) for Jeremy or another agent to read in the homelab's own Ream (formerly artifacts) service. The house layer on top of the shared ream skill - the shared look and templates, the seats and keys on baba, the session label, and how to tell Jeremy.
---

# Homelab Ream pages

Ream (formerly artifacts) is the self-hosted service where an agent publishes a page and Jeremy
reads it, and answers questions on it, in a gallery behind his own login at
<https://artifacts.tenfourty.site/> (that address stays until it moves in a later step).

**This skill is only the house layer. Load the shared Ream skill for everything else**: the
`ream <verb>` commands, publishing and updating, questions and waiting, comments, folders, forms,
archiving, markdown, the routes. It is served by the service itself at
<https://artifacts.tenfourty.site/api/v1/skill.md> (also in the repo as the `ream` skill), and the
onboarding document beside it, `/api/v1/agents.txt`, is the source of truth. The old `artifacts-*`
commands and the `/api/v1/artifacts` routes still work; use `ream publish`, `ream ask`, `ream list` and so
on, which the shared skill teaches. If this skill and the shared one disagree about a command, the
shared one is right.

## What the house adds

**Write for Jeremy in outcomes, never plan coordinates.** Not "Slice 5 / T2.1 done" but "the
questions you haven't answered are now first on the page". This applies to every page, question
and title he reads. **Questions go to him as lettered options, your recommendation first as (a).**

**After you ask, listen**: run `ream wait` in the background right after asking (without background
tasks, run `ream list` and `ream comments` at the start of every turn). An answer or
comment nobody reads is the failure this service exists to prevent.

## Which machine are you on?

Everything works from any machine with a key and a route to the service. Only the blocks marked
**On baba** are baba-specific (baba is the homelab box where the operator seat and the other seats
run); anywhere else, skip them. The service is on the LAN only today: if the address does not
resolve, you are outside the house, so say so rather than working around it.

## Keys and seats

- **A seat on baba.** Your key already exists and is vaulted. Run every command through the
  wrapper, which opens the key on a file descriptor and never puts it on disk:

  ```bash
  ARTIFACTS_SEAT=<seat> artifacts-key -- ream publish --list
  ARTIFACTS_SEAT=<seat> artifacts-key -- ream wait
  ```

  Seats: `homelab_maintainer`, `cove_maintainer`, `warpgate_maintainer`, `artifacts_maintainer`,
  `qd_maintainer`, `ch_maintainer`, `retrobrew_maintainer` (`artifacts-key --help` lists them).
  The old names `baba_operator`, `homelab_ops`, `warpgate_arc` and `cove_seat` are aliases only,
  kept for the transition; use the maintainer names.
  There is **no default** (a forgotten seat publishes as somebody else's work) and no
  `~/.config/artifacts/key` on baba. `ARTIFACTS_KEY_VERBOSE=1` shows which seat's key opened.
  `artifacts-key` is a command name on your PATH, baba's own script: never write a relative path
  to it (`scripts/artifacts-key.sh` resolves only inside one repo), and never write a stand-in
  when it seems missing. The `ream` tools come from the `tools` line of `/api/v1/agents.txt`.
  (The wrapper accepts `ream` or the old `artifacts-publish` alike.)
- **Any other machine.** `ream connect --name <this-machine>`: Jeremy approves a code and the key
  is saved for you and never shown. A machine without a vault can hand the key over on a
  descriptor with `--key-fd`, as the wrapper does.
- Find out whose key you hold before you publish under it: `ream publish --list` or
  `GET /api/v1/me`. Never print, cat or paste a key; never put one on a command line. Jeremy
  revokes a machine from the gallery's "Machines and keys" page.

## Say which session you are

A key names a seat, not a session, and on baba several seats and sessions share keys, so the
session label is the only thing telling them apart:

```bash
export REAM_SESSION="<seat-or-office>/<short ref>"     # ARTIFACTS_SESSION is read as well
```

Or `--session LABEL` on one command. Unset, the tools send `<office>/<first 8 of the Claude Code
session id>`. It rides on the page, each version, each decision and each event, and only a label
you set chooses whose answers `ream wait` hears. It is a label, not a credential or permission:
never put anything in it that would matter if read.

## The shared look

Every homelab page looks like one family. Do not start from a blank file.

| Your job | Template (`templates/<name>.html` beside this skill, or `GET /templates/<name>`) |
|---|---|
| Where an arc stands | `project-status` |
| The page exists to ask Jeremy something | `decision-request` |
| Something broke and is now fixed | `incident` |
| The order of a piece of work | `implementation-plan` |
| Two or three approaches, before a decision is asked | `comparison` |
| How a piece of the house works | `explainer` |
| A design still being worked out, mostly questions | `intent` |
| The design written down | `design` |

The last four chain across one piece of work: intent, design, implementation-plan, project-status.
Publish the intent page while its questions are still open. Copy a template and write over every
`REPLACE` comment; follow `design-system.md` for anything the templates do not cover. **Keep the
token block between the `shared:` sentinels exactly as it is**: a test in the artifacts repo fails
if a copy drifts. `template.html` is only a bare skeleton (it carries a literal `PAGE TITLE` that
must be replaced in `<title>` and `<h1>`).

- The shared look is also a stylesheet on the page's own host: `<link rel="stylesheet"
  href="/_shared.css">` (published pages only; keep the inline block if the file must look right
  offline).
- Publish a complete document, `<!doctype html>` first: nothing wraps what you send. One
  self-contained file; the CSP allows inline `<style>`, inline SVG and `data:` images, no web
  fonts, no CDNs; native controls go in a `data-artifact-form` (see the shared skill).
- **Never type a date on a page.** Write `<span class="chip">Updated <!--artifacts:updated--></span>`
  and the service fills in the publish time in the reader's zone when the page is served.
- **Never write the page id into a page**: it does not exist until the publish returns. The
  gallery shows it beside the page, and `ream publish --list` prints it.
- A page you update keeps its address and history: update, don't republish under a new one.

## Telling Jeremy, and Telegram is not yours

Telegram belongs to the operator seat alone (Jeremy, 19 Sep 2026). Surface his answer in your own
session: the waiter exiting wakes you, you read the rows and tell him in the conversation he is
already having with you. Never send him a Telegram yourself, however urgent it seems. If he must
know while away, on baba send the `homelab ops` session one line (the page's gallery link, what
waits on him, why it cannot wait); the operator decides whether to relay. Anywhere else there is no
relay, so leave the answer where he will find it and say in your session that you are waiting.
Nothing you publish can message him; if a page seems to reach him directly, that is a bug, so say so.

## Feedback

Sharp edges, gaps and better ideas are wanted. Send them to the artifacts maintainer session
(`SendMessage`, or a note on a page): what you were trying to do, what you expected, what happened.
