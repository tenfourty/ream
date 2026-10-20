# Feedback: publishing a page from markdown (20 Oct 2026)

Jeremy asked (20 Oct 2026, confirmed as option (a) of his decision) for an easy way for agents to
hand over markdown and get a nicely formatted page in the house look, converted by the service so
it works for any agent through the API as well as the CLI. Asked of every office, in parallel with
the build rather than gating it: (1) how you make a page's HTML today, and how often you would
rather write markdown; (2) which markdown features you need (tables, fenced code, task lists,
images, footnotes, Mermaid, raw HTML mixed in); (3) how to mark a publish as markdown (file
extension, a flag, or a content type on the API). Condensed from their replies, all nine offices.

| | Today | Share as markdown | Must have | Nice / not needed | Marking |
|---|---|---|---|---|---|
| gg-ops | hand HTML, runbooks and incident write-ups, a few a week | almost all | tables, fenced code | task lists, occasional Mermaid; no raw HTML | `.md`; content type for scripts |
| harland | hand HTML, rarely | most (reports, handoffs, decision memos) | tables, fenced code, task lists | Mermaid nice; no images, footnotes, raw HTML | `.md` + content type; no flag |
| quarterdeck | its board is generated | weekly write-ups | tables, fenced code, links | images now and then; no Mermaid, raw HTML | `.md` + content type |
| retrobrew | its board is generated | hardly ever | tables, fenced code, task lists | none of the rest | `.md` + content type; "a flag is one more thing to forget" |
| ch | hand HTML with its own CSS and dark mode; some script-generated | about 70% | tables (heavily), fenced code with language, linkable heading anchors, inline raw HTML (SVG, `<details>`) | Mermaid would replace its hand-drawn SVG; attachments with sha256; a title option, not only the first heading | `.md` + content type |
| cove | hand HTML from the skill template | about two thirds (status, research) | tables, fenced code, task lists, raw HTML (the decision slot) | Mermaid nice | `.md` + content type; flag redundant |
| jeremys-mac | hand HTML, often from a script rendering JSON to tables | about half (status notes, briefs) | tables, fenced code, links, headings | task lists nice; a little raw HTML (a status chip, `<details>`); no Mermaid | `.md` + content type for scripts posting a string |
| warpgate | one hand page kept current with `--section --prepend` | most new pages, if section updates still work | tables, fenced code, links | task lists nice; raw HTML only for `id=` anchors | `.md` + content type; no flag |
| runcove-pmo | hand briefs, decision pages, plans; board and roadmap generated | about 80% of hand pages | tables, fenced code, task lists, links | Mermaid nice for timelines | `.md` + content type; no flag |
| homelab | hand HTML from the template; boards generated | about two thirds (plans, incident notes, status) | tables, fenced code, task lists, heading anchors | Mermaid nice; raw HTML only for the decision slot, which must survive | `.md` by default; content type for stdin scripts; no flag |

## What it decided

- **Marking:** a `.md` file is markdown; on the API a `format: "markdown"` field. No flag: every
  office said one more option is one more thing to forget.
- **Features in the first cut:** tables, fenced code (language kept as a class), task lists,
  links, headings with stable ids to link to. Raw HTML passes through, so a decision slot, a
  `<details>` fold, an inline SVG or an `id=` anchor works on a markdown page (cove, homelab, ch,
  jeremys-mac, warpgate).
- **Section updates keep working** (warpgate): a markdown fragment is rendered and placed like an
  HTML one.
- **Title:** `--title` wins; without it, the first `#` heading (ch asked for the override).
- **Not in the first cut:** Mermaid (wanted by five as a nice-to-have; needs a script bundle served
  from the page's own origin, sized separately), attachments with checksums (ch; a separate item).
