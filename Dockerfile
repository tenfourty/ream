# Two stages: `build` resolves dependencies and builds this project with uv, on
# top of a full Debian base (uv needs a working compiler toolchain for psycopg's
# C extension isn't needed here -- psycopg[binary] ships wheels -- but the slim
# base is enough either way). `runtime` copies only the finished virtualenv onto
# a fresh copy of the same base image: uv and pip never exist in the image a
# pod actually runs.

FROM python:3.12-slim-bookworm AS build

# The pinned uv release itself, copied from astral's own image rather than
# installed via pip/curl -- no interpreter needed to fetch it, and the pin
# means the exact uv version is a `FROM` line, not a moving `latest`.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1

# Dependencies first, from the lockfile alone: this layer only invalidates when
# pyproject.toml or uv.lock change, never on a plain source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project --extra server

# Now the project itself (README.md is `readme = "README.md"` in pyproject.toml --
# uv_build refuses to build without it).
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra server

# The agents' tool package, which the service hands out from an open address
# (homelab-ij8.68): an agent on another machine runs it with uvx. Built here so
# it is always exactly the code this image runs. It has no dependencies -- the
# server's are the `server` extra above -- so it installs anywhere Python does.
#
# A NEW VERSION EVERY BUILD, stamped into pyproject.toml just before this step
# (after the sync, so the lockfile is never consulted). Measured 23 Sep 2026:
# uv reused its cached copy of a package from the same URL despite no-cache,
# so agents kept running yesterday's tools. A version per build means a new
# filename, hence a new URL, on every deploy -- and agents.txt always prints
# whatever file is here.
RUN sed -i "0,/^version = \"\([0-9.]*\)\"/s//version = \"\1.post$(date -u +%Y%m%d%H%M%S)\"/" pyproject.toml \
 && grep -m1 '^version' pyproject.toml \
 && uv build --wheel --out-dir /app/dist

FROM python:3.12-slim-bookworm

# The headless browser that draws a page's thumbnail for the gallery wall
# (homelab-ij8.22). Jeremy chose the renderer running inside the service over a
# separate one, with the cost of that stated to him; `artifacts.thumbs` is where
# the constraints on the child process live, and they are the reason this is
# acceptable rather than merely convenient.
#
# BEFORE the venv COPY on purpose. This layer changes only when the base image
# or this line does, so it is built once and pulled once: every ordinary deploy
# invalidates the small app layer below it and re-uses this one. The image goes
# from about 167 MB to roughly 500 MB, but the bytes moved per deploy barely
# change, which is the number that actually matters when CI deploys on every
# push.
#
# Debian's `chromium` rather than a downloaded bundle: apt resolves the
# twenty-odd system libraries it needs, and the package keeps getting security
# updates. `artifacts.thumbs` finds it on PATH, so nothing has to name the path.
#
# fonts-dejavu-core is not optional. The pages deliberately use no web fonts —
# they ask for the reader's own sans, serif and mono — so a container with no
# fonts at all renders every thumbnail as boxes. DejaVu supplies all three.
# A thumbnail will therefore not be letter-for-letter what Jeremy sees in his
# own browser, which is inherent to rendering anywhere but on his machine.
# The chromium layer is the slowest step (~2 min) and the biggest push (~70 s),
# and it is identical from one build to the next, so CI reuses it from the
# previous build's cache image. CHROMIUM_REFRESH is the ISO week: a new week
# changes this line, which rebuilds the layer, so chromium's security updates
# arrive within a week and a cached layer never goes stale for longer.
ARG CHROMIUM_REFRESH=unset
RUN echo "chromium layer for ${CHROMIUM_REFRESH}" && apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu-core tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/dist /app/dist
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8000

# uvicorn reads FORWARDED_ALLOW_IPS from the environment itself when it is not
# passed on the command line (uvicorn 0.53.0, config.py: `self.forwarded_allow_ips
# = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1")`) -- the deployment's
# ConfigMap sets it, so it does not need to appear on this command line too.
# --no-access-log is not a style choice: every content-page URL carries its
# read capability token in the path, and an access log would put it on disk.
# --timeout-graceful-shutdown is a BACKSTOP, not the mechanism: held /events
# requests end themselves the moment SIGTERM arrives (artifacts.serve), so
# uvicorn should find nothing to wait for. If something ever does hold on, 15
# seconds is well inside Kubernetes' 30-second default grace period, so the
# process exits on its own terms rather than being killed mid-answer.
# tini is PID 1 so orphaned chromium helpers are reaped instead of piling up as
# zombies (homelab-o88p). -g forwards signals to the whole group, so SIGTERM
# still reaches uvicorn for its graceful shutdown.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["uvicorn", "artifacts.serve:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "15"]
