# The claude.ai artifacts UI, as read off Jeremy's screenshots

*20 September 2026. He sent fifteen screenshots and said "lets try to replicate
all of this". This file is what I read off them.*

**Why this file exists.** The screenshots live only in one session's context and
are lost at a compaction; I have no way to write image bytes out to disk. So
everything worth knowing from them is written down here instead, in enough
detail to build from without seeing them again. If something below is vague, it
is vague because the screenshot was — say so and ask him rather than inventing
it. Tracked by `homelab-ij8.84`.

---

## The page view

A **compact top bar**, roughly 40px tall, white, hairline bottom border, full
bleed across the window. Not the tall masthead this service has.

- **Left**: a small outline house icon, then the page title as plain text
  (~1rem, regular weight, dark), then a small chevron. The title and chevron
  together are one control.
- **Right**, in order: a round avatar with initials on a dark green fill; a
  speech-bubble icon with its own chevron beside it; a **Chat** button with a
  small orange asterisk glyph; a **Share** button with a padlock. The last two
  are outlined pills, not filled.

### The title dropdown

Opens below the title, left-aligned to it. White card, ~12px radius, soft
shadow, ~300px wide, generous row height.

```
Artifact by you
Updated 1d ago
------------------------------      <- hairline divider
[pencil]  Rename
[copy]    Duplicate
[share]   Share
[refresh] Refresh
[pin]     Pin
[grid]    All artifacts
------------------------------
[flag]    Report artifact
[trash]   Delete artifact           <- RED text and red icon
```

The two-line header is muted, smaller than the rows. Icons are line-drawn, all
the same size, left-aligned in a consistent gutter. Delete is the only coloured
row and sits last, below its own divider.

### The comments panel

Opens from the speech-bubble icon, top right, as a floating card.

- Header: "All comments", a filter/sliders icon, an X.
- Empty state, centred: "No comments yet" then, smaller and muted, "Press the
  comment button, then click anywhere on the artifact to add one."
- The filter icon opens: "This page" / "All pages" (with a tick on the active
  one) / a "Show resolved" checkbox.

The chevron beside the speech bubble opens a single item: "Show all comments".

### The share panel

Opens from Share, top right, floating card, ~420px.

- Title = the page name, with an X.
- "Invite people by email" — a full-width input.
- "Who has access" — a muted label.
- A select-looking control: padlock icon, "Only you", chevron. Opening it gives
  "Only people invited" (ticked) and "Anyone with the link" with the sub-line
  "People with the link can view, but not edit."
- A person row: avatar, "Jeremy Brown (you)", the email beneath in muted grey,
  and "Author" with a chevron on the right.
- Footer row: "Only you can open this link" on the left; a gear icon and a
  "Copy link" button on the right.

---

## The gallery

Large serif **"Artifacts"** heading, well above everything else.

**Tab row**: `All` / `Yours` / `Shared with you`. The active one is a filled
grey pill; the others are plain text.

**Right of the tab row**: a search (magnifier) icon, a **view-toggle icon**
(shows a list glyph when in grid mode and a grid glyph when in list mode), and
an "All types" pill with a chevron.

**Type filter**, when opened:

```
[grid] All types        (ticked)
[icon] Slides                    0
[icon] Design                    0
[icon] Design System             0
[icon] Other                    14
```

Counts are right-aligned and zero counts are still listed.

**"Make something new"** — a muted label above three large type cards (Docs,
Slides, Design), each a soft gradient thumbnail with the name and a "Beta" chip
beneath. *Not applicable here; we have one kind of thing.*

### Grid view

Three columns, generous gutters, cards of equal height.

A **card**:
- ~14px radius, hairline border, very soft shadow, page-coloured background.
- The top ~60% is a **live preview of the page**, bleeding to the card's left,
  right and top edges and sharing its top corner radius. The page's own
  background colour shows. Text in the preview is small but the headline is
  legible — it reads as a shrunken page, not a blur.
- A **coloured rounded-square icon badge** (~34px, ~10px radius, pale tint with
  a darker glyph — purple, orange, red seen) sits at the **bottom-left of the
  preview, overlapping the boundary** into the footer.
- Below, on the card's plain background: the **title** (~1.05rem, medium
  weight), then a meta row: a small padlock, a middot, "Edited yesterday", and —
  when pinned — a **pin glyph at the end of that row**.
- A **vertical-dots (⋮) button** at the **top-right of the preview**, in a pale
  rounded chip. It is visible without hovering.

### List view

Rows **grouped by date**, with a muted group heading above each group: `Today`,
`Yesterday`, `Sep 18`, `September`, `August`.

Each row: the coloured type badge, the title, and on the right a padlock, a
middot, "Viewed 2m ago" / "Edited 2d ago", then the **⋮ menu**.

### The ⋮ menu (same in both views)

```
[pin]   Pin
[pencil] Rename
[copy]  Duplicate
[link]  Copy link
[trash] Delete          <- RED
```

White card, ~12px radius, shadow, icons in a consistent left gutter. Delete is
the only coloured row. Note this is a SHORTER list than the title dropdown on a
page — no Share, Refresh or All artifacts.

### The rename dialog

A **modal**, centred, with the page behind it dimmed and blurred.

- "Rename artifact" — semibold, ~1.4rem.
- A full-width input containing the current name, **pre-selected**, with a blue
  focus ring.
- Bottom-right: `Cancel` (outlined) and `Save` (filled grey).

### Empty state

"No shared artifacts yet" as a heading, then "When someone shares an artifact
with you, it appears here." — plain, left-aligned, no illustration.

---

## What NOT to copy, and why

- **"Yours" / "Shared with you"** — every page here is his to read, and the
  publishing seat is already on the tile. A tab that means nothing in this house
  should not exist just because the reference has one.
- **Docs / Slides / Design "make something new" cards** — agents publish here;
  he does not author. Types themselves are deferred (`homelab-ij8.83`).
- **Chat** — no equivalent.
- **Report artifact** — no equivalent; he owns everything here.
- **Delete** — he has an open decision about what deleting should do
  (`mwfspjce6vgtbsifil5kngpglx`). Do not add the row until it is answered. A
  menu item that destroys his answers must not be built on a guess.
- **Share / invite by email** — sharing beyond his own login is a much larger
  question and is not part of this arc.

## The caution worth keeping

The reference is a product with a design team; this is not. Copy the SHAPE — a
compact bar, actions on the title, dots per tile, a view toggle, date grouping —
not every affordance. Where the reference does something this service has no
equivalent of, leave it out rather than invent a reason for it.
