# Running the renderer as its own process or pod (REAM_RENDERER_URL)

Ream draws each page's picture, and checks what the page did, in a headless browser. By default that
browser runs inside the service's own process. A deployment can instead run it as its own process or pod,
for resource isolation: the browser's memory and CPU are then the renderer's, not the service's. The
choice and the options that lost are in `docs/decisions.md` (13 Nov 2026).

## What the service does with it

Set `REAM_RENDERER_URL=http://<host>:<port>` on the service. It must be `http://` with an explicit port
and nothing else, on a host other than the gallery's, `REAM_API_HOST` and `REAM_FORMER_HOST`; start-up
refuses anything else and names the variable. Unset, nothing changes.

Set:
- every render (the background picture after a publish, and `POST /api/v1/preview`) is sent to the
  renderer; the service starts no browser and ignores `REAM_BROWSER`;
- `renderer: true` is reported whether or not the renderer is up; the service's own readiness never
  depends on it;
- a render the renderer gives no usable answer for (not up, slower than 30 s, busy, or an answer that
  fails the service's checks) is stored as verdict `unchecked` with one finding saying the renderer could
  not be reached. It is never redone in the service's own process. Preview still answers 200, with
  `rendered: false` and `reason: "renderer_unavailable"`.

## What a deployment adds

1. **A Deployment** of one replica running the service's own image (the same digest), with the command
   `python -m ream.renderd --port 8091`. The image already has the browser, fonts and init. Give it
   none of the service's environment: renderd refuses to start if `DATABASE_URL` or any `REAM_*` /
   `ARTIFACTS_*` variable other than `REAM_BROWSER` and `REAM_BIND_HOST` is set (it prints how many,
   never their names or values). It needs a writable `/tmp` and nothing else written. One browser runs
   at a time; a request that waits more than 5 s for it gets 503.
2. **A Service** in front of it on port 8091.
3. **A network policy** that admits only the service's pods to the renderer. renderd has no key or token:
   who may call it is decided by this policy alone. It requires a fixed, non-secret request header
   (`X-Ream-Render: 1`), so that a page in a browser cannot be made to call it.
4. Then set `REAM_RENDERER_URL=http://<service name>:8091` on the service and restart it.

## How to check it

- `GET /healthz` on the renderer: 200 while the process is up (liveness).
- `GET /readyz`: 200 once the browser, `prlimit` and a writable work directory are there; 503 naming
  what is missing otherwise.
- `python -m ream.renderd --selfcheck` inside the renderer's container renders a built-in page, checks the
  answer and exits 0 (or 1). The same command works as an exec probe.
- From the service: publish a page, then `GET /api/v1/pages/<id>/render`. A verdict other than
  `unchecked` with the "could not be reached" finding means the two are talking.

## Going back

Unset `REAM_RENDERER_URL` on the service and restart it: renders run in the service's own process again.
The renderer can then be removed.
