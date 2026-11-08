# Artifacts decisions

What was decided about the artifacts service, why, and why each other option lost: one entry per
decision, newest last. The rule and the entry format are runcove's `ways-of-working/decisions.md`.
The designs under `docs/agent/specs/` hold the detail a decision led to; this file holds the choice.

## Archiving a page sets its open questions aside instead of closing them (2 Nov 2026)

**Chosen:** archiving or binning a page sets aside every question on it that is still open. While
the page is archived or binned, its questions show nowhere Jeremy looks (his wall, his waiting count,
the page itself, any office's list of what waits on him), and he cannot answer them. Unarchiving or
restoring brings them back open and unanswered, keeping their place by when they were first asked.
The agent that asked is told both times. Archiving a folder does the same to every page in it. Why:
he archives a page to get it off his wall, not to throw its questions away, and a question that
comes back with its page saves the agent asking again. This replaces the 21 Sep 2026 rule that
archiving or binning a page closes its open questions.

**Not chosen:**
- Keep the 21 Sep rule, archiving closes the questions: hides them just as well, but a question is
  lost for good if the page comes back, and the agent has to notice and ask again.
- Leave the questions open with nothing else changed, as before 21 Sep: the waiting agent could not
  tell "not answered yet" from "never going to be shown", and an office's waiting list once put such
  a question in front of him 24 minutes after its page had left his wall.

**Evidence:** confirmed (Jeremy, 2 Nov 2026)

## The Older pages link is proven by CI until a real view passes 200 pages (5 Nov 2026)

**Chosen:** the Older pages link on the wall (it shows the next 200 pages of a view past the
200-page cap) counts as proven by CI's tests, which page the wall, a folder and the Archived tab,
search and filters included. The maintainer checks it in the browser the first time a real view
passes 200 pages. Why: no view in production holds more than 200 pages yet (the widest held 162 on
5 Nov 2026), so there is nothing for him to tap, and the tests already cover what a tap would show.

**Not chosen:**
- Fill a throwaway folder with 205 blank pages so he can tap the link at once, then delete them:
  real proof today, but it clutters the wall, the events and every office's reads to prove what the
  tests already prove.
- Leave it unproven until a real view passes 200 pages, then ask him: keeps the work open for weeks
  with nothing left to do on it, and puts the check on him when the maintainer can make it.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## The company deployment's design restarts now, and building waits for the core clean-up (5 Nov 2026)

**Chosen:** design work on the company deployment (artifacts for the whole of GitGuardian)
restarts now. Every question still open on it (four from 26 Oct and four from his 27 Oct page
comments) is withdrawn from where it was asked and asked again in one new thread in
#hq-artifacts. Each question is first revised against what has changed since then, and any that
newer work already settles is dropped. The existing design page is republished as a new version,
not replaced by a new page. That version takes in what has shipped since 27 Oct (pinned comments,
forms, receipts), the core-and-plug-ins design, and the 4 Nov ruling that the core stays
single-reader until this design adds many readers. Building the company deployment comes after
the core clean-up. Why: he expects to start the company deployment soon, and its questions were
scattered over two threads and partly overtaken by later work.

**Not chosen:**
- Build the company deployment before the core clean-up: it would add many readers on top of
  the household-specific code the clean-up removes, so that code would have to be untangled twice.
- Move only the four 27 Oct questions and leave the 26 Oct four in their own thread: keeps two
  threads for one design, which is what made the open questions hard to follow.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## Selecting text always offers Comment, even in pin mode, until commenting is redesigned (5 Nov 2026)

**Chosen:** a text selection made while pin mode is on leaves pin mode and shows the selection bar
(Comment, Remove this, Looks good). A click that picks an element still works as before. Why: pin
mode held the bar back with nothing on screen saying so, and Jeremy, who never meant to turn it on,
could not comment on any page. This is a stopgap. The whole reader-side comment experience is being
redesigned from the best existing tools, and that redesign may replace pin mode entirely.

**Not chosen:**
- No stopgap, wait for the redesign: leaves a reader who presses C by accident unable to comment
  for days.
- Keep holding the bar back but show a strip saying pin mode is on: still blocks what the reader
  plainly asked for, which is to comment on the words selected.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## Commenting: a corner button replaces pin mode, one chord shortcut, a page button, Open and Resolved tabs (5 Nov 2026)

**Chosen:** commenting on a chart, table or picture is a small Comment button in that element's
corner, shown when the pointer is over it (or it has keyboard focus, or after a first tap on
touch); pressing it starts a comment on that element. Pin mode, the Pin button and the bare C key
are removed. The only commenting shortcut is Ctrl+Shift+M (Cmd+Shift+M), which comments on the
selected words. A "Comment on page" button in the page bar starts a whole-page comment. The
comments panel has Open and Resolved tabs, and a resolved thread can be reopened. Why: pin mode
was a loud state the reader entered by accident, could not see the way out of, and blocked the
page's own clicks and the selection bar while on (it left Jeremy unable to comment on 5 Nov);
a button next to the thing itself needs no mode and no key to learn, and what agents receive is
unchanged.

**Not chosen:**
- A loud picking mode that ends after one comment: still a mode to enter, to see and to leave,
  and still swallows the page's clicks while it lasts.
- Both the corner button and a picking mode: two ways to do one thing, and the mode's faults stay.
- Keeping C as a shortcut shown on the button: a single letter fires by accident, in a page that
  has its own keys, and was how the mode got turned on unmeant.
- A whole-page comment only at the panel's foot: not found by a reader who has not opened the
  panel.
- A resolved fold at the bottom of the panel: resolved threads sit below every open one and are
  not found again; tabs keep both lists in view one click apart.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## Inside a folder, the All tab names the folder and hides when it is alone (5 Nov 2026)

**Chosen:** on a folder's page the first tab reads "All in <folder name>" (the name escaped), and
when it is the only tab the tab row is not drawn at all. Waiting on you, Pinned and the other
tabs still appear once they have something in them, as on the wall. Why: "All" inside a folder was
already selected and reloaded the same view, so it read as a way out of the folder and did
nothing; the label now says what it covers, and a button that can do nothing is not shown.

**Not chosen:**
- Make All leave the folder and show the whole wall: the Gallery link already does that, and two
  ways out of one folder confuse.
- Keep it as it was: a selected button labelled "All" that reloads the same view, which a reader
  takes for a way out.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## A sent form shows as a read-only summary with a Change answers button (5 Nov 2026)

**Chosen:** once Jeremy has sent a form, the page replaces it with a read-only summary: plain text,
each question and the answer he sent, and when. A Change answers button under it brings the form
back with his answers filled in, and the page bar then offers Send again; after he sends, the
summary returns. Reloading the page shows the summary again while his send stands. The button is a
real button, reachable by Tab, and focus lands on it (or on the form's first field) when the view
changes. What agents receive and the API do not change: only what the gallery draws. Why: a filled
form with a Sent bar and a Send again button read to him as strange, as if something were still
to be filled in.

**Not chosen:**
- Keep the form but locked read-only, with an Edit button: it still looks like a form to fill in,
  and the strangeness he named stays.
- Keep it as it was (an editable filled form, a Sent bar and Send again): the thing he found
  strange.

**Evidence:** confirmed (Jeremy, 5 Nov 2026)

## The open project is named Ream; what it makes is still "a page" (7 Nov 2026)

**Chosen:** the open project takes the name **Ream**. What an agent makes and shares stays "a page"
(or "a webpage"): "create a page on Ream", "share a page from Ream", "my pages on Ream". A ream is a
pack of paper, so the name pictures a place that holds many pages without renaming the page itself,
and it is four letters, easy to type and to say (`ream publish`). The skill can still trigger on
"share this page". GitGuardian's internal instance keeps its own Roman name, Carmenta. Nothing is
renamed yet: a rename plan (repository, command-line tools, docs, the homelab address, with every old
name kept working) comes to Jeremy for approval before any change.

**Not chosen:**
- Keep "artifacts": the word is crowded (Claude's own Artifacts, and several self-hosted HTML
  publishers for agents use it), and it is not the word people use for what they share.
- A shortlist of Ream beside other page names (Leaf, Verso, Colophon, Pagewall, Pagina), shown as
  wordmarks before choosing: Jeremy already had the name he wanted, so a comparison only delays it.
- Another round of names: the earlier rounds' clash-checked picks (pinwall, tack, Sheetwall,
  wallsheet) were rejected as not good names, and Ream answers the rule they missed, that the thing
  made is "a page".
- A name that is itself the noun for one page (placard, broadside, pamphlet): Jeremy wants "page" to
  stay the word, so the app's name should belong to pages, not replace the word.

A known cost: in American English "to ream" is also slang for a harsh telling-off, with a cruder
sense too; weighed and accepted.

**Evidence:** confirmed (Jeremy, 7 Nov 2026)

## One shared skill for showing anything to a person, in place of a host's built-in panel (7 Nov 2026)

**Chosen:** the skill that ships with Ream is the one an agent reaches for whenever it shows or shares
something with a person beyond a short chat answer: a report, a plan, a chart, a diagram, a
comparison. It does not wait for the words "page" or "publish". Where it is installed on Claude.ai or
ChatGPT and a key is set up, it is used instead of their built-in artifacts or canvas; with no key,
the agent falls back to the built-in panel and says why. A page carries a question only when the
agent is blocked on the person or needs their choice, never just to have one; otherwise it is
something to read. The skill is written for any agent and any host; the homelab's own skill keeps
only its house look and keys on top. It is written once, under the new name, as part of the rename.
Why: a skill that fires only on "publish a page" is missed whenever the agent thinks of what it is
doing as a report or a chart, and the person then gets it in a chat panel they cannot comment on or
answer from.

**Not chosen:**
- Fire on every reply, short ones included: a one-line answer belongs in the chat; a page for it is
  noise.
- Always replace the built-in panel and ask the person for a key when none is set up: blocks a
  person who has not installed Ream yet from seeing anything at all.
- Change only the homelab's skill for now: other agents and hosts are the point of the open
  project, and two skills drift.
- Write it now under the current name and rename it later: the rename is already planned, so it
  would be written twice.
- Leave questions entirely to each agent: agents then add a question to every page, and the person
  is asked for decisions nobody needs.

**Evidence:** confirmed (Jeremy, 7 Nov 2026)

## Ream lives at its own address, is typed as one command, and its API says pages (7 Nov 2026)

**Chosen:** three choices in the rename to Ream, each kept beside a working old name.
- **The address:** Ream moves to `ream.tenfourty.site`, and the old address forwards to it for good.
  People sign in once more, because a sign-in belongs to an address.
- **What agents type:** one `ream` command with verbs (`ream publish`, `ream ask`, `ream wait`).
  It reads the way a person says it. The `artifacts-*` commands keep working.
- **The API's word:** `/api/v1/pages` becomes the main route, and `/api/v1/artifacts` keeps working
  for good, because what an agent makes is a page.

What never changes: the code's import name, the cluster's namespace, service and volume names, the
database, the page addresses on the content domain, and the office id. Nobody sees them, and renaming
them is where the risk is.

**Not chosen:**
- Keep `artifacts.tenfourty.site` and change only the name on the page: the address is the name
  people see most, and it would keep saying the old one.
- Separate commands as today (`ream-publish`, `ream-decide`): longer to type, and two words for one
  tool.
- Leave `/artifacts` and say "pages" only in the docs: the API would keep a word for the thing that
  its own docs no longer use.

**Evidence:** confirmed (Jeremy, 7 Nov 2026)

## Ream inside the code too, the cluster names included, with old names kept working for good (7 Nov 2026)

**Chosen:** the rename to Ream goes all the way into the code, not only what people and agents see.
This replaces the "what never changes" list in the entry above, except for page addresses and the
office id, which still never change.
- **The code:** the package and every import, the settings names (`REAM_…`), logs, tests, scripts
  and CI all say ream. Someone reading the code meets "artifacts" only where it is kept on purpose
  for old callers.
- **The deployment's cluster names** (namespace, service, volume, secret files) are renamed as
  well. Homelab runs that as its own migration, with a maintenance window, because the data and its
  backups have to move.
- **Old names keep working for good:** the `ARTIFACTS_…` settings and the `artifacts` import stay
  as aliases, so nothing outside the repository breaks on the day or later.
- **Order:** the code refactor comes first. It lands as one mechanical change once the two held
  clean-up changes are in, before any wording or address change.
- **The plan is approved** with this added, so the work starts when those clean-up changes land.

**Not chosen:**
- Leave the cluster names alone: that avoids moving data, but they would keep the old name for
  good.
- Keep the old names for one release with a warning, then drop them: that breaks callers outside
  the repository that nobody has listed.
- A clean break with no aliases: everything outside the repository breaks on the day.
- Do the user-facing steps first: the wording and address changes would then be written against
  code that is about to be renamed under them.

**Evidence:** confirmed (Jeremy, 7 Nov 2026, lettered answer to the in-code rename playback)

## The core runs for anyone, and our house specifics become plug-ins in a separate repo (4 Nov 2026)

**Chosen:** Ream's core is cleaned of everything particular to one household and works with no
plug-in installed. A fresh install with nothing set starts, serves a gallery, takes pages and
decisions, and says nothing about anyone's house. What is ours (the quarterdeck bridge and its
inbound route, the doorbell, GitGuardian scanning, Mailgun, the tracker pages, our default
endpoint and wording) becomes plug-ins, kept in a separate repo and installed into the deployed
image at build time; the artifacts repo holds only the core. The cleanup comes first, before any
new feature, and goes in slices that each leave our deployment behaving exactly as today (same
rows, same API, same text, same events, checked by golden tests of the served text).
- **Extension points:** one entry-point group (`ream.plugins`, old `artifacts.plugins` kept),
  one function `register(api)`, and one emit point (`events.emit`) that journals, writes the
  outbox in the action's transaction, and fans out after commit. The interfaces are event sink,
  answer source, answer hook, scanner, mail backend, CLI command and client defaults, plus text
  variables, text sections and a router on a dedicated host. The extension points exist only where
  a house module has to plug in today.
- **Stays single-reader:** the owner becomes a setting (id, name, pronoun), stored in
  `service_meta` so a wrong id refuses to start instead of reading old answers as a stranger's.
  Many readers is a later design.
- **A failing plug-in is never silent:** one that cannot load or declares another API major
  stops the server's start-up; one that fails at run time is counted and logged and, for a sink,
  paused by a per-sink circuit breaker. The API is versioned from day one.
- **Stored values stay data, not code:** the two schema CHECKs that named house values (answer
  source, event kind) are dropped and validated in code, so a plug-in can add a source without a
  migration; old rows keep their values.
- **Built so far (8 Nov 2026):** steps 1 to 7 of the design, including slice 7b. **Not built:**
  step 8 (the plug-in repo, its image pipeline and the pin-lander change) and step 9 (moving each
  house module out of the core, one at a time). The author guide is `docs/plugins.md`.

**Not chosen:**
- Leave the house code in the core behind settings only: the core would still import
  Mailgun, GitGuardian, quarterdeck and the doorbell by name, and every new feature would add to
  the debt.
- Build new features first and clean up later: the coupling compounds with each one.
- Keep our plug-ins in the artifacts repo: the repo would no longer hold only the core.
- Build the deployed image from the artifacts repo by cloning the plug-in repo (the design's
  option B), or from a third assembling repo (option C): B ties every plug-in change to a commit
  in the core repo and blurs who owns what runs; C is worth it only if a second deployment
  appears. The design recommends the plug-in repo building the image (option A); that choice, with
  where the repo lives and how a core change reaches production, is among the design's open
  decisions for Jeremy (section 10), not settled here.
- A framework with more extension points than the house needs, or plug-in-added tables, gallery
  pages or JavaScript, a marketplace, or a stable third-party guarantee: out of scope for v1.
- Fail silently when a plug-in breaks: a scanner or answer source quietly absent is worse than a
  server that does not start.

**Evidence:** the rulings of 4 Nov 2026 recorded as Jeremy's in section 2 of
`docs/agent/specs/2026-11-04-core-and-plugins-design.md` (house behaviour out of the core and
cleaned up before more features; the core works with no plug-ins; our plug-ins in a separate repo
installed at build time; single-reader; the cleanup first), and the design's section 10.6 (new
features wait for the cleanup; decision d_01M6K1QHEVKDT977KK25W648FC). The shape of the extension
points and the choices inside each slice are the maintainer's, as built and recorded in that
design's section 8 ledger. Section 10 questions 1 to 5 (repo name, which repo builds the image, how
a core change reaches production, first-install defaults, aliases for renamed words) are listed
there for Jeremy; where this entry names a default it is the design's recommendation, not his
answer.

## The old address keeps serving agents for good and forwards people to the new one (8 Nov 2026)

**Chosen:** after Ream moves to `ream.tenfourty.site`, `artifacts.tenfourty.site` keeps answering the
agent API itself, for good, and forwards everything a person opens (the gallery, pages, links in
Slack) to the same path on the new address. Agents move to the new address in their own time. This
narrows the 7 Nov entry's "the old address forwards to it for good", because the installed agent
tools refuse forwards on purpose: a forward on the API would stop every agent still pointed at the
old address.

The maintainer's own calls within this: browser paths forward with a 308, built only from the
configured origin; `/healthz` is served like the API; the defaults and docs name the new address only
once it is proven live.

The owner's own call, 8 Nov 2026: the old address keeps its sign-in at the ingress. A signed-out person
logs in once and is then forwarded; agents are unaffected, because the API already passes the sign-in.
The gallery has no login of its own, so this way a bug in the forwarding cannot expose it on a host the
internet can reach.

**Not chosen:**
- Forward everything, the API included, and update every agent's tools first: any agent not updated
  stops working until it is.
- Serve both addresses fully for good, with no forwarding: old links keep showing the old name in the
  browser, and the gallery assumes one origin throughout.
- Remove the old address's sign-in so a signed-out person is forwarded at once: it saves one login, and
  leaves the app's forwarding as the only guard on that address.

**Evidence:** the behaviour of the old address: confirmed (Jeremy, 8 Nov 2026). The maintainer's own
calls: unconfirmed.

## Commenting on a page: compare three designs on a prototype before changing it (8 Nov 2026)

**Chosen:** the owner compares three designs on a clickable prototype and then picks one; the live pages
do not change until he has. The aim is a clean page with no text button on it and no hidden knowledge
needed to comment, replacing the corner Comment button chosen on 5 Nov.

**Not chosen:** building the hybrid or the quiet design straight away, without seeing them side by side.

## The Ream logo: the R whose top sheet lifts off the pile (8 Nov 2026)

**Chosen:** the logo is the fourth-round mark in its "rooted" variant: an R whose stem is a stack of
sheets, the top sheet lifting away in the accent colour. It goes in the gallery's bar and browser tab
now. The docs and the repository take it when they take the name. The drawing rounds are over and the
mark is not to be redrawn.

The maintainer's own calls within this: the drawings live in `src/ream/brand/` (large and small; light,
dark and one-colour), where the docs and the repository can use them later and the running service reads
them. The bar shows the large drawing beside the title on the wall, and elsewhere the small drawing
replaces the house icon as the way back to the wall. The tab icon is one small-drawing SVG at
`/favicon.svg` that follows the reader's light or dark setting. It is the gallery's own chrome: pages on
the content origin never get it, and the old address forwards it like any other browser path.

**Not chosen:** the four other fourth-round variants (the sheet over a sliced stem, the stem as its own
stack, the sheet curling up harder, four sheets with wider variation); further rounds of drawing.

**Evidence:** the mark, where it goes now, the docs and repository later, and no further rounds:
confirmed (decision d_01M6XGT9CRKX07VKMBHN166K28, 8 Nov 2026). The maintainer's own calls: unconfirmed.
