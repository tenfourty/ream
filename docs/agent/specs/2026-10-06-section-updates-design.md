# Updating one section of a page — design (6 Oct 2026)

Argued from the maintainers' feedback, `docs/agent/feedback/2026-10-06-section-updates.md`
(warpgate, homelab, quarterdeck, cove from real use; retrobrew as a guess). Each choice below
names whose feedback settled it.

## The problem it solves

Every maintainer's real failure is the same: a page is republished whole from a local copy that
has drifted from what is live, and content silently disappears — two release rows (warpgate), a
promise to Jeremy (homelab), a layout regression in an untouched part (cove). Section updates make
the server apply a small change to what is **live**, so nothing outside the section can be lost.

## Shape

- **Addressing: an element `id` already in the page** (all four). Exactly one element must match;
  zero or several is refused, loudly — never applied somewhere else (warpgate, homelab, qd).
- **Operations** (in the order they asked):
  - `replace` — the element's content (it keeps its tag and id) (all; most used);
  - `append` — a new last child (all; logs, "landed today");
  - `prepend` — a new first child (warpgate: newest row on top);
  - `remove` — the element (rare; homelab prefers replacing with a closed state).
  - Insert-after-id is left out until someone needs it (homelab: occasional).
- **Read one section**: `--fetch ID --section EL` returns just that element from the live page, with
  its section revision (warpgate: edit what is live, not a local copy).
- **CLI**: `artifacts-publish --update ID --section EL --replace|--append|--prepend FILE`,
  `--remove`; API `PATCH /api/v1/artifacts/{id}/sections/{el}` and `GET` of the same. Every gallery
  control keeps an API twin; this is an agent action with no gallery control.

## Safety (all four agreed)

- **Its own version**, labelled with the section and operation ("#running-now: prepend"), so
  rollback stays "fetch version N, publish" and history shows what moved.
- **The page check runs on the whole resulting page** — CSP, the built-in scanner and GitGuardian
  — because a secret or a broken tag can span a section boundary (qd, cove). A fragment that
  leaves its section unbalanced is refused (cove).
- **Question slots are never lost silently**: if the result drops a `data-artifact-decision`
  placeholder for an open question, the update is refused (homelab; cove asked for at least a
  warning).
- **No notification by default**; `--notify` for the updates that need Jeremy's eye (all four).
- The response echoes the new page revision and the section's revision, so a script can chain
  (warpgate).

## Concurrency (where they differed)

Both preconditions are offered, the caller choosing:
- `--if-section REV`: refuse only if **this section** changed since it was read — two sessions
  editing different sections both land (cove, qd, warpgate, retrobrew).
- `--if-revision REV`: refuse if **anything** on the page changed since that revision — the
  whole-page guard (homelab), also usable on a whole-page republish (warpgate).
- With neither, the operation applies to the live page as it is; other sections are untouched by
  construction (a generator that owns its section can just overwrite, as cove asked).

## For Jeremy (his gallery)

How a section update shows on his wall and in a page's history is his call, not the agents' —
posed to him as lettered questions on the artifacts maintainer's design page.
