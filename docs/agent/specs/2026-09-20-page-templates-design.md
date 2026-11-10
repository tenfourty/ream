# Page templates agents build from — design

*20 September 2026. Tracks `homelab-ij8.69`, with children `ij8.69.1`
(intent/design templates) and `ij8.69.2` (metrics from beads). Closes
`ij8.38` as a side effect.*

## Intent

Jeremy, after looking at the artifacts status page:

> *"Makes me think we might need some 'templates' for agents to build off to
> get best practices — like a project status template and maybe some others"*
> … *"the last repo html-effectiveness seems like a good basis for many of the
> templates you could create."*

and, while this was being shaped:

> *"we also need artifact templates for spec/design stages of superpowers (read
> what the cove repo has for what we capture — especially as in it's
> feature-flow skill we have intent/design/spec in phases and some of the Q&A or
> options can be incorporated in there)"*

> *"agents here could have velocity and other stuff because we track our work in
> beads and we could calculate that here and have metrics etc."*

**Goal.** An agent that needs to publish a page never starts from a blank file
or from `template.html`'s worked-example-of-nothing. It fetches the template for
the job it is doing, replaces prose with its own, and publishes a page that
already obeys every constraint this service imposes.

**Non-goals.** Not a rewrite of `SKILL.md` — that is `ij8.73`, and it waits
until these exist. Not a template engine in the service: templates are static
bytes, served as they are. Not a component library; a template is a page, not a
kit.

**Success.** Eight templates exist. An agent can name the job it is doing and be
told which one to start from. Every one of them passes the service's own CSP
check with zero warnings. The artifacts arc's own status page is rendered from
the project-status template rather than from a hand-written file.

## The eight

Two families. The first six come from `anthropics/html-effectiveness`, which is
twenty dependency-free single-file HTML pages — the same constraint this service
already imposes, which is why Jeremy singled it out. The last two come from
the cove project's feature-flow skill.

**What an agent publishes about work:**

| Template | Use when | Shape borrowed from |
|---|---|---|
| `project-status` | Where an arc stands. | `11-status-report.html` |
| `decision-request` | The page exists to ask him something. | ours |
| `incident` | Something broke. | `12-incident-report.html` |
| `implementation-plan` | Phases by outcome, what blocks what. | `16-implementation-plan.html` |
| `comparison` | Two or three approaches, before a decision is asked. | `01-exploration-code-approaches.html` |
| `explainer` | How a piece of the house works. | `15-research-concept-explainer.html` |

**The stages of the work itself:**

| Template | Use when | Shape borrowed from |
|---|---|---|
| `intent` | A design is still soft and the questions are the page. | feature-flow Brainstorm |
| `design` | The design is settled enough to write down. | feature-flow Spec |

These four chain across a piece of work: **intent → design →
implementation-plan → project-status**. feature-flow's Plan phase is served by
`implementation-plan` and its Implement phase by `project-status`, which is why
there are two new templates and not five.

`intent` is the one that most directly serves the MVP brief — *"being able to
interact on decisions on a page and not just visualise progress"* — because it
is mostly questions. It carries several open decision slots at once rather than
one in a footer. The gallery already releases a slot's reserved height when its
decision is answered, so a page with five questions looks right at every stage.

What it renders, from feature-flow's brainstorm reference:

- the **refined-prompt blockquote**: the original request kept verbatim, one
  bullet appended per answered question, so intent crystallises visibly;
- goal / non-goals / success criteria / constraints;
- the **Q&A log** with stable ids that stay unambiguous across rounds, lettered
  options, a line of context per question saying why it matters, and an
  "other" escape;
- approaches with trade-offs and a stated recommendation, placed **above** the
  lettered questions whenever there is an architectural fork.

`design` renders the **deep-module sketch**: per module, its one-line role, its
interface shape, what it hides, its **deletion-test verdict** (does deleting it
concentrate complexity, or merely move it), and the proposed test surface — then
architecture, components, data flow, error handling, test strategy and risks.

## Where the bytes live

`src/artifacts/templates/<name>.html` is the single source of truth. Inside the
package, so the wheel carries them and the service can serve them.

Copies live in a skill directory in this repo and in the deployment's repo,
because Jeremy asked for both the skill directory and the API. They are written
by a sync script and guarded by a test. Eight files across two repos is a drift machine otherwise; the two skill
copies that exist today are byte-identical only by luck.

## The API

```
GET /api/v1/templates                    → index: name, title, use_when, bytes
GET /api/v1/templates/{name}             → the file
```

Key required, like everything else. The body comes back as
`text/plain; charset=utf-8` with `X-Content-Type-Options: nosniff`, so it can
never render as a page on the API origin — it is source, not a page, and the API
origin is the one origin in this service that must never render agent-adjacent
HTML. One line for an agent anywhere:

```bash
akey | curl -sS -H @- "$BASE/templates/project-status" -o page.html
```

Unknown name is 404, missing key is 401. Routes are additive and read-only.

## Keeping eight files looking like one page family

Each template is self-contained, so the token block is physically repeated eight
times. A build step would fix that and break the property that makes these
useful — that you can open one in a browser with nothing installed. So the
shared CSS sits between two sentinel comments:

```css
/* ==== shared: begin — edit in every template or the test fails ==== */
…
/* ==== shared: end ==== */
```

and a test asserts, in this order:

1. both markers are present in every template (a file that loses its markers
   fails loudly rather than passing on an empty region), and
2. the region between them is byte-identical across all eight.

Per-template CSS goes below the shared block, never inside it. Changing a colour
is a scripted edit across eight files and a passing test.

## Metrics, and the honesty rules that make them safe

Jeremy overruled the proposal to drop the reference report's metric tiles. He is
right: work here is tracked in beads, so the numbers exist and are auditable.
`bd list --json` carries `created_at`, `closed_at`, `status`, `priority` and
`parent`, which is everything the tiles need.

A helper takes a bead prefix and emits JSON; the template's tiles and velocity
bar are filled from it. The discipline is the same as `<!--artifacts:updated-->`:
**a number an agent types is a number an agent can get wrong**, and a wrong
number on a status page is the worst failure this service has, because the page
reads as current.

Three rules, enforced by the helper rather than by an author's care:

- **A tile with no source is not rendered.** Never zero-as-unknown.
- **Every tile says what it counted and over what window**, in the tile.
- **The velocity chart is not drawn until the arc is seven days old.** A
  two-bar chart implies a trend that is not there.

Measured today, the `ij8` arc is 2 days old (first bead 18 Sep 21:06), 77 beads,
14 closed, 23 P1 open. So the arc's own page shows tiles and **no chart** — which
is the rule working, not the rule failing, and the chart appears on its own once
there is a week behind it.

Chart specifics, from the dataviz method: one series, so one hue and no legend —
the title names it. Bars get 4px rounded data-ends anchored to the baseline and a
2px surface gap between neighbours. The hover layer is an SVG `<title>` child per
bar, which gives a native tooltip under a CSP that forbids everything else. Dark
mode is a selected step against the dark surface, not a flipped light one.

## Tests

`tests/test_templates.py`, every template through each:

- `csp.check(body)` returns **zero** warnings. This is the service's own verdict
  that a reader will not see the page broken, and it is the check that matters
  most, because an agent cannot look at its own page.
- `<!doctype html>` first; `<!--artifacts:updated-->` present exactly once; **no
  typed date anywhere** — the eyebrow and chips are checked against a
  date-shaped pattern.
- no `PAGE TITLE`, and no literal `Page id <code>ID</code>`. Both have shipped to
  Jeremy for real (`ij8.38`), and both are invisible to the agent that shipped
  them.
- the shared-block check above; the skill-directory copies byte-identical to
  canonical.
- the API: the index lists exactly the eight, each body route returns the bytes
  on disk, unknown name 404, no key 401.

## Order

1. `project-status.html`, and the arc's own status page re-rendered from it.
   Jeremy looks at both before the rest is built.
2. The other five of the first family.
3. `intent` and `design`.
4. The API routes and the metrics helper.
5. A picker table in `SKILL.md`: job → template. The full skill rewrite stays in
   `ij8.73`.

## Risk

A push here builds an image and deploys to production minutes later, unattended.
Jeremy has said explicitly (20 Sep) that he is content with that — *"I'm okay for
you to deploy as often and as frequently as you need/want"* — so it is no longer
a gate. Announcing a push to the operator session with its hashes still stands,
because that is about not throwing away a CI run someone else is watching, which
his permission does not touch.
