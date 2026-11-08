# Moving the gallery to a new host name (REAM_FORMER_HOST)

The app never serves two gallery hosts: CSP `frame-ancestors`, the postMessage origin checks and the
gallery's Origin check all assume the one `REAM_GALLERY_ORIGIN`. A move is therefore one config flip, and
the old name is kept answering by `REAM_FORMER_HOST`.

## What the setting does

Set `REAM_FORMER_HOST=<old host name>` (bare host: no scheme, port or path; it must differ from the gallery
host and from `REAM_API_HOST`, or start-up refuses and names the variable). Unset, the old name 404s like any
unknown host.

On the former host (`hosts.is_former`, checked after the gallery and API hosts in the router in `app.py`):

| Request | Answer |
|---|---|
| `/healthz`, `/api/v1/*` (`/pages` and `/artifacts`, any method) | served by the API app, sharing the gallery's failed-key limiter. Never redirected: installed CLIs refuse every 3xx. |
| everything else (including `/api/v1` without a slash, `/.well-known/agent.json`, `/agents.txt`, form POSTs) | 308 to the same path and query on `REAM_GALLERY_ORIGIN`; `cache-control: max-age=3600`, `nosniff` |

The Location is built from the configured origin and the raw request path only, never from the Host header.
agents.txt, skill.md and agent.json fetched through the old name advertise the gallery origin, not the old host.
Key-authenticated API calls carry no Origin check, so POSTs through the old name work; browser-form routes
are not served there. A stale browser tab on the old name forwards a form with the old Origin and the gallery
refuses it; reloading fixes it.

`traps.py` treats a link to either host as a gallery link (needs `target="_blank"`) while the setting is set.

## Moving, in order

1. Land this feature (inert while unset).
2. Make the new name resolve and route to the same service (ingress, DNS, certificate, proxy auth rules).
3. In one rollout set `REAM_GALLERY_ORIGIN` to the new origin, `REAM_FORMER_HOST` to the old host name, and
   move any probe `Host` header to the new name (the router is fail-closed: a wrong Host is a 404).
4. Prove: old `GET /api/v1/me` 200; old `POST /api/v1/pages` 201; old `/a/<id>?x=1` 308 to the new host;
   `agents.txt` via the old name names the new one.
5. Keep the old name's ingress, certificate and DNS for good: installed CLIs default to it.

Rollback: restore the old `REAM_GALLERY_ORIGIN` and unset `REAM_FORMER_HOST`.
