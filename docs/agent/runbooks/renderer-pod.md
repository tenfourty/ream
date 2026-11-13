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
- a renderer that answers 503 is asked once more after its `Retry-After` (whole seconds, at most 10),
  when that fits in the same 30 s;
- a render the renderer gives no usable answer for (not up, slower than 30 s, still busy, or an answer
  that fails the service's checks) is stored as verdict `unchecked` with one finding saying the renderer
  could not be reached; one it answers it did not render (`not_run`) is stored as `unchecked` with a
  finding saying so. Neither is redone in the service's own process. Preview still answers 200, with
  `rendered: false` and `reason` `"renderer_unavailable"` or `"renderer_not_run"`.

**What the service trusts the renderer for.** The service checks the shape of every answer (the
picture is a well-formed PNG of the size asked for, the errors are short plain strings, the check
numbers are in bounds), but it cannot tell whether the renderer's check numbers and errors are honest.
A renderer is trusted for its verdict on a page, and that is all: it holds no key, no database and none
of the service's settings, so what it can affect is limited to its own pod and the verdicts and
pictures it answers.

## What a deployment adds

1. **A Deployment** of one replica running the service's own image (the same digest). Give the start
   line as the container's `args:`, and set no `command:`:

   ```yaml
   args: ["python", "-m", "ream.renderd", "--end-all-after-render", "--port", "8091"]
   ```

   `args:` replaces only the image's `CMD`, so the image's `ENTRYPOINT` (`/usr/bin/tini -g --`) stays,
   tini is pid 1 and renderd its child. A `command:` replaces the entrypoint too: renderd is then pid 1
   and refuses to start. The image already has the browser, fonts and init, and sets
   `REAM_BIND_HOST=0.0.0.0`, which renderd binds to.

   After every render, renderd ends every process of its uid in its container (`kill(-1, SIGKILL)`) and
   empties its work directory, so nothing a render started outlives it. That is safe only
   in its own container: renderd refuses to start without `--end-all-after-render` or `--dev`, and with
   `--end-all-after-render` it refuses when it is pid 1, when there is no `/proc/1`, or when it looks like a
   whole machine rather than a container (pid 1's command is `systemd` or `init` or cannot be read, or pid 2
   is `kthreadd` or is there but cannot be read). `--dev` is for a shared machine: nothing is ended, it says so at start, and a process
   left descended from renderd makes it exit instead. renderd also makes itself not dumpable at start, so
   the browser it starts cannot trace it or read its memory; it refuses to start if it cannot. Give it
   none of the service's environment: renderd refuses to start if `DATABASE_URL` or any `REAM_*` /
   `ARTIFACTS_*` variable other than `REAM_BROWSER` and `REAM_BIND_HOST` is set (it prints how many,
   never their names or values). It needs a writable `/tmp` and nothing else written. One browser runs
   at a time; a request that waits more than 5 s for it gets 503.
2. **A Service** in front of it on port 8091.
3. **A network policy** that admits only the service's pods to the renderer. renderd has no key or token:
   who may call it is decided by this policy alone. It requires a fixed, non-secret request header
   (`X-Ream-Render: 1`), so that a page in a browser cannot be made to call it.
4. **The pod's own limits:**
   - a network policy that allows no egress;
   - no service-account token (`automountServiceAccountToken: false`);
   - a read-only root filesystem, with `/tmp` an `emptyDir`;
   - all capabilities dropped, no privilege escalation;
   - a non-root user;
   - probes that are `httpGet` or `tcpSocket` on port 8091 (`/healthz` for liveness, `/readyz` for
     readiness), never `exec`.
5. Then set `REAM_RENDERER_URL=http://<service name>:8091` on the service and restart it. The host is a
   name of letters, digits, dots and hyphens, or an IP address; the port is 1 to 65535.

## How to check it

**Probes are `httpGet` or `tcpSocket` on renderd's port, never `exec`.** An exec probe runs as the same uid in
the same container, and is ended with every render.

- `GET /healthz` on the renderer: 200 while the process is up (liveness).
- `GET /readyz`: 200 once the browser, `prlimit` and a writable work directory are there; 503 naming
  what is missing otherwise (readiness).
- `python -m ream.renderd --dev --selfcheck --work-dir /tmp/rd-check` renders a built-in page, checks the
  answer and exits 0 (or 1). Run it by hand, never as a probe. Beside a running renderer it needs `--dev`
  (armed, it would end the renderer) and a work directory of its own (it empties the one it is given).
- From the service: publish a page, then `GET /api/v1/pages/<id>/render`. A verdict other than
  `unchecked` with the "could not be reached" finding means the two are talking.

## Going back

Unset `REAM_RENDERER_URL` on the service and restart it: renders run in the service's own process again.
The renderer can then be removed.
