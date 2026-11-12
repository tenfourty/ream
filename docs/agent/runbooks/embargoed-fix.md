# An embargoed security fix reaches production before it is public

`runcove/artifacts` is public (every branch) and its `main` is mirrored to GitHub; its images
(`runcove/artifacts-core`, `runcove/artifacts`) can be pulled by anyone, and ream-homelab's CI
installs the core from the public repo and pushes its image to the public owner. So an embargoed
fix touches none of them until release.

1. **Where it lives.** The private repo `runcove-sec/artifacts-sec` (no mirrors; created fresh,
   never a fork of the public repo, and nobody forks it). Its `main` is the public `main` plus the
   fix. Branch `sec/<topic>` there, open the PR there, security reviews it, merge into its `main`.
   Commit subjects and PR text name the behaviour, never the weakness. Work from the clone in
   "The working clone" below, nowhere else.
2. **The workflow guard, then the privacy check, run first.** `.forgejo/workflows/sec.yml` (inert
   outside artifacts-sec) starts with the job `runner-guard`, which runs
   `scripts/guards/workflow-runner-guard.sh` on the pushed tree before any job that uses
   `SEC_CHECK_TOKEN`; every other job waits for it, and the guard itself refuses a `sec.yml` where
   that is not so. The guard job has exactly two steps, a checkout pinned to a commit and the guard
   step, which builds its own Python with PyYAML from hash-pinned downloads in a temporary
   directory (`--pinned-python`). One sec run goes at a time (a fixed concurrency group). Next comes `.forgejo/scripts/sec-private-check.sh repo`: the repo private, not a fork, with no
   forks and no push mirror (a mirror would carry every branch and tag), and the registry owner a
   private org, proved both anonymously (refused) and with a token (settings read). It also proves
   where the jobs run, from the forge's own list of the runners it would hand them to: every runner
   of the org `runcove-sec` carries exactly `["runcove-sec"]` or exactly `["runcove-sec-unpriv"]`
   and at least one carries each, none is registered to the repo itself, no global runner is
   listed at all, no runner outside the org carries a label starting with `runcove-sec`, and the runner running the check
   (`SEC_RUNNER_NAME`) is one of the org's and carries the label its job runs on (sec.yml passes
   it, and the workflow guard requires it to match the job's `runs-on`). Any error or unexpected answer fails the run
   before a test or a build. It runs again on the image runner
   right before pushing, and once more after the last push (see 4).
3. **Tests.** The same jobs as `ci.yml` (copied; keep them in step). Every job in `ci.yml` has
   `github.repository == 'runcove/artifacts'` in its `if:`, so none of them is dispatched in
   artifacts-sec; every job in `sec.yml` runs on a label only this repo's dedicated runners carry
   (the image build on the privileged one, every other job on the unprivileged one). The point: the jobs of artifacts-sec
   hold its `SEC_*` secrets, so they run only on its dedicated runners. `scripts/guards/workflow-runner-guard.sh`
   refuses a workflow that breaks either rule (CI runs it with the other guard selftests; run it
   before pushing any change under `.forgejo/workflows`, because a push to artifacts-sec runs the
   file it carries). Add a job to `ci.yml` with the gate, or the guard fails the run. In `sec.yml` every action is
   pinned to a full commit (or a `docker://` digest), every container and service image to an
   `@sha256:` digest, the docker CLI download is checked against a pinned sha256, and the test jobs
   install uv with `workflow-runner-guard.sh --install-uv` (a pinned, sha256-checked release); only
   the `image` job may carry host-privilege container options. To move a pin, change the commit,
   digest or hash in the same commit and run the guard's selftest. It holds
   `.github/workflows` too (the GitHub mirror's `publish.yml`): every job there is gated to
   `github.repository == 'tenfourty/ream'`, so a forge setting or version that reads that folder still
   dispatches nothing in artifacts-sec.
4. **Images.** On artifacts-sec `main` only: `runcove-sec/artifacts-core:<sha12>`, then the
   deployed image `runcove-sec/artifacts:<sha12>-<plugins sha12>` from ream-homelab's Dockerfile
   at its `main` on that core. Straight after its push each is proved to exist (an authenticated
   call lists its tags; an anonymous 404 alone would also be the answer for a mistyped name) and
   then not pullable anonymously. After the last push the repo, the owner and both images are
   proved again, in case one went public while the pushes ran; only then does the run print one
   `SEC BUILT image=<ref>@sha256:<digest>` line. No lock PR: ream-homelab's `main` never moves.
5. **The pin is by hand.** No automation pins a sec image: nothing in this repo or in
   ream-homelab opens or merges a pin for it. The maintainer sends homelab's office the image
   reference (the `SEC BUILT` line) and the words "security fix", nothing about the weakness, and
   homelab pins it by hand. Production pulls it with the private owner's pull credentials, which
   homelab holds. Prove the fix in the pod, then tell security. At release the normal pin
   replaces it.
6. **Release** (after the rollout and security's yes): push artifacts-sec `main` to
   `runcove/artifacts` `main` as a fast-forward (if public `main` moved, merge it into
   artifacts-sec first and let sec.yml rebuild and re-pin). The normal chain rebuilds the public
   images and the next ream-homelab pin replaces the sec image, moving production back to the
   public owner. The push goes to the public URL given on the command line, `main` only (see
   below), never through a remote added to the working clone. The order is fixed: **the operator's
   yes -> delete the local `sec/*` refs -> push.** The pre-push guard refuses that push while a
   local `sec/*` branch or tag is still in the clone (it would carry the fix), so it stays armed
   until the yes; only after the yes, delete those local refs (`git branch -D sec/<topic>`,
   `git tag -d sec/<tag>`), which is the deliberate act that lets the release go. Never delete
   them earlier to "tidy up": that disarms the guard before the release is approved.

Secrets on `runcove-sec/artifacts-sec` (homelab sets them; never in a file or on a command line):

| Name | What it is |
|---|---|
| `SEC_CHECK_TOKEN` | the single token of `artifacts-sec-checker`, an **owner** of `runcove-sec` (conditions below) (Forgejo lists a repo's runners only to its owners: `reqOwner`, so a repo admin is refused 403), with the scopes `read:organization,read:repository` and no other: `sec-private-check.sh repo` proves that first, by probing, and stops if not. The runner list needs no further scope (it sits under `read:repository`) |
| `SEC_REGISTRY_TOKEN` | package write on `runcove-sec` only, account `artifacts-sec-publisher` |
| `SEC_HOMELAB_READ_TOKEN` | read of `runcove/ream-homelab` only |

`sec-private-check.sh repo` also requires artifacts-sec's collaborators to be exactly
`artifacts-maintainer` (write), `security` (read) and `artifacts-sec-checker` (owner: the forge
reports a collaborator's effective role, and the checker owns `runcove-sec`); that list
(`EXPECTED_COLLABORATORS` in the script) changes only with security's read.

### The checker account and the Owners-team check

`SEC_CHECK_TOKEN` belongs to `artifacts-sec-checker`, which is an owner of `runcove-sec` because the
forge lists a repository's runners only to its owners. Security approved that ownership on these
conditions, and each must hold for as long as it is an owner:

- **It cannot sign in; the token is the only way in.** No password anyone knows (a random one,
  never stored), no SSH or GPG keys, no two-factor set up, no OAuth2 applications, and no token
  but the vaulted one. Its email address is one only the operator controls, so a password reset
  cannot hand anyone a login. homelab reads all of this back with its forge access script's
  `--audit-logins`. Not "login prohibited": on this forge (Forgejo 17.0.1) `prohibit_login`
  makes the API refuse the account's token, and the checks below read the API, so the checker
  could not run (homelab tried it on 11 Nov 2026 and set it back).
- **Its single token is read-only**, with exactly the scopes `read:organization` and
  `read:repository` and no other (`sec-private-check.sh repo` proves this first, by probing, and
  stops if not). It has no other token.
- **The token lives in two places only:** the vault, and artifacts-sec's Actions secrets (as
  `SEC_CHECK_TOKEN`). Never on an office VM, never in a file or on a command line.
- **The accepted risk:** a leaked token can read every repository in `runcove-sec`. That is
  accepted because the org holds only security's repositories (the embargoed builds). If anything
  else moves into `runcove-sec`, revisit this before it moves.

Every `repo` run checks the ownership itself: it lists `runcove-sec`'s teams
(`GET /orgs/{org}/teams`), finds the Owners team, and lists its members
(`GET /teams/{id}/members`); both need only `read:organization` and membership of the Owners team
itself (org membership is not enough for the members list), which the checker has as an owner. The members must
be exactly the operator's forge account `jeremy` and `artifacts-sec-checker` (`EXPECTED_OWNERS` in
the script, a fixed list never read from the forge; it changes only with security's read). The check
turns red, and the build stops, when:

- an owner is added, or an expected one is removed;
- there is no Owners team, it lacks owner permission, or another team has owner permission;
- the teams or members list is refused (a missing scope or membership is named), is not a list of
  teams or accounts, names an account twice, or is not one whole page (its `X-Total-Count` does
  not match what was listed, or is missing);
- any other answer than 200 comes back.

Not a secret, set by homelab in the dedicated runner's own config (`runner.envs`), never in a
workflow or a local action (`workflow-runner-guard.sh` refuses a workflow, or an `action.yml` or
`action.yaml`, that names it). The runner-list check accepts only a name that is one of the organization's
own runners carrying the job's label:

| Name | What it is |
|---|---|
| `SEC_RUNNER_NAME` | the name each dedicated runner was registered with (the operator keeps the list). Forgejo's runner tells a job nothing that names the runner (it sets only `RUNNER_OS`, `RUNNER_ARCH`, `RUNNER_TEMP`, `RUNNER_TOOL_CACHE`), so this is how the check knows which runner took it; without it `repo` fails |

**After any Forgejo upgrade**, re-list the token scope categories in the new version's
`routers/api/v1/api.go` and `models/auth/access_token_scope.go`, and compare them with the probes in
`sec-private-check.sh` (written against 17.0.1). A changed refusal message already makes the check
fail closed, but a new category, or a route newly behind `misc`, is only caught if a probe reaches
it: add a probe for each one before the next embargoed run. Re-read the runner list too
(`GET /repos/{owner}/{repo}/actions/runners?visible=true`: its permission in `api.go`, its filter in
`models/actions/runner.go` `FindRunnerOptions`, its fields in `modules/structs/repo_actions.go`),
and check that the runner still sets no variable naming itself. Re-read the two Owners-team
calls as well (`/orgs/{org}/teams` and `/teams/{teamid}/members` in `api.go`, `ListTeams` and
`GetTeamMembers` in `routers/api/v1/org/team.go`, and `IsOrganizationOwner` in
`models/organization/org_user.go`).

Needed before first use: the private org `runcove-sec` (the name is `SEC_REGISTRY_OWNER` in
sec.yml), the accounts above, and the runners dedicated to this repo: one carrying the label `runcove-sec` (privileged, for the
image build only) and one carrying `runcove-sec-unpriv` (unprivileged, every other job), each on a
machine that runs no other jobs (never a shared label: see the comment above `jobs:` in sec.yml),
registered to the org `runcove-sec` (not to the repo, not global) with its one label, and with
`SEC_RUNNER_NAME` in its `runner.envs`.

## The working clone

Embargoed work happens in its own clone whose ONLY remote is the private repo, never in the
everyday clone whose `origin` is the public repo:

```bash
git clone https://git.tenfourty.site/runcove-sec/artifacts-sec.git artifacts-sec-work
# or over ssh: git clone git@git-ssh.tenfourty.site:runcove-sec/artifacts-sec.git artifacts-sec-work
cd artifacts-sec-work
lefthook install        # the pre-push guard below lives in this clone's hooks, nowhere else
git remote -v           # exactly one remote, origin, and it is runcove-sec/artifacts-sec
git switch -c sec/<topic>
```

- Never add the public repo (or the GitHub mirror) as a remote of this clone. To bring public
  `main` in, fetch it by URL and merge: `git fetch https://git.tenfourty.site/runcove/artifacts.git main`,
  then `git merge FETCH_HEAD`. Fetching publishes nothing. At release, the same one-off URL on the
  push, `main` only: `PUSH_COUNT=<n> git push https://git.tenfourty.site/runcove/artifacts.git main:main`.
- Never prove a fix on a public `ci/<name>` branch. A pushed branch of `runcove/artifacts` is public
  at once, and its CI log and the commits on it with it. The tests run in artifacts-sec's own CI
  (a push of `sec/*` runs the test jobs in `sec.yml`).
- The pre-push guard `scripts/guards/sec-branch-guard.sh` backs this up: it refuses any push of a
  `sec/*` branch or tag (in any letter case, by either side of the refspec, and `HEAD` while on a
  `sec/` branch) to a remote that is not exactly `runcove-sec/artifacts-sec` on the forge: https
  on `git.tenfourty.site`, or ssh (`ssh://` or `git@host:owner/repo`, port 22 or none) on
  `git.tenfourty.site` or on the forge's ssh endpoint `git-ssh.tenfourty.site`; https on
  `git-ssh.tenfourty.site`, any other host or lookalike of it, and any other port are refused. It also
  refuses by content: a push whose commit is, or builds on, the tip of a local `sec/*` branch or
  tag is refused under whatever name it goes (`HEAD~0:main`, `@{-1}:main`, a raw commit id, a
  `main` with the fix merged in). The repository's old name `runcove/artifacts-sec` does not pass
  either, because a redirect from it is not the repository. It has no override and it is only a
  back-up: the clone with one remote is the main control. Deleting a `sec/*` ref on a public remote is
  allowed (the clean-up of a leak) and prints a note: it undoes nothing, so tell security.
- **`runcove-sec/artifacts-sec` must never be made public**, nor given a mirror, nor forked, nor
  moved to an owner that is not private. Its CI run logs, PR threads and review comments hold the
  fix and the talk about it, and they would be public with the repo, whatever is done to the code
  afterwards. The privacy check fails a run on any of those.
