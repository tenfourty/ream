# Ream (formerly artifacts)

A self-hosted page publisher: agents publish an HTML
page over a keyed API and get back a stable URL. A person views published pages
in a gallery behind a sign-in; each page is served from its own
`<id>.<content domain>` hostname behind a capability token in the URL.

## Install the tools

The agent command-line tools (`ream`, with the older `artifacts-*` commands beside it) install
from PyPI:

```bash
uv tool install ream-artifacts
```

The install name is `ream-artifacts`; a bare `ream` on PyPI is someone else's package.

## Development

```bash
uv run pytest        # run tests
uv run ruff check src tests   # lint
```

Requires Python 3.12 exactly (pinned in `.python-version`) — a later task's
test dependency (`pgserver`) has no wheels for 3.13+.

## Run locally

```bash
export DATABASE_URL=postgresql://...   # a real Postgres; migrate() runs on startup
uv run uvicorn ream.serve:app_from_env --factory --reload
```

The router is fail-closed: `/healthz` only answers 200 with the gallery's own
`Host` (the host of `REAM_GALLERY_ORIGIN`; `localhost:8000` on a fresh install)
and 404 on anything else, so curl it with that Host header, not the
bare address.

## Mint a key for an agent

A token is generated where it will be used and never crosses a terminal, a
transcript, or a network hop — only its SHA-256 is ever registered:

```bash
# On the machine (or pod) that will hold the key:
artifacts-admin new-key --agent my-agent --out <your key file>

# Then, with the sha256 that command just printed, on the running pod:
kubectl -n artifacts exec deploy/artifacts -- artifacts-admin register-key \
  --agent my-agent --sha256 <the printed sha256>
```

`artifacts-admin revoke-key <key_id> [--reason ...]` and `artifacts-admin list-keys` manage
keys from there; neither ever prints a token. A revocation records who did it and why, writes an
`agent_key_revoked` event, and a machine still sending the key is told the reason in its 401.
`artifacts-admin rename-agent <old> <new> --revoke-keys` (a fold) lists each live key of the old name
with its last use, and refuses when any was used in the last 24 hours unless `--force`; `--dry-run`
shows the same list.

## The skill

One skill, `GET /api/v1/skill.md` (open, no key), in the open SKILL.md format: save it wherever
your agent keeps skills. It is named `ream` and fires whenever the agent shows or shares something
beyond a short chat answer (a report, a plan, a chart, a diagram, a comparison), not only on the
words "page" or "publish". With a key set up it publishes to Ream instead of a host's built-in
artifacts or canvas panel; with none it falls back to that panel and says why. A page carries a
question only when the agent is blocked on the person or needs their choice. It names no house, so
it works for any agent on any host; a deployment's own skill adds only its look and keys.

## Publish a page

The command is `ream`, with a verb for each job (`ream publish`, `ream ask`,
`ream wait`; `ream --help` lists them). The older `artifacts-publish`,
`artifacts-decide` and the other `artifacts-*` commands keep working for good,
as do the `ARTIFACTS_*` settings, and `/api/v1/artifacts` answers exactly as
`/api/v1/pages` does (the same handlers, so a POST to either works).

```bash
ream publish page.html --title "My page"
ream publish page.html --update <id>   # after editing page.html
ream publish --list
```

Where the key comes from, in the order to prefer:

```bash
# A vault hands it over on a descriptor; no cleartext copy exists. Where keys are
# vaulted, a wrapper does it (here `artifacts-key`, with your seat's name):
ARTIFACTS_SEAT=<seat> artifacts-key -- ream publish page.html --title "My page"

# Or any store of your own that can open one:
ream publish --key-fd 3 page.html --title "My page" 3< <(pass show artifacts/key)

# Or a file, mode 600 -- it refuses anything looser:
REAM_KEY_FILE=~/.config/artifacts/key ream publish --list
```

## Tidy one up

Everything Jeremy can do to a page in the gallery, an agent can do to its own
pages — his rule, 20 Sep 2026: *"ideally agents can take all the same actions a
human can."* None of these send the body, so none of them needs the file:

```bash
ream publish --rename <id> --title "Its real name"
ream publish --archive <id>     # off the wall; nothing is deleted
ream publish --unarchive <id>
ream publish --pin <id>         # leads the gallery
ream publish --unpin <id>
ream publish --duplicate <id>   # a copy at a new address
```

Over HTTP these are `POST /api/v1/pages/{id}/{archive,rename,pin}`, each
scoped to the calling key's own agent: another seat's page answers 404, and so
does a page that does not exist — the same 404, so a key cannot be used to
learn which ids are real.

`$REAM_API` (default `https://artifacts.tenfourty.site/api/v1`) and
`$REAM_KEY_FILE` (default `~/.config/artifacts/key`) point it elsewhere;
`--key-file` overrides both for one call.

## How deploys happen

Every push to `main` runs `.forgejo/workflows/ci.yml`: tests, then an image
build pushed to the forge's registry as `<repo>:<sha12>`, then a pull request on
the deployment's own repo that moves its `core.lock` to that image
(`.forgejo/scripts/core-lock-pr.sh`). That repo builds the deployed image and opens
the pin PR; production moves when that PR is merged. The runbook is
`docs/agent/runbooks/image-from-ream-homelab.md`. This repo keeps no deployment
values: they are in the deployment's own repo.
