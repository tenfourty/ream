# Production's image comes from ream-homelab

Today core's `pin` job opens the homelab pin PR. After the cut-over, core's `core-lock` job opens
a PR on `runcove/ream-homelab` that moves its `core.lock` to the new core image and asks the forge
to merge it when that repo's checks succeed; ream-homelab then builds the production image and
opens the homelab pin PR (`docs/decisions.md`, 9 Nov 2026). One setting chooses which path runs:
the repo Actions variable `REAM_IMAGE_FROM` (`ream-homelab`: `core-lock` runs, `pin` is skipped;
unset or anything else: `pin` runs, `core-lock` is skipped).

## Cut over

1. Homelab creates the account's token and sets it as the secret `CORE_LOCK_TOKEN` on
   `runcove/artifacts`: write access to `runcove/ream-homelab` (push a branch, open and merge PRs)
   and nothing else. Never put it in a file or a command line. A run with the variable set and no
   token fails `core-lock` with "CORE_LOCK_TOKEN is not set".
2. Switch ream-homelab's own `pin` job on (its CI, `main`), so a merged lock update produces the
   homelab pin PR. Check one pin PR from ream-homelab is merged and deploys before step 3.
3. Set the repo variable `REAM_IMAGE_FROM` to `ream-homelab` on `runcove/artifacts`.
4. Watch the next push to `main`: `image` prints `PUBLISHED core image=...`, `core-lock` opens
   `core: lock <sha12>` on ream-homelab, it merges when green, ream-homelab opens the pin PR.
   A `REUSED core` line means no new image, so `core-lock` is skipped.

## Roll back

Unset `REAM_IMAGE_FROM` (or set it to anything but `ream-homelab`). The next push to `main` runs
`pin` again and skips `core-lock`. Close any open `core-lock/next` PR on ream-homelab by hand.

## Things to know

- One branch, `core-lock/next`, is force-pushed, so there is one open lock PR; a newer image
  updates it.
- The script stands down when `core.lock` already names the commit or a newer one.
- A red check on ream-homelab leaves the PR open for this office.
