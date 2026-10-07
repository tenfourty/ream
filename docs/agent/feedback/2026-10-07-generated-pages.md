# Feedback: pages a script regenerates, and pages that must stay current (7 Oct 2026)

Asked of every maintainer after reviewing runcove PR 132 (the programme-board generator), on
Jeremy's rule that agent-facing changes are shaped by their users. Questions: (1) which pages a
script or timer regenerates, and how often; (2) on a whole republish, should the wall name the
parts that changed — (a) automatically, (b) only when the publisher asks, (c) no; (3) freshness —
(a) a service-filled time plus `--touch` ("checked, still current", no new version), (b) the
filled-in time alone, (c) neither; (4) should the wall flag a page whose refresh stopped — (a)
interval declared by the publisher, (b) inferred from history, (c) no; (5) anything else.
Condensed from their replies; each is from real use unless marked.

Already shipped before the questions went out (090bed4): `--note` and `--if-revision` on a
whole-page `--update`. Already existing, which the question did not say: the service fills
`<!--artifacts:updated-->` with the publish time (warpgate and cove use it), and
`artifacts-publish --preview PNG` renders a page without publishing it.

| | Generated pages | Part names | Freshness | Staleness flag |
|---|---|---|---|---|
| warpgate | None; one hand-kept page, section updates since 6 Oct | (b); auto-diff of a hand-kept page names "the whole page" | (b), already uses the marker | (a) if built; inferring misfires on event-driven pages quiet for days |
| cove | Unknown whether its old generated boards still run; office page hand-published | (b), but (a) for an id appearing or disappearing (a new decision slot, a card removed) | (a): `--touch` keeps history meaningful | (a), and for hand-published pages too: the office page sat unrepublished 30 Sep–7 Oct while work moved hourly, until Jeremy asked |
| retrobrew | The MVP board, 30-min timer; ~1 in 4–6 renders differs (a guess) | (b): prefers `--note` naming what moved (the renderer knows); ids repeat across versions | (a): "Updated" = last real change, "checked" = last render; drops PR 132's stamp and checksum | (a): declares 30 min, flag at ~2.5 intervals |
| homelab | Ops board every 3 h (scripts/ops-board-tick.sh); runcove sign-ups page on a timer; both whole | (a): most republishes change one line and Jeremy cannot tell which | (a): a checked-but-unchanged page looks identical to a dead timer | (a): it knows the interval; pages quiet at night defeat inference |
| quarterdeck | _awaiting_ | | | |

Also asked for:
- cove: `--if-revision` by default on whole republishes; a publish warning for a decision slot
  that is not an open question on the page, and the reverse (check against what exists:
  homelab-ij8.50 warns on a slot naming no decision); a server-side nudge for unacknowledged
  answers, because background `artifacts-decide wait`s were reaped under memory pressure on baba
  on 7 Oct; `--preview out.png` (exists).
- homelab: a timer's publish failure is silent unless the script checks the exit code; a
  server-side "last successful publish" that the stale flag reads would catch it.
- retrobrew: `--touch --status failed`, so a broken renderer shows on the wall instead of looking
  current but old.

## Where they agree (so far)
- Part names on a whole republish: **split**. (b) from warpgate, cove and retrobrew — the
  publisher says what moved (`--note`); (a) from homelab, whose one-line changes are invisible to
  Jeremy. Cove's exception (an id appearing or disappearing) and a `--note` shown on the tile may
  satisfy both.
- Freshness: **(a)** from cove, retrobrew and homelab; a "checked" time distinct
  from "changed".
- Staleness: **(a)**, declared by the publisher, never inferred — and it applies to hand-kept
  pages as much as generated ones.
