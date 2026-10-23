# Feedback: a discovery file at /.well-known/agent.json (23 Oct 2026)

**runcove-5t3oj.28**, from the Quarry deep dive. Jeremy said go on 23 Oct
(d_01M5KSBN3XNA8192KAGGP55VX4, option a): start it now, beginning with this round, which
also settles whether the file names a fixed host or the host it was asked on.

Asked of every office on 23 Oct (#bots thread 1792739475.152219):
(1) how your agent finds artifacts' routes today; (2) whether you'd read a JSON route list,
and for what; (3) the fields you'd need; (4) a fixed configured host, or the host you asked
on, as agents.txt does; (5) any agent-card format you already read.

All ten offices answered within two minutes.

| | (1) Today | (2) Use | (3) Fields beyond base URL, routes, auth | (4) Host | (5) Format |
|---|---|---|---|---|---|
| harland | skill + CLI | to check a route or its auth when the CLI fails | version, docs link | fixed | none |
| gg-ops | skill + CLI | from a box with no CLI (the OVH host in an incident) | version | fixed | n/a |
| cove | CLI | none | version | fixed | OpenAPI, if any |
| jeremys-mac | skill + CLI | when the CLI is missing or old | version, install line, docs | fixed (roams LAN, Tailscale, public) | plain JSON |
| warpgate | CLI | when the CLI breaks or a route is new | version, install line | fixed; list both names if needed | none |
| homelab | CLI via lab-artifacts | a fresh worker with no skill; CI checking routes still exist | install line, version with a changed-at date | one fixed host per audience, both from config; would change agents.txt too | OpenAPI, if any |
| runcove-pmo | skill + CLI | to tell a refusal from an outage when a call fails | install line and minimum version, service version, docs | fixed; list both | A2A, if any |
| quarterdeck | CLI via lab-artifacts | a bell or client outside the house with no skill | install line, version (to tell a stale copy) | fixed; list both | A2A shape, if any |
| retrobrew | CLI in a script | only for an agent without the tools | install line, version | fixed, or a fixed list | no |
| ch | skill + CLI | to check a route or auth before guessing | install line, version | fixed; list both | n/a |

**Read.** Nobody reads agents.txt; everyone goes through the CLI. The file is for the agent
the CLI doesn't serve: missing, broken or out of date, or on a machine outside the house.
**All ten want hosts from configuration, never from the request**: an agent follows the
file and sends its key where it points, so a forged Host would be a phishing aid. Several
add: list each configured name (the house name and the public API host) explicitly. Fields
all ten name: base URL, routes and methods, the auth scheme (never a key), a version; most
add the install line. No shared card format; plain JSON.

## What was built, and whose answer shaped it

- **Hosts from configuration only** (all ten): `ARTIFACTS_GALLERY_ORIGIN`, and
  `ARTIFACTS_API_HOST` when set, each listed with what it reaches (warpgate, homelab,
  runcove-pmo, quarterdeck, retrobrew, ch). On the public API host the card names only that
  host, as agents.txt there never names the house name.
- **The routes that need no key, and where the full list is.** The full contract stays at
  `/api/v1/openapi.json` behind a key, as decided when it was built ("the shape of this API is
  not something any device on the LAN should be able to ask for"). Every office that wants to
  check a route still exists (homelab's CI, harland, ch) already holds a key. A reviewer
  caught that a full open list would have undone that decision.
- **Auth as a scheme and a header**, never a key or where one lives (ch, runcove-pmo).
- **Version is the tool package's** `0.1.0.post<UTC build time>`: a version and a changed-at
  date in one (homelab, quarterdeck), and the same one the install line fetches.
- **The install line and docs links** (most offices).
- **Plain JSON**, not A2A or OpenAPI: no office reads either today (jeremys-mac, cove,
  quarterdeck said "only if free").
- Served at **`/api/v1/agent.json`** (reachable on the house name, where Authelia bypasses only
  /api/v1) and at **`/.well-known/agent.json`** (the public API host; behind Authelia on the
  house name). agents.txt names it in "Where you are".

Not done here: homelab's suggestion to make agents.txt name configured hosts too. agents.txt
takes the name it was asked on deliberately (app.py), and the router already refuses any
name but the configured ones, so a forged Host never reaches it; changing it is a separate
question.
