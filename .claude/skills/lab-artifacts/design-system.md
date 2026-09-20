# Homelab artifacts — the shared look

Every page an agent publishes to the homelab artifacts service uses this
skeleton, so a reader recognises them as one family. Start from
`template.html`; change the content, not the token block.

## Hard constraints (the page CSP, not taste)

`default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline';
script-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'none'`

- No web fonts, no CDNs, no external images. A page is one self-contained
  file: inline `<style>`, inline SVG, `data:` URIs.
- No forms — `form-action 'none'`. A page reports; it does not collect.
- The page renders only inside the gallery frame, so it inherits the
  gallery's width; design for ~52rem and for a phone.

## Tokens

Declared once on `:root`, redefined under
`@media (prefers-color-scheme: dark)`. Never hard-code a colour in a rule.

| Token | Role |
|---|---|
| `--paper`, `--ink`, `--muted` | page background, body text, secondary text |
| `--display`, `--mono` | serif display face for the title, mono for the eyebrow and chips |
| `--rule` | the heavy line that closes the masthead |
| `--card`, `--line` | raised surface, hairline borders |
| `--accent`, `--accent-soft` | masthead rule, links, chip backgrounds |
| `--ok`, `--now`, `--wait` | a thing is done / in hand / not started |

Type: three faces, all of them the reader's own — no web font may load.
Body is the system sans at 16px/1.55; the title is the system serif
(`--display`), large and tight; the eyebrow, chips and code are the system
mono (`--mono`), uppercase and letterspaced for the eyebrow. One accent hue
per page — state colours are the only other hues, and they carry meaning.

## Shapes

- **Masthead** — a Gallery pill linking back to
  `https://artifacts.tenfourty.site/` **with `target="_blank"
  rel="noopener"`** — the page is framed, the gallery refuses to be framed,
  and the sandbox forbids top-level navigation, so a same-tab link is a dead
  link (found by Jeremy, 19 Sep 2026), then a mono eyebrow reading
  `KIND · DATE, TIME · HOST`, the serif title, one sentence of what the page
  is, a row of chips (state, when it was updated), and a heavy rule closing
  the block. Flat — no wash, the type carries it.
- **Card** — bordered block, `.card.live` adds an accent left edge for the
  thing that is currently true.
- **State table** — one row per item, a state chip in the first cell.
  Chips read `done` / `now` / `next` / `held`, never ids or phase names.
- **Decision block** — `.ask`, with lettered options `(a)`, `(b)`, the
  recommendation marked. Jeremy answers by letter.
- **Footer** — where the page came from and its id.

## Writing

Outcomes, never plan coordinates: "the page gate holds", not "GATE 4" or
"T2.1". Say what is true now, and date anything that will go stale.
