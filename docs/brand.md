# Ream's look: the logo and the gallery's design values

Ream (formerly artifacts) ships one standard look. This guide says how to use the logo and
lists the named values the gallery is drawn with. Why it is this way: `docs/decisions.md`,
"The Ream logo" and "A standard look first" (8 Nov 2026).

## The logo

The mark is an R whose stem is a stack of sheets, the top sheet lifting away in the accent
colour. It is finished: do not redraw it. The drawings in `src/ream/brand/` are the only
geometry; the gallery's bar and its tab icon are cut from them (`src/ream/logo.py`).

### The six versions

| File | Drawing | Colours | Use it on |
|---|---|---|---|
| `rooted-lg-lt.svg` | large | ink `#191a17`, lifting sheet `#0f6f63` | a light background |
| `rooted-lg-dk.svg` | large | ink `#e9eae4`, lifting sheet `#5ec9b8` | a dark background |
| `rooted-lg-mono.svg` | large | one colour, `#191a17` | where only one ink is possible |
| `rooted-sm-lt.svg` | small | as the large light version | a light background |
| `rooted-sm-dk.svg` | small | as the large dark version | a dark background |
| `rooted-sm-mono.svg` | small | as the large one-colour version | where only one ink is possible |

Each is a 512-unit square. The one-colour version may be filled with another single colour
(white on a photo, for instance), as long as every part takes the same colour.

### Sizes: which drawing, and how small

- **Small drawing:** drawn on a 16-unit grid, for 16 to 31 px. It is the browser-tab icon
  (`/favicon.svg`, 16 px) and the way back to the wall in the gallery's bar (about 20 px).
  Never smaller than 16 px.
- **Large drawing:** drawn on a 64-unit grid, with finer sheets; it needs about 32 px to
  read. Use it at 32 px and above: the wall shows it at 32 px beside its title. Never
  smaller than 32 px; below that, use the small drawing.
- The tab icon is one file that follows the reader's light or dark setting. Pages on the
  content origin never carry the mark.

### Clear space

The drawing fills its square almost to the edges: the lifting sheet reaches the top and the
letter's leg the bottom. Keep at least one eighth of the square's side clear on every side
of the square (4 px at 32 px), and never crop into the square.

### Don'ts

- Don't redraw, stretch, rotate or crop the mark, or set it in another letter.
- Don't recolour the two-colour versions, or swap the ink and the lifting sheet.
- Don't put the light version on a dark background, or the dark version on a light one.
- Don't scale the small drawing up past 31 px in place of the large one, or the large one
  below 32 px.
- Don't add shadows, outlines, gradients or a frame.

## The gallery's design values

Every colour, shadow, font stack, recurring type size and recurring spacing step the
gallery's stylesheet uses is named once in `src/ream/design.py`. The stylesheet starts from
the `:root` block that file produces, and a rule writes `var(--name)`, never the value.
Changing the look means changing that table; a test keeps it and this table equal, and
another refuses a raw colour or font stack in the gallery's rules.

- Colours and shadows have a light and a dark value; "same" means one value for both.
- The three sans-serif stacks are kept apart, not merged, so that every element renders in
  the face it did before the values were named.
- Type sizes are the reference's pixel steps. Sizes off that scale (`.7rem`, `.74rem`,
  `.8rem`, `.82rem`, `.85rem`, `.9rem`, `.92rem` and others) are left as literals in their
  rules: folding them onto the scale would change the look.
- Spacing names count tenths of a rem (`--space-35` is `.35rem`, `--space-10` is `1rem`):
  the margin, padding and gap steps used ten times or more. Other spacings, positions,
  radii and line heights are left as literals.

| Name | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#fcfcfb` | `#141619` | the page behind everything |
| `--ink` | `#0b0b0b` | `#e9eae4` | body text |
| `--muted` | `#898781` | `#989a90` | secondary text |
| `--card` | `#fff` | `#1c1f23` | a card, panel or dialog |
| `--well` | `#f5f5f4` | `#1c1f23` | a box set into the page |
| `--line` | `rgba(11,11,11,.1)` | `#2b2e34` | hairlines and borders |
| `--line-hover` | `rgba(11,11,11,.28)` | `#5a606a` | a border under the pointer |
| `--accent` | `#0f6f63` | `#5ec9b8` | the homelab teal: links, focus, fills |
| `--accent-soft` | `rgba(15,111,99,.10)` | `rgba(94,201,184,.12)` | an accent tint |
| `--accent-ink` | `#0b4f46` | `#9fe3d7` | text in the accent colour |
| `--on-accent` | `#fff` | `#0b2723` | text on a solid accent fill |
| `--on-chosen` | `#fff` | `#10201d` | text on a chosen answer |
| `--ok` | `#2f7a45` | `#6cc48a` | success |
| `--now` | `#b06a12` | `#e0a458` | waiting on the owner |
| `--now-solid` | `#9c5e10` | `#e0a458` | the waiting badge's fill |
| `--on-now` | `#fff` | `#2a1d0a` | text on the waiting badge |
| `--danger` | `#a8271b` | `#f08b7f` | destructive actions |
| `--danger-soft` | `rgba(168,39,27,.08)` | `rgba(240,139,127,.12)` | a danger tint |
| `--on-danger` | `#fff` | same | text on a solid danger fill |
| `--chip` | `#fcfcfb` | `#2b3038` | a control on a card |
| `--chip-hover` | `#fff` | `#343a44` | that control under the pointer |
| `--pin-fill` | `rgba(217,164,0,.12)` | same | the outline of a pinned spot |
| `--backdrop` | `rgba(11,11,11,.45)` | same | behind a dialog |
| `--connect-err` | `#b42318` | same | an error on the connect pages |
| `--connect-ok` | `#067647` | same | a success on the connect pages |
| `--mark-ink` | `#191a17` | `#e9eae4` | the logo's letter and sheets |
| `--mark-lift` | `#0f6f63` | `#5ec9b8` | the logo's lifting sheet |
| `--shadow` | `0 1px 2px rgba(20,20,15,.05), 0 8px 24px -16px rgba(20,20,15,.25)` | `0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8)` | a card at rest |
| `--shadow-menu` | `0 2px 6px rgba(20,20,15,.06),0 14px 34px -18px rgba(20,20,15,.45)` | same | an open menu |
| `--shadow-float` | `0 2px 8px rgba(0,0,0,.18)` | same | a button floating on a page |
| `--shadow-pin` | `0 1px 4px rgba(0,0,0,.3)` | same | a pin's numbered badge |
| `--shadow-chip` | `0 1px 3px rgba(20,20,15,.10)` | same | a tile's dots chip |
| `--shadow-chip-hover` | `0 2px 6px rgba(20,20,15,.16)` | same | that chip, raised |
| `--shadow-badge` | `0 1px 4px rgba(20,20,15,.28)` | same | a tile's waiting badge |
| `--shadow-dialog` | `0 12px 40px -12px rgba(11,11,11,.4)` | same | a dialog |
| `--shadow-toast` | `0 6px 24px rgba(0,0,0,.14)` | same | the newer-version notice |
| `--shadow-dock` | `0 -2px 10px rgba(20,20,15,.12)` | same | the bar docked at the foot |
| `--display` | `ui-serif,"Iowan Old Style",Georgia,"Times New Roman",serif` | same | the wall's title |
| `--sans` | `ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif` | same | body text, tile and page titles |
| `--sans-system` | `ui-sans-serif,system-ui,-apple-system,sans-serif` | same | the bar, badges, lists |
| `--sans-short` | `ui-sans-serif,system-ui,sans-serif` | same | comments, selection |
| `--mono` | `ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace` | same | code, ids, labels |
| `--mono-short` | `ui-monospace,monospace` | same | the connect code |
| `--text-2xs` | `.6875rem` | same | 11px: badges |
| `--text-xs` | `.75rem` | same | 12px: small print |
| `--text-sm` | `.8125rem` | same | 13px: secondary text |
| `--text-md` | `.875rem` | same | 14px: titles in the chrome |
| `--space-2` | `.2rem` | same | spacing |
| `--space-3` | `.3rem` | same | spacing |
| `--space-35` | `.35rem` | same | spacing |
| `--space-4` | `.4rem` | same | spacing |
| `--space-45` | `.45rem` | same | spacing |
| `--space-5` | `.5rem` | same | spacing |
| `--space-6` | `.6rem` | same | spacing |
| `--space-7` | `.7rem` | same | spacing |
| `--space-75` | `.75rem` | same | spacing |
| `--space-10` | `1rem` | same | spacing |
