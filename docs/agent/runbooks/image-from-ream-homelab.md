# How a core change reaches production

Since 9 Nov 2026 (`docs/decisions.md`) production's image is built by `runcove/ream-homelab`, not
here:

1. A push to `main` here passes `test` and `test-sqlite`; `image` builds the image and pushes
   `runcove/artifacts-core:<sha12>`, printing `PUBLISHED core image=... digest=...`. A
   `REUSED core` line means no new image: step 2 is skipped. The same job builds `--target full`
   (the Dockerfile's last stage, the image with the browser), pushes that one digest as
   `runcove/artifacts:<sha12>`, `runcove/artifacts-core:<sha12>` and `runcove/artifacts-renderer:<sha12>`
   (printing `PUBLISHED renderer image=... digest=...`; job output `renderer_ref`, which nothing reads yet),
   then builds `--target app`, runs it and the full image, and fails if the app image finds a browser or
   the full image finds none. The app image is not pushed. That check runs after the tags are pushed:
   if it fails, the images stay published but `core-lock` is skipped for that commit, and a re-run does
   not recover it (the re-run finds the pushed tag and reuses it, so `core_ref` is empty). Only a new
   commit on main unblocks the deploy.
2. `core-lock` runs `.forgejo/scripts/core-lock-pr.sh`: it force-pushes branch `core-lock/next` on
   ream-homelab with `core.lock` set to that ref, opens (or updates) the one PR `core: lock <sha12>`,
   and asks the forge to merge it when checks succeed. It touches only a PR whose head is that
   branch on ream-homelab itself and sits at the commit it pushed. It stands down if `core.lock`
   already names this commit or a newer one. When the `image` job also published
   `runcove/artifacts-renderer:<sha12>` (`PUBLISHED renderer image=...`), the same commit writes
   `renderer.lock` (one line, the renderer's `<repo>:<sha12>@sha256:<digest>`, created if absent), so one
   PR moves both. If the renderer step failed or reused a tag, only `core.lock` moves and the
   renderer pin stays where it was until the next run that publishes one. The stand-down rules read
   `core.lock` only; an existing `renderer.lock` that is not exactly one renderer ref is refused, not
   overwritten.
3. ream-homelab's CI runs the combined suite; green, the PR merges by itself. Red, it stays open
   for this office.
4. ream-homelab's pin job builds the deployed image and opens the homelab pin PR; the homelab
   maintainer merges it, and that deploys. When ream-homelab has a `renderer.lock`, the same pin PR
   also moves the renderer pod's image (`renderer.yaml`, both containers) to it, and the pin job
   prints a second line, `PUBLISHED renderer image=... digest=...`. The app's `PUBLISHED image=...`
   line is unchanged. With no `renderer.lock`, or while `renderer.yaml` still holds the placeholder
   image, the pin PR changes the app's line only. The renderer moves only when `renderer.lock`'s tag
   equals `core.lock`'s (else it is left alone and the PR says so). The two repos can land in either
   order only while `renderer.yaml` has the placeholder: in the same change that replaces it, the
   homelab repo must remove the kustomize `replacements` that copy the app image onto the renderer
   pod and teach its pin lander that the artifacts target may change `renderer.yaml`
   (homelab bead homelab-q9dof).

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
