# Feedback: artifacts for the whole company (26 Oct 2026)

**runcove-5t3oj.46**, the company-wide design (many people, personal and shared agents,
Drive-style sharing, live data). Jeremy's four answers are on the item; this round covers the
agent-facing side, before any of it is designed.

Asked of every office on 26 Oct (#bots thread 1793010545.702289):
(1) who a page is from: a person's agent ("Jeremy, via cove") or a shared agent ("cove");
(2) who else should read your pages; (3) which pages would be better with data fetched when
opened, and from where; (4) the one friction that costs most today.

Nine offices answered within a minute.

| | (1) From | (2) Share with | (3) Live source | (4) Friction |
|---|---|---|---|---|
| gg-ops | shared; prod reports must name the accountable office | SRE team, cove users; host internals team-scoped, never everyone | OVH env-check output, cove-prod `/health`, tracker | finding an old report's page id to update it |
| warpgate | shared; readers care when it was last updated | other maintainer offices; security triage Jeremy-only until released | forge Actions runs, tracker (`bd list --label warpgate`) | hand-prepending a section per CI run, so the page lags the forge |
| harland | shared, acting for the project; readers want which office acts on an answer | release co-owners; SDK/API builders | forge PRs and CI, tracker | ownership keyed to the session label, not the office: a later session could not ack or ask |
| ch | shared; the instruct it acted on should be linkable | cove, gg-ops, cove host runners; measurement reports to whoever sets a default | forge package files and checksums, CI, tracker | nothing links a page to the bead or release it describes, so it drifts silently |
| runcove-pmo | shared, for the whole house | every office and anyone at GitGuardian; decision memos Jeremy-only | tracker, forge, decision stores (artifacts and quarterdeck asks) | cannot read answers on another office's page; `artifacts-decide list` has no per-page filter |
| quarterdeck | shared; a recorded decision should name the person | operators and adopters; review rounds Jeremy-only | forge PRs and CI, tracker, hub pin | `artifacts-wait --session` exits after ~47 min, and 7 of 17 requests failed on one run |
| retrobrew | shared, timer-driven | anyone following Retrobrew; everyone once public | the repo's beads | the `artifacts-key` wrapper error does not list its seat |
| jeremys-mac | **personal**: "Jeremy, via jeremys-mac"; it speaks for his machine and his go-aheads | cove maintainers, anyone trying local mode; decision memos Jeremy-only | tracker, forge PRs and CI | questions went stale on pages while he answered in Slack (9 found); now Slack asks |
| cove | shared | cove engineers, ops seats; release checklists to cove-dev testers | tracker, forge PRs/heads/CI | republishing the whole page for one bead's status |

## What it shapes

- **Both kinds already exist.** Eight offices are shared agents; jeremys-mac is a personal one
  ("Jeremy, via jeremys-mac"). So "a person, via their agent" is needed on day one, not only on
  the company side, and the two must read differently on the page.
- **Ownership by office, not by session** (harland). A session label attributes a page; it
  should never gate who can ack or ask on it.
- **Team-scoped sharing is the common case**, with a Jeremy-only floor for security triage and
  decision memos. "Everyone" is rare and explicit.
- **Live data is wanted by all nine, and the sources are the same three:** the tracker, the
  forge, and a service health endpoint. On the homelab that argues for server-side connectors
  to those sources before any warehouse; on the company side Snowflake is the fourth.
- **A page should name what it describes** (ch, gg-ops): a link from page to bead, release
  or report key, so an agent can find "the page for X" and a reader can tell if it is stale.
- **Partial updates** (cove, warpgate): one bead's change should not mean republishing the page.
  Live sections answer most of this.
- **Cross-office reading of decisions** (pmo) and **a reliable wait** (quarterdeck) are fixes
  to file against today's service, not the company design.
