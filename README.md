# artifacts

A self-hosted "artifacts" publisher for the homelab: agents publish an HTML
page over a keyed API and get back a stable URL. Jeremy views published pages
in an Authelia-gated gallery; each page is served from its own
`<id>.<content domain>` hostname behind a capability token in the URL.

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
uv run uvicorn artifacts.serve:app_from_env --factory --reload
```

The router is fail-closed: `/healthz` only answers 200 with
`Host: artifacts.tenfourty.site` (or whatever `ARTIFACTS_GALLERY_ORIGIN`
names) and 404 on anything else, so curl it with that Host header, not the
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

`artifacts-admin revoke-key <key_id>` and `artifacts-admin list-keys` manage
keys from there; neither ever prints a token.

## Publish a page

```bash
artifacts-publish page.html --title "My page"
artifacts-publish page.html --update <id>   # after editing page.html
artifacts-publish --list
```

Where the key comes from, in the order to prefer:

```bash
# A vault hands it over on a descriptor; no cleartext copy exists. On baba:
ARTIFACTS_SEAT=baba_operator artifacts-key -- artifacts-publish page.html --title "My page"

# Or any store of your own that can open one:
artifacts-publish --key-fd 3 page.html --title "My page" 3< <(pass show artifacts/key)

# Or a file, mode 600 -- it refuses anything looser:
ARTIFACTS_KEY_FILE=~/.config/artifacts/key artifacts-publish --list
```

## Tidy one up

Everything Jeremy can do to a page in the gallery, an agent can do to its own
pages — his rule, 20 Sep 2026: *"ideally agents can take all the same actions a
human can."* None of these send the body, so none of them needs the file:

```bash
artifacts-publish --rename <id> --title "Its real name"
artifacts-publish --archive <id>     # off the wall; nothing is deleted
artifacts-publish --unarchive <id>
artifacts-publish --pin <id>         # leads the gallery
artifacts-publish --unpin <id>
artifacts-publish --duplicate <id>   # a copy at a new address
```

Over HTTP these are `POST /api/v1/artifacts/{id}/{archive,rename,pin}`, each
scoped to the calling key's own agent: another seat's page answers 404, and so
does a page that does not exist — the same 404, so a key cannot be used to
learn which ids are real.

`$ARTIFACTS_API` (default `https://artifacts.tenfourty.site/api/v1`) and
`$ARTIFACTS_KEY_FILE` (default `~/.config/artifacts/key`) point it elsewhere;
`--key-file` overrides both for one call.

## How deploys happen

Every push to `main` runs `.forgejo/workflows/ci.yml`: tests, then an image
build pushed to `git.tenfourty.site/jeremy/artifacts:<sha12>`, then a commit
that rewrites `deploy/kustomization.yaml`'s `newTag:` to that sha and pushes
it back to `main` (with `[skip ci]`, so it never retriggers itself). Flux
applies `deploy/` from there on — pushing to `main` is the whole deploy step.
