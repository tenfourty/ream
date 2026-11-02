# Folder row on the wall: prior art (2 Nov 2026)

Tracker item runcove-5t3oj.61 (folders). Context: the owner found folders hard to see among page
tiles and chose folders first as their own row, compact cards with a folder icon, sorted by name,
each keeping its "N waiting" badge; loose pages below in today's order. He asked for prior art
before the build. Design it argues from: `docs/agent/specs/2026-11-02-folders-design.md`.

## Method and limits

Web search and fetch on 2 Nov 2026. Vendor help pages say little about pixel detail, and several
products' current grid cannot be reached without a login, so **what follows separates what a page
says from what is inferred**. Nothing here was tested in a live app. Where a source did not say,
the table says "not stated".

## What each product does

| Product | Separation | Folder vs file card | Order | Counts / activity | Phone |
|---|---|---|---|---|---|
| Google Drive | A sort choice, not a layout: **Folders "On top"** or "Mixed with files", saved per user ([help](https://support.google.com/drive/answer/2375177?hl=en)). The web homepage puts a **row of suggested folders above suggested files**: 6 shown, expandable to 12 ([9to5Google](https://9to5google.com/2024/09/05/google-drive-homepage-suggested)). | Grid view shows name and type only ([help](https://support.google.com/drive/answer/6034939)); folder cards are a different, shorter shape from file cards. A 2023 Android redesign tinted folder backgrounds and was criticised as busier ([Android Headlines](https://www.androidheadlines.com/2023/11/google-drive-new-odd-ui-update-android.html), not fetched: 403, from the search snippet only). | Sort key of the user's choice (Name, Modified), folders grouped first. | Not stated for folders. | Not stated; the Android app uses the same folders-then-files grouping. |
| Apple Files (iOS) | Sorted by **Kind** puts folders above other files; Name/Date/Size/Tags otherwise mix them ([Apple](https://support.apple.com/en-md/guide/iphone/iphe4bff8827/ios), [Cult of Mac](https://www.cultofmac.com/how-to/how-to-change-ios-files-sort-order)). | Icon view: same-size cells, folder glyph versus a document preview. | Alphabetical within a kind. | Not stated for a folder badge; no source found. | Icon grid of 3-4 columns, or list; no horizontal rows. |
| Dropbox | Mobile: "folders will always come first and the files second" ([forum, moderator](https://www.dropboxforum.com/discussions/101001013/file-sorting-in-ios-app-why-are-folders-first/651809)). Web: no separate section found. | Web grid: folder tile plus its type under it ([UMich notice](https://teamdynamix.umich.edu/TDClient/30/Portal/KB/PrintArticle?ID=11116)). | Folders first, then the chosen sort. Users who sort by date complain when folders stay first, so it is a rule people notice. | Not stated. | Folders first, in a list. |
| Figma | Folders (formerly projects) are the primary object, files inside them. Replaced projects with folders in 2026; users wrote the new grid was "a small, identical row, just an icon and a name" and lost file previews and counts ([Figma forum](https://forum.figma.com/share-your-feedback-26/figma-folders-57057/index1.html)). | Official reply (16 Sep): folder tiles again show previews of the files inside, with more distinct colours. | Not stated. | Users asked for **file counts** back and for emoji icons; colour alone was judged too weak, and an accessibility problem when tints are close (pink against purple). | Not stated. |
| Apple HIG | Says only: reflect how Finder and Files lay out the file system, use the default browser where you can, add filtering and recents ([HIG file management](https://developer.apple.com/design/human-interface-guidelines/file-management.md)). No size or section rule for folders. | - | - | - | - |
| Material 3 | No folder-versus-file rule found. Cards are "often used in lists or grids", 48dp minimum touch target, 8dp mobile margins (secondary summaries only, not m3.material.io). | - | - | - | - |
| Notion | Not researched beyond search; its sidebar nests pages rather than separating them. Not used as evidence. | - | - | - | - |

## What the evidence supports

1. **Folders first is the norm** (Drive's default sort, Dropbox, Files by Kind). Mixing is the
   exception and is offered as an opt-out. This agrees with the owner's choice.
2. **A folder is a shorter, shape-distinct object**, not a smaller page card. The one product that
   made folder tiles look like thin identical rows (Figma) was criticised for losing preview and
   count; the one that tinted folders (Drive Android) was criticised as noisy. Distinct shape plus
   a **count** and **one cue of what is inside** is the safe middle.
3. **Drive's homepage row is capped (6, expand to 12).** A folder row that grows without bound
   pushes the pages off the first screen; we should cap it on a phone.
4. **Colour must not carry meaning alone** (Figma's complaint). The waiting badge needs text
   ("2 waiting"), which ours already has.
5. **No source shows an unread or activity badge on a folder.** Ours is new ground; keep it to
   the one signal we already have, waiting, and do not add a second dot.

## Recommendations for artifacts

Today: `_folder_tile` (`src/artifacts/gallery.py` ~4588) renders a folder with the full page-card
frame (`.tile-wrap.folder > a > .frame/.shot.none/.body`), and the wall sort `rank` (~5024) puts
folders among pages by `(pinned, waiting, unread, newest)`. The `.tiles` grid is at ~828.

1. **Split the list, do not re-rank it.** Render folders as their own `<ul class="folders">`
   before `.tiles`, sorted by name (case-insensitive, then id). Leave `rank` for pages only; the
   folder branch of `rank` can go once loose pages and folders no longer share a sort. Inside a
   folder view there are no folder items, so nothing changes there. Archived tab: same split.
2. **Compact folder card: a one-line pill-card, 3.25rem tall, min 13rem wide.** Layout: folder
   glyph (1.25rem, `--accent` stroke, `--accent-soft` 2.25rem rounded square behind it), then the
   title (one line, ellipsis, `--ink`, 600), then a muted second line "N pages" in `--muted`.
   Background `--card`, 1px `--line`, radius matching `.tiles > li > a`, hover `--line-hover`.
   This is about a quarter of a page card's height, so shape alone says "container". The existing
   `_folder_row` (list view) already uses the same glyph; reuse its icon call.
3. **Phone: wrap into a two-column grid, do not scroll sideways.** Use
   `display:grid; grid-template-columns:repeat(auto-fill,minmax(min(13rem,100%),1fr)); gap:.6rem`
   which gives two columns at 390px and one on very narrow screens. Reasons: a horizontal row hides
   folders off screen, which is exactly the "hard to see" the owner reported; Files, Dropbox and
   Drive's own grid all wrap. Cap the **first paint** at six folders on a phone (Drive's number)
   with a "Show all N folders" control when there are more; on a wide screen show all, since they
   are one slim line each.
4. **Section labels, quiet, only when both sections exist.** "Folders" above the row and "Pages"
   above the tiles, as the small uppercase mono label the gallery already uses (`.htag`-style:
   .7rem, 600, letter-spacing .06em, `--muted`). If there are no folders, no labels at all, so a
   wall without folders looks exactly as today. In search results, skip the split (folders match
   by title only; keep them first, unlabelled).
5. **Badge: "N waiting" as a pill at the right edge of the card, text plus tint.** Reuse `.wait`;
   `--accent-soft` fill, `--accent-ink` text, so it passes in both modes (the dark block already
   lifts `--accent-ink` to #9fe3d7). Also give a waiting folder the 3px left edge the `.ask`
   block uses, so the cue is not colour alone. On a 13rem card, if title plus pill do not fit,
   the pill wins and the title truncates; the title is also in the `aria-label`. No separate
   unread dot (point 5 above); a folder's unread state stays in its sort position only if we ever
   re-add it, and for now name order is stable by design.

## Tokens, light and dark

Only existing tokens, so dark mode comes free from the `:root` and dark blocks (~140 and ~173):

- card surface `--card` (light #fff, dark #1c1f23); border `--line`; hover `--line-hover`
- glyph `--accent`, glyph backing `--accent-soft`; waiting pill `--accent-ink` on `--accent-soft`
- count text `--muted`; label text `--muted`
- selected / archived skins: reuse the `.archived` dimming already on `.tile-wrap`

No new colours, and nothing sets a folder apart by hue (Figma's lesson).

## Open points for the owner

- Name order is case-insensitive and ignores nothing else; "02 Roadmap" sorts before "Roadmap".
  Say if he wants a pin to lift a folder (a folder has no pin today, see `_folder_tile` docstring).
- Whether to cap at six on a phone, or always show all (recommended: cap, with a show-all control).
