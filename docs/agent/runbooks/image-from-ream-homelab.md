# How a core change reaches production

Since 9 Nov 2026 (`docs/decisions.md`) production's image is built by `runcove/ream-homelab`, not
here:

1. A push to `main` here passes `test` and `test-sqlite`; `image` builds the image and pushes
   `runcove/artifacts-core:<sha12>`, printing `PUBLISHED core image=... digest=...`. A
   `REUSED core` line means no new image: step 2 is skipped.
2. `core-lock` runs `.forgejo/scripts/core-lock-pr.sh`: it force-pushes branch `core-lock/next` on
   ream-homelab with `core.lock` set to that ref, opens (or updates) the one PR `core: lock <sha12>`,
   and asks the forge to merge it when checks succeed. It touches only a PR whose head is that
   branch on ream-homelab itself and sits at the commit it pushed. It stands down if `core.lock`
   already names this commit or a newer one.
3. ream-homelab's CI runs the combined suite; green, the PR merges by itself. Red, it stays open
   for this office.
4. ream-homelab's pin job builds the deployed image and opens the homelab pin PR; the homelab
   maintainer merges it, and that deploys.

Needs the secret `CORE_LOCK_TOKEN` on `runcove/artifacts` (write access to ream-homelab, nothing
else; never in a file or on a command line). Without it `core-lock` fails with "CORE_LOCK_TOKEN is
not set". Core's own `pin` job and `pin-pr.sh` are off; `pin-pr.sh` stays only for its selftest in
the `test` job and for a rollback.

## Roll back

Change the `if:` on the `pin` job in `.forgejo/workflows/ci.yml` (now `github.repository == 'runcove/artifacts' && false`)
back to its main-only condition, keeping the `github.repository == 'runcove/artifacts'` conjunct (the workflow-runner guard
refuses a job without it), and take `core-lock` off main. Homelab's pin lander must also accept `runcove/artifacts` as a source
again, which is the homelab maintainer's change; without it the pin PRs are refused. Close any open
`core-lock/next` PR on ream-homelab by hand.
