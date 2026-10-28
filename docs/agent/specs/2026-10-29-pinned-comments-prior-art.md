# Prior art: comments pinned to a spot (research, 29 Oct 2026)

Researched for runcove-5t3oj.53 at Jeremy's request, before his five gallery choices. Claims without a link are unverified.


Not verified (searches returned nothing authoritative): Google Docs' exact "original content deleted" wording, Notion's behaviour when anchored text is deleted, Word's official behaviour, Slides object-comment UI, Figma frame-pin and image-attach behaviour, Figma Comment object fields (order_id, resolved_at), Marker.io/BugHerd selector capture. The Hypothesis API create payload came from an empty fetch, so that row is indicative only.

## 1. Pinning to a non-text element
- Figma: `C` or toolbar enters comment mode; click drops a pin; objects cannot be edited in the mode; Esc exits. https://help.figma.com/hc/en-us/articles/360039825314-Guide-to-comments-in-Figma
- Vercel preview comments: `c` enables a placement cursor, or Comment in the toolbar; click places the pin; click-drag screenshots a region (extension only). https://vercel.com/docs/comments/using-comments
- Hypothesis: text-only (selection plus Annotate button).
- lavish-axi: element click stores a CSS locator; table cells also store visible row/column names, omitted if ambiguous ("only less descriptive, never mislabelled"); text selections store quote plus range anchors. https://github.com/kunchenguid/lavish-axi
- Standard: explicit comment mode, toolbar button plus single key, page interaction suppressed, Esc exits. Nobody uses click-anywhere.
- Recommendation: keep the "Pin" toggle, add the `C` shortcut (guard against inputs), highlight hovered element, Esc exits. Text selection stays a separate path needing no mode.

## 2. Pinned spot removed by a republish
- Hypothesis: annotation moves to "Unanchored" (formerly orphans); not deleted or hidden; original selected text stays on the card, crossed out. https://web.hypothes.is/help/what-are-unanchored-annotations/
- GitHub PRs: thread stays, labelled "Outdated"; not auto-collapsed since 2018; outdates only when the exact anchoring line changes. https://github.blog/changelog/2018-09-24-precision-review-comment-outdating/
- Confluence: auto-resolves with "text was deleted"; many false-positive bugs (reverts, table edits, cut/paste), sometimes not reopenable. https://jira.atlassian.com/browse/CONFCLOUD-79573 , https://support.atlassian.com/confluence/kb/keep-inline-comments-while-editing-confluence-pages/ . Silent auto-resolve is the anti-pattern.
- Word / Google Docs: weak sources say comments survive in the sidebar after the anchor is deleted (NROC support, Microsoft Q&A, WPS). Unverified.
- Fibery: users complained unlinked comments became hard to find. https://community.fibery.io/t/unlinked-comments-should-still-be-visible-and-or-interactable/11111
- Standard: never delete or auto-resolve; keep the comment, show original quote struck through or labelled, group unanchored/outdated separately.
- Recommendation: "both" is right. Never auto-resolve. Struck-through blockquote plus "on v3, since removed". Collapsed outdated group at the top of the panel.

## 3. Pinned text still exists but was edited
- Hypothesis (2013 post, may be dated): four strategies in order. (1) stored XPath range, verified against text; (2) position offsets, verified; (3) context-first fuzzy: find prefix near expected start, suffix near expected end, accept text between if difference under threshold; fails if prefix or suffix not found; (4) fuzzy on the quote alone. Uses diff-match-patch (Bitap); 32-char prefix/suffix. https://web.hypothes.is/blog/fuzzy-anchoring/
- GitHub: outdates on any change to the exact anchoring line; no fuzzy matching.
- Confluence: treats edits and moves as deletions (see 2).
- Standard: exact match first; fuzzy is the exception, only Hypothesis does it (it annotates pages it does not control).
- Recommendation: keep "edited in place" as a distinct third state ("text changed since you pinned this"), showing old quote and new nearby text. Fuzzy-match the quote within the nearest heading's section (diff-match-patch or Levenshtein ratio about 0.8+). Never silently re-attach a pin elsewhere; mark "approximate" and show both quotes.

## 4. Images on a comment
- lavish-axi: paste, drag-drop, file picker on composer or annotation card; PNG/JPEG/WebP; stored server-side; agent gets absolute path plus content-hash id, not bytes; per-image and per-prompt limits; an over-limit image rejects the whole batch.
- Vercel: plus icon upload; camera icon screenshot; click-drag region screenshot (last two need the extension).
- Standard: paste, drop and choose all supported, thumbnail preview before send.
- Recommendation: plan matches. Add all-or-nothing rejection with a clear message; content-hash dedupe; re-encode client-side (strips EXIF); give the agent a stable URL or path. I found no source for 5 MB / 4000 px limits at any product; they are our own choice.

## 5. Auto-screenshot of the pinned spot
- Vercel: manual only (region drag needs extension).
- BugHerd / Marker.io: auto-capture screenshot plus browser details (vendor comparison pages, caution). Bug-triage tools on live sites.
- lavish-axi: none found; DOM snapshot on Send.
- Hypothesis, Figma, Google, Notion: none verified.
- Standard: split. Bug-report tools capture automatically; document/annotation tools do not.
- Recommendation: skip for now. Cheaper substitutes: quote, nearest heading, element outerHTML snippet. Revisit if image/chart pins are unreadable once outdated.

## Agent-facing API comparison
| Product | Anchor read | Create anchored | Images | Learn orphaned/outdated |
|---|---|---|---|---|
| Google Drive (https://developers.google.com/workspace/drive/api/guides/manage-comments , https://developers.google.com/workspace/drive/api/reference/rest/v3/comments) | `anchor` is an app-defined JSON string Drive stores uninterpreted; `quotedFileContent` {mimeType, value} | Yes, but Docs/Sheets/Slides do not render API anchors (shown as unanchored in All Comments); anchor immutable, position across revisions not guaranteed | none found | No. `deleted`, `resolved` only |
| Notion (https://developers.notion.com/reference/create-a-comment) | `discussion_id`, `parent` (page/block), `original_content_deleted` | Only whole page/block (`parent.block_id`) or reply via `discussion_id`; cannot create text-range inline comments | up to 3 `attachments` by `file_upload_id` (upload first) | Yes: boolean `original_content_deleted` |
| Figma (https://developers.figma.com/docs/rest-api/comments-endpoints/) | `client_meta`, `parent_id` (others unverified) | Yes: POST /v1/files/:key/comments with `message`, optional `comment_id` (reply to root only), `client_meta` one of Vector, FrameOffset, Region, FrameOffsetRegion | not found | not found |
| Hypothesis (https://h.readthedocs.io/en/latest/api-reference/v1/) | `target[].selector[]` array of typed selectors (TextQuote exact/prefix/suffix, TextPosition, Range, possibly XPath/CSS/Fragment; unverified) | Yes: uri, text, target, document | not found | Client decides; no verified server orphan field |
| GitHub (https://docs.github.com/en/rest/pulls/comments) | `line`, `start_line`, `side`, `original_line`, `original_start_line`, `original_position`, `commit_id`, `original_commit_id`, `subject_type`; `position` deprecated | Yes: body, commit_id, path, line or position | not covered | REST: no documented flag. GraphQL `PullRequestReviewThread` has `isOutdated`, `isResolved`, `line`, `originalLine` (https://jsr.io/@luxass/github-schema/doc/~/PullRequestReviewThread); `line` reported null when moved (community) |

Nobody exposes a diff of old vs new anchor text or an "edited in place" signal.

Patterns to copy: Notion's plain boolean; GitHub's original/current pair; Hypothesis's typed selector array. Avoid Drive's opaque string.

## Recommended agent-facing shape
```json
{
  "id": "...", "thread_id": "...", "page_version_pinned": 3,
  "anchor": {
    "quote": {"exact": "...", "prefix": "32 chars", "suffix": "32 chars"},
    "heading": "Nearest heading text",
    "element": {"id": "fig-2", "css": "main > figure:nth-of-type(2)", "label": "alt/caption"},
    "table_cell": {"row": "Q3", "column": "Revenue"}
  },
  "anchor_state": "exact | moved | edited | outdated",
  "current": {"quote": "...", "heading": "..."},
  "attachments": [{"id": "sha256...", "url": "...", "mime": "image/png", "bytes": 123}]
}
```
- `anchor_state` is one explicit enum, computed on read, never stored. `edited` carries `current.quote`.
- Create takes the same `anchor` object; server resolves id, then quote under heading, then quote anywhere; response returns the resolved state so the agent learns at write time.
- Images: upload first, reference by id in `attachments` (Notion pattern); over-limit rejects the whole request.
- Every payload carries quote and heading.
- No server-side fuzzy match yet; if added, report as `edited` with both quotes, never `exact`.

## What we are missing
1. Store more than one selector (W3C allows several; Hypothesis stores range, position, quote). Add a CSS path. https://www.w3.org/TR/annotation-model/ (position-only is brittle).
2. Verify, don't trust: check the text at a stored position/element id matches the quote before accepting.
3. Specify prefix/suffix length (Hypothesis: 32 chars).
4. Table cells: record row/column header text (lavish-axi); charts/images get a label from alt text, caption or figure heading.
5. Stamp the page version the pin was made on ("pinned on v3").
6. Duplicate quotes: W3C says multiple matches match all; use prefix, suffix, heading; if still ambiguous, say so.
7. Unsent drafts whose target vanished on republish should be handed back, not dropped (lavish-axi).
8. No silent resolve; outdated must never change resolved state (Confluence).
9. `C` shortcut as in Figma and Vercel; ignore keystrokes in inputs and the iframe's forms.
