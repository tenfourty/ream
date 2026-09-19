# syntax=docker/dockerfile:1.7
#
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
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Dependencies first, from the lockfile alone: this layer only invalidates when
# pyproject.toml or uv.lock change, never on a plain source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Now the project itself (README.md is `readme = "README.md"` in pyproject.toml --
# uv_build refuses to build without it).
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm

COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
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
CMD ["uvicorn", "artifacts.serve:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
