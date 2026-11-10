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

## A standard look first: a logo guide and the gallery's design values in one place (8 Nov 2026)

**Chosen:** Ream ships one standard look that everything shares. Letting each install have a
look of its own comes later, as its own piece of work. For now the minimum: a short guide to
the logo (`docs/brand.md`), and the gallery's colours, type and spacing as one set of named
values in one place (`src/ream/design.py`), with the gallery looking exactly as it did.

The maintainer's own calls within this: the values are a table in `design.py` that produces
the stylesheet's `:root` block, and every rule names a value rather than writing it. Only
values that recur exactly are named; off-scale sizes and one-off spacings stay literals,
because folding them onto a scale would change the look. The three sans-serif stacks stay
three names rather than one. The logo's two colours are named values too, and the tab icon,
which stands alone, has them written in from the same table. The rendered gallery was
checked unchanged: every element's computed style, light and dark, on the wall, the list,
a page with its comments open and the connect page, and the screenshots pixel for pixel.

**Not chosen:** a full design system or theming now; merging near-equal values into a tidy
scale, which would have changed the look.

**Evidence:** a standard look first, with the logo guide and the named values as the minimum,
look unchanged, and per-install looks later as their own work: confirmed (the owner, 8 Nov
2026). The maintainer's own calls: unconfirmed.

## Ream for GitGuardian: the eight design questions answered, and MCP sign-in for people's agents (8 Nov 2026)

**Chosen:** the company deployment (Ream installed for the people inside GitGuardian) works as
follows.

1. A person's own agent can read everything its person can, so it can summarise the pages shared
   with them.
2. Sharing outside GitGuardian is off at launch: company people only. It is looked at again once
   the service has run for a while.
3. A question an agent asks on a page can be answered by the person it is addressed to and by the
   page's owners.
4. No house office ever moves onto the company deployment, gg-ops included (9 Nov 2026, replacing
   "not until it has run on its own for a month"): he and every house office stay on the homelab's
   instance; the company's is for GitGuardian people and their agents. Both run the same Ream
   version, deployed separately and configured differently.
5. The core ships generic live-data sources set up by configuration (any JSON API, metrics queries,
   SQL). A source that needs more, such as Snowflake's login, is a plug-in.
6. A live source always runs with the viewer's own login. A source with no per-person login is not
   offered.
7. Teams come from the groups the company login sends at each sign-in, and a session lasts at most
   12 hours, so leaving a team takes effect within a day.
8. The company deployment keeps its data in Supabase's Postgres and its page bodies in Supabase's
   storage. Sign-in stays the company login.
9. Most people at GitGuardian use Claude.ai, so the basic case is a person's own agent there. Ream
   offers itself as an MCP server with an OAuth sign-in: a person connects Claude.ai (or another
   agent) once, signs in, and from then on that agent acts for them with no key to paste. The agent
   field names the client, for example "Alice, via Claude.ai". Keys stay for machines, offices and
   CI, which have no person behind them. The MCP sign-in goes into the core, so the homelab gets it
   first, behind the owner's own login, and he tries it from Claude.ai before the company build
   starts. It moves to the front of the company build order.

**Not chosen:**
- A personal agent reads only the pages shared with the agent itself: a person would have to share
  every page a second time before their agent could summarise it.
- The homelab's secret links and email codes on the company deployment from the start: it opens
  company pages to people outside before the service has a track record.
- Anyone with editor access may answer: a question meant for one person could be answered by
  someone else on the page.
- gg-ops moves first, as soon as the company deployment runs (the recommendation): the deployment
  proves itself on its own first.
- Configuration only for live sources: Snowflake's login cannot be written as configuration. A code
  plug-in for every source: each new source would need a code change.
- The viewer's login where the source supports it, and a read-only service account where it does
  not (the recommendation): a shared account can show a viewer data their own access would not.
  Service accounts for the tracker and forge, as first written: the same objection, for every
  source.
- The login's groups plus a nightly directory check, or a nightly directory sync alone: more moving
  parts, for what the 12-hour session cap already gives.
- Supabase's Postgres with page bodies in S3: two providers where one serves. Supabase for sign-in
  as well: the company login is already where people and their groups are.
- MCP with OAuth for everything, keys retired: machines, offices and CI have no person to sign in.
  The MCP server last, after the company deployment, as first written: it is the case most people at
  GitGuardian meet first.

**Evidence:** questions 1 to 8; the MCP sign-in, with keys kept beside it, built into the core and
tried on the homelab first, and first in the build order: confirmed (Jeremy, 8 Nov 2026; the MCP
sign-in comes from his comment on the design page). Answer 4 as now written: confirmed (Jeremy,
9 Nov 2026: the house offices stay on the homelab's instance for good and the design drops its
"move" step).

## Ream in Claude.ai: the six sign-in choices (8 Nov 2026)

**Chosen:** how a person's Claude signs in to Ream and what it may then do
(`docs/agent/specs/2026-11-08-mcp-oauth-design.md`).

1. When his Claude acts as him, it may pass on his answer to any question put to him, not only its
   own: a relay, never a tap, labelled with the app ("Jeremy said (b) in claude.ai"), with his words.
2. It reads everything he can read, changes only pages made under his sign-in (by any of his
   apps), and comments and asks anywhere he could.
3. The consent screen offers three plain permissions, each ticked and each untickable: read;
   publish, comment and ask; pass on answers. It names the app by its checked address and where it
   sends him back, and is remembered per app until he revokes it.
4. On the homelab he signs in through Authelia's standard OIDC sign-in, as the forge does: the
   path the company sign-on will take.
5. Each connection is a row in "Machines and keys" with Revoke; tokens last an hour and renew
   silently; a connection unused for 30 days lapses.
6. Any MCP app may ask, shown by its checked address and approved on the consent screen; a
   deployment may set a list of allowed apps (the company starts with Claude.ai only).

The maintainer's own calls within this: Ream is its own OAuth authorization server towards the
apps and hands the login to the OIDC provider (as Cloudflare's library and cove do); it follows
cove's MCP sign-in wherever the six answers allow: `/mcp` and cove's `/oauth/*` and `/.well-known`
paths, stateless Streamable HTTP, the `Origin` check, opaque hashed tokens with rotation and reuse
detection, a 90-day cap beside the 30-day lapse, client metadata documents first and open
registration for loopback apps only, keys refused at `/mcp` while sign-in is on, cove's setting
names. It also speaks MCP's 2026-07-28 revision, keeps sign-in state in the database, and gives
each person-and-app pair its own agent. Where it differs from cove and why: the spec's section 14.

**Not chosen:** the app answering as him, counted as a tap; listing questions only, answering
staying a tap. Reach limited to what one connection made; reach over everything. One
all-or-nothing permission; read-only first with a second ask. Signing in through the gallery's
existing gate only. A 7-day lapse; 90-day tokens with no renewal. Only listed apps, the homelab
included; any app named by what it says it is. The identity provider as the authorization server
(few company sign-ons accept Claude's client metadata, and Ream could not list or cut connections).

**Evidence:** the six answers: confirmed (Jeremy, 8 Nov 2026, each the recommended option on the
design page). Following cove's choices wherever the answers allow: Jeremy's instruction, 8 Nov
2026. The 90-day cap: confirmed (Jeremy, 9 Nov 2026: follow cove, a connection signs in again
every 90 days as well as lapsing after 30 days unused). Asking: confirmed (Jeremy, 9 Nov 2026):
his signed-in Claude may put a question to him on any page he can read, the question showing it
came from his Claude, not the page's agent (Ream records who asked; build step 8). Not chosen:
asking only on pages it published itself. The maintainer's other calls: unconfirmed.

**Added 9 Nov 2026, the company deployment:** a long Claude.ai connection follows team changes by
Ream re-reading the person's groups from the company login at every hourly renewal (it keeps the
login's refresh token for this); if the login refuses, the connection ends. Not chosen: a short
absolute lifetime, which would mean signing in again in Claude.ai every day. Evidence: confirmed
(Jeremy, 9 Nov 2026, the recommended option).

## Commenting on a page: one Comment control, a lit mode and a pill (9 Nov 2026)

**Chosen:** the reader gets one Comment control, with a count, that replaces the corner button,
"Comment on page" and the bubble. Pressing it lights a mode that stays lit with no band: a chart,
table (by the cell) or picture gets a box when pointed at and a click starts a comment, while links
still work; pressing it again, or Escape, turns the mode off. Selected words get a dark pill
offering Comment only. An "Offer comments when I select text" switch, on by default and kept in the
browser, turns the pill off. A "How to comment" box of numbered steps sits at the top of the
comments panel whenever Comment is lit; its steps change with the switch and say "Tap" on touch.
The touch layout follows the main pointer (`(pointer: coarse), (hover: none)`).

**Explain, Shorten and Fix** shipped later the same day as three new comment kinds (`explain`,
`shorten`, `fix`), on the pill beside Comment, after the offices reviewed the contract (page
`25yo76njdcwxwqripobxuir7fi`). A bare tap stores `Explain these words.` (and so on), with his note
after an em dash; agents act on the kind, never the body; a reply is always owed and an agent may
not settle one before replying; a declined Fix stays open for him.
**Remove and Looks good** are retired from the reader's screen; the API still accepts them and
existing threads of those kinds still display.

**Back and Forward on the comment card** step through the open comments in page order, skipping
settled ones; the arrows grey out at the ends (Jeremy, 9 Nov 2026). The card shows "N of M" between
two arrow buttons beside its kind label. A step closes the card, scrolls the page to the next
comment and opens its card. Page order is the frame's: the shim reports the placed marks top to
bottom (`order` on `artifact-marks-placed`), and comments with no anchor are not in the sequence.
Navigation only, so there is no agent route.

**The comments panel lists the Open threads in page order** (Jeremy, 9 Nov 2026): comments on the
whole page first, then the anchored ones top to bottom as the frame's `order` gives them, then those
whose passage the page could not place, in the order they were made. The gallery reorders the list
on each `artifact-marks-placed` and after a live swap; the server still renders oldest first. The
Resolved list stays oldest first (the frame is only told the open marks). With no `order` from the
page the card's arrows step through the panel's own open comments rather than not showing.
A comment whose passage a republish removed has no mark: it cannot scroll the page, open a card or
join the sequence, and the panel says "Outdated" on it.

**The comment card over highlighted words does everything the sidebar does** (reply, resolve and
reopen, Back and Forward, delete his own comment); Open is removed; the sidebar is for scanning and
clicking a comment there jumps to its passage (Jeremy, 9 Nov 2026). The card shows the whole thread
with its kind label, a reply box that is always there (Enter sends, as in the panel), and Mark
settled or Reopen, using the panel's own routes (`/c/{id}`, `/c/{id}/resolve`, `/c/{id}/reopen`), so
there is no new route and the agent twins are the ones that already exist. After an action the
gallery asks `/live.js` for the panel at once (`ream-refresh`) and redraws the card from it, so the
card and the sidebar agree; a page with no live loop reloads, as the panel's own form does. Delete
is not in this slice: the sidebar has no delete today, so the card has none either. Pressing an
outdated comment in the sidebar (no mark to go to) says plainly under its quote that the words it
pointed at are no longer on the page.

**Not chosen:** touch decided by screen size only, or by either test. The how-to box shown only
until his first comment, or as a tip under the button. The select-text switch off by default.
Explain, Shorten and Fix as labelled ordinary comments, or as pre-filled comment text.

**Evidence:** confirmed (Jeremy, 9 Nov 2026).

## The tools check their version on every call and reinstall themselves (9 Nov 2026)

**Chosen:** every call from `ream` and the `artifacts-*` commands sends its client level
(`X-Ream-Client`, and `X-Artifacts-Client`). Every `/api/v1` response carries the oldest level the
service wants (`X-Ream-Client-Min`) and, only when the caller is older, `X-Ream-Client-Outdated:
true`; the call is answered as usual. `agent.json` declares the same under `client_level` where `REAM_CLIENT_INSTALL_SOURCE` is set.
**The server only signals.** It names no ref, version or source to install.
When a tool is told it is out of date it runs `uv tool install --force --refresh <source>@main`,
where the source is the tool's own and `main` is the reviewed, CI-gated branch, then repeats the same
command once (a call that already changed something is not repeated, so nothing posts twice).
Guards: no downgrade (it asks the installed copy for its level and, if that is lower than its own or
unreadable, refuses to re-exec, prints the command and carries on as the old tool); at most one
attempt an hour per machine (a stamp file in the state directory), so a server that always says "out
of date" cannot cause an install loop; an environment marker stops a second update in one command.
With uv missing or the install failing it prints one line with the exact command and carries on.
On by default; `REAM_NO_SELF_UPDATE=1` turns the install off and prints the line. Offices' Claude
sessions then move to MCP, while scripts keep the self-updating tool.

**The version identity is a client level**, an integer bumped by hand in `src/ream/clientlevel.py`
when a change needs new tools. The package version has been 0.1.0 since the first commit and the
module's install time says when, not what.

**The install source is fixed on the client and never taken from the server.** It is the tool's own
`REAM_INSTALL_SOURCE` (a `git+https://` address) or the first line of
`~/.config/ream/install-source`, written when the tool is installed; with neither, the `git`
requirement in uv's own `uv-receipt.toml` (in `sys.prefix`), ref dropped, only when it is a
`https` git address. A path install (a checkout) has none, so reinstalling once from the forge
address (`uv tool install --force git+<forge address>`) is what makes updates automatic; the
tool prints that command, with the server's `X-Ream-Install-Source` note if present, for a person
to run. A source the server advertises
(`REAM_CLIENT_INSTALL_SOURCE`) is information for a person; it is never installed from.

**A tool installed from the tools index updates from that same index (10 Nov 2026, runcove-5t3oj.106).**
The office VMs install with `uv tool install --force --refresh --no-index --find-links
<base>/api/v1/tools/ ream`, and uv's receipt for that holds no git address: the requirement is the
bare name `ream` and the index rides in `[tool.options]` as `no-index = true` and `find-links =
[...]` (read from a real install; checked on uv 0.13). Such a tool therefore had no source and never
updated. With no setting, no file and no git requirement, the receipt's index now counts as the
source when the requirement is the bare `ream` (no git, path, directory or url of its own),
`no-index` is true and every `find-links` entry is an `https` address (plain `http` only to
`localhost` or `127.0.0.1`; no login, query, fragment or space). The reinstall is `uv tool install
--force --refresh --no-index --find-links <each address> ream`: the same flags, no ref (the index
serves the wheel the deployed image built). **Never a bare package name:** an unrelated package
called `ream` is on public PyPI, so the name only goes to uv beside `--no-index`; a receipt with no
`no-index`, or with neither a git address nor an index, gets no source and prints the manual line,
and `tests/test_no_bare_install_hint.py` still refuses a bare install line anywhere in `src`. A
setting or file still comes first and a wrong one is still not replaced by a guess; a git
requirement still wins over index options. Only tools that already carry this change can use it, so
a tool installed before it is reinstalled by hand once (the onboarding line) and updates itself
after that.

**A tool installed with plug-in packages keeps them when it updates (10 Nov 2026, runcove-5t3oj.107).**
`uv tool install --with <plug-in source> --with-executables-from <plug-in package> <core source>`
leaves the plug-in only in uv's receipt, and the update command had no `--with`, so it came back as
core alone and lost the plug-in's verbs and console scripts. The receipt (read from real installs on
uv 0.13.5) lists the package first, then one `[tool].requirements` entry per `--with`: `{ name }` for
a name the index answers, `{ name, git = "<https address>?rev=<ref>" }`, `{ name, path }`,
`{ name, directory }` or `{ name, editable = "<dir>" }`; `--with-executables-from X` adds another
bare `{ name = "X" }` and puts X's console scripts in `entrypoints` with `from = "X"`. The update now
passes each extra back: `--with git+https://...[@ref]` (the recorded ref is kept, unlike the
package's, because the person chose it), `--with <name>[extras][specifier]` only when the receipt shows
`no-index` and `find-links` (the same index options go on the command), and
`--with-executables-from <name>` where the receipt shows its scripts were installed. **Never a bare
name without that index** (the public `ream` problem again). Anything else (a local path or editable
directory, whose contents may have changed since the install, so building it again would run code
nobody chose; a url, a marker, an `index` or `subdirectory` key, an `http` or login git address,
two sources for one package) means the tool does not update itself: it prints the manual line naming the plug-in,
and leaves out the server's note (a reinstall from it alone would drop the plug-ins). The setting
or file still chooses the package's source; `REAM_NO_SELF_UPDATE=1` still only prints the line,
now with the plug-in options in it.

**Unattended and private sources.** The install never waits on a prompt (`stdin` is /dev/null,
`GIT_TERMINAL_PROMPT=0`). When uv or git's output says it was refused for want of a login
("could not read Username", "Authentication failed", 401, "terminal prompts disabled"), the tool
says the source needs a login on this machine, names it and prints the by-hand command; the
captured output is matched, never printed. The once-an-hour stamp still applies to a failing
machine. `ream --version` / `ream version` print the package version, client level and, when
uv's receipt records one (`?rev=`), the commit; the `artifacts-*` commands do not take
`--version` (separate entry points).

**Not chosen:** the version check alone (a stale tool is told, and nobody acts until a call fails).
MCP only, with scripts reinstalled by hand (scripts are the part that goes stale unseen). A ref
named by the server (a spoofed or compromised server could then install any commit, an unreviewed
`ci/` branch included).

**Evidence:** Jeremy, 9 Nov 2026; the ref-from-the-server design was dropped after a security review
the same day.

## The Ream source is public on the forge (9 Nov 2026)

**Chosen:** `runcove/artifacts` is readable by anyone on the forge, read-only, so every agent's tool
can reinstall itself from `main` with no login, on any machine Jeremy gives a key to. Before the
switch, the whole history and every branch were scanned for secrets (only test fixtures and prose
matched), the repository had no issues or pull requests to carry embargoed detail, and CI logs mask
secrets. Security fixes keep their rule: no detail in commits or public text until the fix is out.

**Not chosen:** keep it private and have machines without a forge login reinstall by hand, or hand
offices a read-only credential. Keep it private and have the service publish each release as a
package to install from (a medium slice, and a second release path to keep honest).

**Evidence:** Jeremy, 9 Nov 2026. Anonymous reads also need the `runcove` organisation to be
visible, which homelab has put to him separately.

## Each page says whether commenting starts on or off (9 Nov 2026)

The agent that publishes a page chooses, per page, whether commenting starts **on** or **off**
(`comments: "on" | "off"`, `ream publish --comments on|off`). Off suits a page a script redraws or
a status board. Off means the ways in start off: selecting words shows no pill (the reader's
"Offer comments when I select text" switch starts off on that page) and nothing on the page
invites a comment. The Comment control is still there. If the reader presses it, or turns the
switch on, commenting is fully on for that page and works as on any page. The reader's choice is
remembered in the browser for that page only (`ream.comments.offer:<page id>`), and on that page it
wins over the agent's setting. The reader's global switch still applies to pages that are on. The
agent changes it later with `POST /pages/{id}/comments-setting`, `ream publish --update ID --comments`, or
the MCP `organise_page` actions `comments_on` and `comments_off`; Jeremy's gallery has no control
for it, since his own switch overrides it per page.

Stored as a nullable column (`artifact.comments_default`). **A NEW page published without the
setting will be refused**, in a second step: step 1 (9 Nov 2026) ships everything but the refusal,
so offices can reinstall their tools and add `--comments` first; a new page without it is accepted
and stored null, which reads as on. `clientlevel.COMMENTS_REQUIRED = True` is step 2: it turns the
refusal on in the server (422 on `/api/v1/pages` under both words, naming
`comments: "on" or "off"`, `ream publish --comments on|off`, and that an older tool needs
reinstalling), by the MCP `publish_page` tool (the same refusal) and by the CLI. An update of an
existing page does not need it. Rows published before this change stay null and behave as on.

**Not chosen:** off disabling commenting entirely. Only the pill off, with other invitations still
shown. The reader's switch overriding every page. The page's setting being fixed against the
reader's choice. Tools-only enforcement with a grace list for the server. No requirement.

**Evidence:** confirmed (Jeremy, 9 Nov 2026).

## Delete your own comment, with undo (9 Nov 2026)

The owner can delete his own comments, in the comment card over highlighted words and in the
sidebar panel, and an agent can delete its own over the API (`DELETE /api/v1/comments/{id}`,
`ream delete-comment`, the MCP `comment` tool). Everyone gets an undo for 10 minutes: the gallery
shows "Deleted · Undo" for about 15 seconds, and the API and tools restore (`POST
/api/v1/comments/{id}/restore`, `ream restore-comment`) until the window closes, after which it
answers 409. It is a soft delete: the row stays, and the page's owning agent hears of it as a
`comment_deleted` (and `comment_restored`) event. Only your own comment can be deleted; anyone
else's answers exactly as if it did not exist. A deleted first comment that others replied under
stays as a "Comment deleted" placeholder with no words, so the thread is not torn in two; a thread
with nothing live left disappears, and its mark leaves the page. Settling a thread is already the
archive, so there is no separate archive for comments.

**Not chosen:** "settling is enough". It lost because he wants to remove a comment made by
mistake, not only put it out of the way.

**Evidence:** Jeremy, 9 Nov 2026.

## Several machines behind one address can connect at once (9 Nov 2026)

Five office VMs share one NAT address, and the connect flow allowed only 3 waiting requests per
address, so two of them got `429 REFUSED` and had to wait for the others to expire. The
per-address cap is now 10 (`connect.MAX_PENDING_PER_ADDRESS`); the ceiling of 50 across all
addresses stays, and it is what bounds a flood. Every waiting request counts against the caps, and a
repeat request never expires or alters an earlier one: replacing by name would let any machine
behind the same address cancel another's waiting request just by asking for its name. Nothing else
changed: the request is still open and unauthenticated, every request still dies
after 15 minutes, and only the owner can approve, by typing the code.

**Not chosen:** replacing an earlier request of the same name. It lost because a neighbour behind
the same address could cancel it. Also not chosen: removing the per-address cap. It lost because one address could then fill the
global ceiling alone and lock every other machine out.

**Evidence:** tracker runcove-5t3oj.102, 9 Nov 2026.

## Moving an agent to a new machine is one approval (9 Nov 2026)

**Chosen:** when a connect request names an agent that already exists, the owner's approve page
offers two buttons. "Same agent, new machine" adds the new key to that agent, so its pages,
comments and decisions stay. "New agent" approves under a different name typed on the page, checked
as before (the agent-name pattern, the reserved owner id, an app's name, and a taken name refused
with a clear message). A free name behaves as it did. The agent's earlier keys are marked to retire
and stop working the first time the new key makes any authenticated call (`agent_key.retire_after`,
enforced in `keys.first_use`, one transaction, safe to repeat); until then both work, so the old
machine is not cut off before the new one is proven. Events: `agent_moved` when the new key is
issued and `agent_keys_retired` when the old ones stop (written on the agent's latest page, since an
event row belongs to a page). A machine's own request never chooses "same agent"; only the owner's tap
does.

The agent's own twin: `POST /api/v1/agents/me/move` (`ream connect --start-move`) from a working
key makes a request already approved for the calling key's own agent and returns a one-time move
token (15 minutes; a new ask cancels an unclaimed earlier one). The new machine runs
`ream connect --move-token-stdin` and collects a key for the same agent with no one to approve. The
token is the credential and is read from stdin, never an argument. There is no way to name another
agent. It is gated like `/keys/rotate` (a vaulted house key gets 409), only the calling key retires (the
owner's approval retires all), a request past its life is never collected, and revoking the key that
started a move cancels it.

**Why:** five offices moving onto VMs needed their old agents renamed by hand and the pages folded
over with `artifacts-admin rename-agent`, one database move each.

**Not chosen:** keeping the old keys until someone revokes them by hand. It lost because a
forgotten key on a machine that was retired stays valid for good. Refusing a taken name at connect time
was the other option. It lost because the machine would only be told no, and the move it was
trying to make would still need the hand rename.

**Evidence:** tracker runcove-5t3oj.102, 9 Nov 2026.

## Ream for the whole of GitGuardian: the design is approved (9 Nov 2026)

**Chosen:** the design on the page "Ream for the whole of GitGuardian"
(https://ream.tenfourty.site/a/pxaqprz3v6x2lmfhycwzsx3cjf; source kept with the office's pages) is
approved as written. Building starts after the core clean-up, which moves the household-specific parts
(the owner's name, Slack, mail, the secret scanner, the tracker pages) out of the core into plug-ins.
Then it goes in the page's order: MCP sign-in for people's Claude (already built on the homelab),
people, personal and shared agents, sharing, live data, and last the company deployment. Each step
leaves the homelab working as it does today. The agent-facing parts (keys, the "via" line, declared
queries, rows in) go back to the offices for review before they are built.

**Why:** the owner wants Ream used across the company, with many people, personal and shared agents,
sharing, and live Snowflake data, from one codebase and two separate deployments.

**Not chosen:** approving with changes, or not yet. The owner picked (a) with nothing added.

**Evidence:** confirmed (Jeremy, 9 Nov 2026), decision d_01M70JRY80X9C684D05WXD1GP3; tracker runcove-5t3oj.46.

## Our homelab's add-ons to Ream live in a private repo named ream-homelab (9 Nov 2026)

**Chosen:** the house-specific parts that leave the core (tracker pages, Mailgun, the secret scanner,
the doorbell, the quarterdeck sink and answer source) live in a new private forge repo,
`ream-homelab`, in the same organisation as this one. The homelab office creates it, and the artifacts
office can write to it. This answers question 10.1 of
`docs/agent/specs/2026-11-04-core-and-plugins-design.md`. The design named the repo
`artifacts-homelab`; it was asked as `ream-homelab` after the project's rename.

**Why:** this repo holds only the core, and our install's specifics need a home of their own that
another deployment never has to carry.

**Not chosen:** the same repo named `ream-plugins`, which reads as if it were for everyone's
plug-ins. A folder inside the homelab repo, which would tie Ream add-on changes to the homelab
repo's review and landing.

**Evidence:** confirmed (Jeremy, 9 Nov 2026), decision d_01M70N0880RG906YRFG5S27Y3H.

## The ream-homelab repo builds the image that runs in production (9 Nov 2026)

**Chosen:** this repo's CI publishes a plug-in-free core image and its wheel. `ream-homelab` builds
the deployed image `FROM` that core image (its digest kept in a `core.lock` file there), installs our
plug-ins, runs both suites, and opens the homelab pin PR. The production image keeps its present name.
The homelab pin lander learns to bind a digest to `ream-homelab`'s CI run instead of this repo's.
This answers question 10.2 of `docs/agent/specs/2026-11-04-core-and-plugins-design.md` (option A,
section 7.1).

**Why:** this repo stays only the core, and a change to one of our plug-ins goes straight to its own
repo without a commit here.

**Not chosen:** this repo building the image by cloning the add-on repo at a pinned version, which
ties every add-on change to a core commit and blurs who owns what runs. A third repo that only
assembles the image is worth it only if a second deployment of our own appears.

**Evidence:** confirmed (Jeremy, 9 Nov 2026), decision d_01M70N0BMCAY974EP7602BX092.

## A core change reaches production through a lock update that merges itself when the combined tests pass (9 Nov 2026)

**Chosen:** when this repo's `main` publishes a new core image, a bot opens a pull request on
`ream-homelab` that moves `core.lock` to it (the same technique as `pin-pr.sh`). That pull request
merges by itself once the combined suite (core plus our plug-ins) is green. `ream-homelab` then builds
the deployed image and opens the homelab pin PR, which the homelab maintainer merges as today. A red
combined suite leaves the update open for this office. This answers question 10.3 of
`docs/agent/specs/2026-11-04-core-and-plugins-design.md`.

**Why:** a core change should reach production as fast as it does today, with only the extra hop the
split needs, and no person in the loop when the tests already prove it.

**Not chosen:** this office merging each lock update by hand, which adds a wait with nothing to judge
when the tests are green. No automation at all, which would let production drift behind the core.

**Evidence:** confirmed (Jeremy, 9 Nov 2026), decision d_01M70N0KES88YJDQWHJGHCM679.

## The comments-setting refusal waits for the office-page scripts' fix (9 Nov 2026)

**Chosen:** Ream turns on the refusal of a new page that does not say whether comments start on or
off (`clientlevel.COMMENTS_REQUIRED`) only once quarterdeck's fix to runcove's office-page scripts,
which makes them pass `--comments off`, is on runcove's `main`. Until then new pages without the
setting are still accepted, so nothing that publishes today breaks. The other confirmations already
asked for (runcove-pmo, cove) still apply.

**Why:** those scripts are the last caller that does not send the setting, and their fix is ready
but held by runcove's open-pull-request limit.

**Not chosen:** turning the refusal on now, which would stop new office status pages until the fix
lands. Asking runcove-pmo to let the fix past runcove's limit first.

**Evidence:** confirmed (Jeremy, 9 Nov 2026), decision d_01M714F34A3AB7V449YNF4CYEA.

## Offices' Claude sessions sign in to Ream's MCP with their own agent keys (10 Nov 2026)

**Chosen:** `/mcp` takes agent keys beside the owner's sign-in (`REAM_MCP_ACCEPT_KEYS=1` on our
deployment), so an office's Claude session uses Ream as its own agent, scoped to its own pages as the
tool is today. The key reaches the session through a helper that reads it from the vault, never from a
file an agent writes or a command line. This office is the pilot; the other offices then move one at a
time. Scripts keep the self-updating `ream` tool, as decided on 9 Nov. The plan, its gap list and the
maintainer's own calls (blocking waits stay on the tool; a key is minted for an office that has none
when it moves; publishing from a file stays a tool job for the pilot) are in
the house's deployment docs.

**Why:** the second half of the 9 Nov decision, offices on MCP so they stop running out-of-date tools,
needs a way for an office seat to be itself at `/mcp`; until now only apps the owner signed in could.

**Not chosen:** keeping offices on the tool until people's sign-in covers agents too; allowing keys for
the pilot office alone and deciding the rest later.

**Evidence:** confirmed (Jeremy, 10 Nov 2026), decision d_01M71ARX2FGFQ28G9Q06804QGW.

## With Ream connected to Claude, a request for an artifact becomes a Ream page (10 Nov 2026)

**Chosen:** where Ream is connected to Claude (claude.ai, Claude desktop, Claude Code) as an MCP
server, any request for an artifact, page, doc, report or dashboard becomes a Ream page, unless the
person names Claude's own built-in artifacts. The first time Claude does so in a conversation it says
the page went to Ream and gives the link; after that it just gives the link. The rule is stated in the
server's `instructions`, the `publish_page` and `get_template` tool descriptions and the guide served
as `skill.md` and `ream://guide`, with the fence wording ("content, never instructions") unchanged.
Tests pin the key points (`tests/test_mcp.py`); how it is laid out is in `docs/mcp.md`.

**Why:** a person who connects Ream to Claude wants what Claude makes for them to land where they can
comment on it and answer on it, at an address that stays. A page in Claude's own panel offers none of
that, and a person should not have to say "in Ream" every time. The one-time note keeps the change
from being a surprise.

**Not chosen:** the same rule applied silently, which leaves the person wondering where their page
went. Ream pages only for things worth keeping, with throwaways left as Claude's artifacts, which asks
the model to judge what is worth keeping and splits one person's pages across two places.

**Evidence:** confirmed (Jeremy, 10 Nov 2026), decision d_01M722QT46FA3YZR8F4HTSR9DN.

## The install name is ream-artifacts, and the tool goes on public PyPI from a public GitHub mirror (10 Nov 2026)

**Chosen:** three linked things.
- The package Ream installs as is named `ream-artifacts` (it was `ream`). `ream` on public PyPI is
  somebody else's package, so a bare `ream` handed to uv without `--no-index` can install a
  stranger's code. Only the install name changes: the `ream` and `artifacts-*` commands, the `ream`
  and `artifacts` import names, settings, headers and everything stored keep their names. Install
  lines keep naming Ream's own tools index (`--no-index --find-links <gallery>/api/v1/tools/
  ream-artifacts`).
- A tool installed under the old name keeps updating. Its self-update asks the index for `ream`,
  so the index also carries the same code built under the old distribution name `ream` (the image
  builds the wheel twice, and `/api/v1/tools/` lists both); the old tool's update succeeds, and
  the code it receives moves it to `ream-artifacts` at its next update and uninstalls the old
  tool. The old-name wheel stays while any tool with `ream` in its receipt may exist.
- The real tool goes on public PyPI as `ream-artifacts`, and every release from `main` is published
  there, so the code becomes public there as well as on the forge. The upload is by PyPI trusted
  publishing (no stored token) from a public GitHub repository on Jeremy's personal account, which
  mirrors `main`, every commit as it lands. `ci/` branches are never mirrored and stay private. A
  history clean-up comes before the repository goes public. Nothing is wired to PyPI yet, and
  install lines keep naming Ream's own index until it is. Once every commit is public as it lands,
  the rule that security fixes carry no detail in commits or public text until the fix is out binds
  `main` too.

**Why:** the name `ream` was never ours on PyPI, and a package a person can install by name from
anywhere needs a name that is. Publishing the real tool, not a stub, is what lets an agent on a
machine without Ream's address install it with one line.

**Not chosen:**
- Keep `ream`: the stranger's package keeps the name on PyPI, so every install line needs
  `--no-index` for ever, and one missed flag installs somebody else's code.
- `ream-artefacts`: a second spelling beside the `artifacts` that the commands, the import name and
  the old package already use, so it would be mistyped and mis-searched.
- A placeholder on PyPI that only holds the name: it ships nothing, so install still needs Ream's
  own index, and the point of being installable by name anywhere stays unmet.
- A client-only package (the command-line tools without the server): a second package to build and
  release in step with this one, to keep private code that the public mirror shows anyway.
- A private GitHub repository for the mirror: Jeremy chose a public one, so the source and history
  can be read beside the package (after the history check).
- GitLab for the mirror: Jeremy chose GitHub.

**Evidence:** Jeremy, 10 Nov 2026, decisions d_01M722KZWW0Z97CWYWCZ0M9AQ5, d_01M722M71EX3DJ899RGE1WE02Z,
d_01M722PV8VRTT81Z8X6WWVJ8ZF, d_01M722PYG8K7JW92VDRHY65059, d_01M722RTPV2CCRZ60SGZDJBPY6, d_01M722W3X8CJFT9AC7W99YVA4M,
d_01M722W715J66S2MH7577BEEZT, d_01M72320PF4FM5ZGHWYR1HPYDY.

## Public docs teach the PyPI install line; our agents keep the deployment's own index (10 Nov 2026)

**Chosen:** `ream-artifacts` 0.1.0 is on PyPI, published from the public GitHub mirror by trusted
publishing, and a release is a version bump in `pyproject.toml`. The public docs (the README, which
is also the PyPI project page, and `docs/` as read on GitHub) teach `uv tool install ream-artifacts`
from PyPI. What the server serves to agents (agents.txt, skill.md, agent.json and the onboarding
text) keeps installing from the deployment's own tools index or source, so our agents keep tracking
`main` with no version bump for each change they need.

**Not chosen:** PyPI everywhere, agents included, with a version bump for every change agents need
(every agent-facing change would wait on a release). Our own index everywhere, with PyPI only for
outside readers who happen to find it (the public docs would teach a line that only works with our
address).

**Why:** a reader outside the deployment has no tools index, so PyPI is the one line that works for
them; our agents already have the index and gain nothing from waiting on releases. A tool's
self-update reinstalls from wherever it was installed, so a PyPI install updates from PyPI when a
version is released.

**Evidence:** confirmed (Jeremy, 10 Nov 2026), decision d_01M72A35206RZBQP0F7MS6ZN6N, answer (a).

## What each round changed, and which pages render broken (10 Nov 2026)

**Chosen:** four answers on the design for tracker item runcove-5t3oj.56, the page "What each round
changed, and pages that render broken".
- **Renders broken** means a script error, a blank page, or a picture or file the page points to that
  does not exist. A page wider than its window is told to the publishing agent only, not flagged on
  the wall.
- **A broken page** shows an amber tag on its gallery tile and a line at the top of the page saying
  what went wrong. It clears when the agent publishes a version that renders clean.
- **Opening a page that changed since he last read it** shows a short strip naming the changed
  parts, each jumping to and briefly outlining its part, plus a "Show marks" link for the words added
  and removed, on any page.
- **History:** each page has a History entry in its menu listing each version (when, who, note,
  changed parts); opening one shows that version with its changes marked.

**Not chosen:**
- Also flag pages wider than their window on the wall: some wide tables are intended, so it would
  flag pages that are fine.
- Flag script errors only: a blank page or a missing picture looks just as broken to him.
- Show a broken page only on the page itself, or tell only the agent: he would not see it on the wall.
- The strip alone, with words added and removed kept to review rounds, or nothing new outside review
  rounds: less than he asked for.
- No History for now, the strip being enough: it only covers what changed since his last visit.

**The strip can be switched off, both ways** (Jeremy, 10 Nov 2026, answer (c) on decision
d_01M72EA9194SDNW911E7KAH9RN). The reader has one switch for the whole wall, kept between visits.
Each page also has a way to hide its strip. That choice is remembered for that page, and a
Show changes link in the page's menu brings the strip back. A page's own choice wins over the wall
switch for that page, as with the commenting switches above. History and the broken-page warnings
are never switched off by either. The office's own readings, reversible: the wall switch starts on,
and hiding a page's strip lasts until he shows it again, not only until that page next changes.
Options that lost: (a) one wall switch only, with Show changes per page; (b) a per-page switch only.

**Evidence:** confirmed (Jeremy, 10 Nov 2026), answer (a) on questions 1, 3, 6 and 9 of the design
page tycqi4frhe2itzs3n2axy7bhxu.

## Revoking a key records who and why (10 Nov 2026, runcove-5t3oj.110)

Folding an agent revoked a seat key a timer still used, and the plain revoke left no reason and no
event, so nobody could say who had cut it. Now every revocation records `revoked_by` and
`revoked_reason` on the key row, and writes an `agent_key_revoked` event on the agent's latest live
page when it has one (an event belongs to a page; a routine rotation writes none); the refused
request's 401 shows the reason to whoever holds the key. `rename-agent --revoke-keys` lists each key with its
last use and refuses one used in the last 24 hours unless `--force`. A plain rename or merge still
moves keys and keeps them working.
