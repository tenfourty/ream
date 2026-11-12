#!/usr/bin/env bash
# sec-private-check.sh -- before an embargoed fix is tested, built or pushed anywhere, prove that
# the repository it lives in, and the registry owner its images go to, are private and that the
# repository has no push mirror (a push mirror carries every branch AND tag). Runbook:
# docs/agent/runbooks/embargoed-fix.md.
#
# It proves privacy from both sides: an anonymous caller must be refused, and an account allowed
# to read the settings must see `private`, no fork flag, no forks and an empty push-mirror list.
# For an image the order is: first an authenticated call proves the image EXISTS, then an
# anonymous pull must be refused. Any answer it did not expect (an error, a timeout, an unreadable
# body, a list it cannot read) is a FAILURE, never a pass: "could not tell" stops the build.
#
#   sec-private-check.sh repo <owner/name> [<registry-owner>]   before anything else, and after the last push
#   sec-private-check.sh image <owner/name>                      after a push: the image exists and is hidden
#   sec-private-check.sh --selftest                              offline, against fixtures
#
# A repository that is a fork, or has forks, is refused: a fork is a second copy whose visibility
# and branches are not this repository's, and the runbook has artifacts-sec created fresh.
#
# Why an image must be shown to exist first: the forge answers 404 to an anonymous caller for an
# image that is hidden AND for one that does not exist, and hands every caller an anonymous token,
# so "anonymous gets 404" proves nothing about a name that was mistyped.
#
# Before `repo` reads anything else it proves that SEC_CHECK_TOKEN's scopes are exactly
# read:organization,read:repository (check_scopes, below), and stops with exit 1 if not.
#
# `repo` also proves where the repository's jobs can run (judge_runners, below): of the runners the
# forge would hand its jobs to (GET /repos/{repo}/actions/runners?visible=true), every runner of
# its organization carries exactly ["runcove-sec"] or exactly ["runcove-sec-unpriv"], and at least
# one carries each; none is bound to the repository itself; no runner outside the organization
# carries a sec label (any label starting with runcove-sec); and the runner that took THIS job,
# named by SEC_RUNNER_NAME (set by the dedicated runner's own config under runner.envs, never by a
# workflow: scripts/guards/workflow-runner-guard.sh refuses one that names it), is one of the
# organization's and carries the label `repo` is given as its second argument (the job's runs-on;
# workflow-runner-guard.sh requires the two to match in sec.yml). Forgejo hands a job nothing that names its runner (forgejo/runner sets only
# RUNNER_OS, RUNNER_ARCH, RUNNER_TEMP, RUNNER_TOOL_CACHE), hence the variable. Listing runners
# needs read:repository (already required) and an account that OWNS the repository (Forgejo's
# reqOwner: an owner of its organization, or a site admin); a repository admin is refused 403.
#
# `repo` also proves who owns the repository's organization (owners_team and judge_owners, below):
# the members of its Owners team are exactly EXPECTED_OWNERS (the operator and the checker
# account). Listing them needs read:organization (already required) and organization membership.
#
# Tokens, in the environment, never on argv (they reach curl on stdin as a config line):
#   SEC_CHECK_TOKEN     `repo`: a token of an account that owns the repository (an owner of its
#                       organization: the forge lists runners only to owners, and shows push mirrors
#                       only to admins) and is a member of the registry owner, with the scopes
#                       read:organization,read:repository and no other.
#   SEC_RUNNER_NAME     `repo`: the name the runner running this job is registered under; set in
#                       the dedicated runner's config (runner.envs), not a secret.
#   SEC_REGISTRY_TOKEN  `image`: the registry token sec.yml already holds (package write on the
#                       private owner; read comes with it) of the account SEC_REGISTRY_USER
#                       (default artifacts-sec-publisher).
# SEC_FORGE: the forge's origin (default https://git.tenfourty.site).
#
# Exit 0: proved private. Exit 1: not private, or could not tell (each reason printed).
# Exit 2: --selftest failed. Exit 64: usage.
set -u

FORGE="${SEC_FORGE:-https://git.tenfourty.site}"
SEC_REGISTRY_USER="${SEC_REGISTRY_USER:-artifacts-sec-publisher}"
SELF="sec-private-check"
work=""
srv=""

say()  { printf '%s: %s\n' "$SELF" "$*"; }
bad()  { printf '%s: NOT PROVEN PRIVATE: %s\n' "$SELF" "$*" >&2; }

# One call; prints the HTTP code ("000" on a transport error). The token, when asked for, goes to
# curl on stdin as a config line, never on argv.
call() { # <anon|auth> <url> <outfile>
  local code
  if [ "$1" = auth ]; then
    code=$(printf 'header = "Authorization: token %s"\n' "$SEC_CHECK_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$3" -w '%{http_code}' "$2" 2>/dev/null)
  else
    code=$(curl -sS --max-time 30 -o "$3" -w '%{http_code}' "$2" 2>/dev/null)
  fi
  printf '%s' "${code:-000}"
}

# One authenticated write call with a fixed JSON body (never a secret); prints the HTTP code.
call_write() { # <method> <url> <json body> <outfile>
  local code
  code=$(printf 'header = "Authorization: token %s"\n' "$SEC_CHECK_TOKEN" \
    | curl -sS --config - --max-time 30 -X "$1" -H 'Content-Type: application/json' \
        --data-raw "$3" -o "$4" -w '%{http_code}' "$2" 2>/dev/null)
  printf '%s' "${code:-000}"
}

# The token's scopes. Forgejo has no endpoint where a token reads its own scopes, so they are
# proved by probing, against Forgejo 17.0.1 (GET /api/v1/version on the forge). Its scope
# middleware (routers/api/v1/permissions/token_requires_scopes.go) refuses a token that lacks a
# scope with exactly this answer, and the probes below match it literally:
#   403 {"message":"token does not have at least one of required scope(s): [<scope>]", ...}
# A write method (POST, PUT, PATCH, DELETE) needs write:<category>; write implies read, so a read
# probe refused for scope also proves the token has no write scope in that category.
REFUSAL_PREFIX='token does not have at least one of required scope(s): '

# The "message" of a forge error answer, on one line and at most 200 characters; nothing if none.
api_message() { # <file>
  python3 -I - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
m = d.get("message") if isinstance(d, dict) else None
if isinstance(m, str):
    print(" ".join(m.split())[:200])
PY
}

# The verdict on one scope probe (pure: the selftest drives it). Prints why the token is not
# proven to have (present) or not to have (absent) <scope>; nothing when it is.
#   absent:  only the forge's literal scope refusal for exactly [<scope>] passes. With unrouted-ok,
#            the router's own plain "404 page not found" (no such route on this forge: the scope
#            reaches nothing) passes too. Anything else is "has it" or "could not tell": a failure.
#   present: only 200 passes.
judge_scope() { # <absent|present> <scope> <code> <body file> [unrouted-ok]
  local want="$1" scope="$2" code="$3" body="$4" msg
  msg=$(api_message "$body")
  if [ "$want" = present ]; then
    [ "$code" != 200 ] || return 0
    if [ "$code" = 403 ] && [ "$msg" = "${REFUSAL_PREFIX}[${scope}]" ]; then
      echo "the token lacks $scope: the forge refused a call that needs it ($code: $msg)"
    elif [ "$code" = 403 ] && [ "${msg#token scope is limited to public}" != "$msg" ]; then
      echo "the token is public-only, which this check never accepts ($code: $msg)"
    else
      echo "could not tell whether the token has $scope: a call that needs it answered $code${msg:+ ($msg)}"
    fi
    return 0
  fi
  if [ "$code" = 403 ] && [ "$msg" = "${REFUSAL_PREFIX}[${scope}]" ]; then return 0; fi
  if [ "${5:-}" = unrouted-ok ] && [ "$code" = 404 ] && printf '404 page not found\n' | cmp -s - "$body"; then
    return 0
  fi
  case "$scope:$code" in
    *:200) echo "the token has $scope or wider: a call that needs it answered 200" ;;
    write:*:404|write:*:422)
      echo "the token has $scope or wider: a write probe got past the forge's scope check and was refused later ($code; it names nothing that exists, so nothing changed)" ;;
    *) echo "could not tell whether the token has $scope: a call that needs it answered $code${msg:+ ($msg)}" ;;
  esac
}

# Prove SEC_CHECK_TOKEN's scopes are exactly read:organization,read:repository. Prints one line
# per scope not proven; nothing when proven. <repo> is owner/name; <org> is the organization whose
# read proves read:organization (the registry owner, else the repository's owner).
# Every probe is one the forge refuses at its scope middleware before any handler, lookup or body
# binding runs: in Forgejo 17.0.1 a group's middlewares run in the order they are listed, before
# its routes' own handlers (modules/web/route.go, Group and wrapMiddlewareAndHandler), and each
# probe's group lists tokenRequiresScopes first (routers/api/v1/api.go).
# There is no probe for read:misc/write:misc: in 17.0.1 it is a placeholder no route requires
# (models/auth/access_token_scope.go), so it grants nothing.
check_scopes() { # <repo> <org>
  local repo="$1" org="$2" owner="${1%%/*}" w="$work/scope"
  # Proves no read:user (nor write:user): GET /user; the /user group checks the user scope first.
  judge_scope absent read:user "$(call auth "$FORGE/api/v1/user" "$w.user")" "$w.user"
  # Proves no read:notification (nor write:notification): GET /notifications.
  judge_scope absent read:notification \
    "$(call auth "$FORGE/api/v1/notifications?limit=1" "$w.notif")" "$w.notif"
  # Proves no read:package (nor write:package): GET /packages/<org>; the scope check comes before
  # the owner lookup.
  judge_scope absent read:package \
    "$(call auth "$FORGE/api/v1/packages/$org?limit=1" "$w.pkg")" "$w.pkg"
  # Proves no read:issue (nor write:issue): GET the repository's issues; the issue scope is the
  # /repos group's first middleware for issue routes, before the repository lookup.
  judge_scope absent read:issue \
    "$(call auth "$FORGE/api/v1/repos/$repo/issues?limit=1" "$w.issue")" "$w.issue"
  # Proves no read:admin (nor write:admin): GET /admin/cron; the admin scope is checked before
  # the site-admin check.
  judge_scope absent read:admin "$(call auth "$FORGE/api/v1/admin/cron" "$w.admin")" "$w.admin"
  # Proves no read:activitypub (nor write:activitypub): GET /activitypub/actor. Forgejo registers
  # these routes only with federation on; with it off the router answers a plain 404 and the
  # scope reaches nothing, so that answer is accepted too (and only that one).
  judge_scope absent read:activitypub \
    "$(call auth "$FORGE/api/v1/activitypub/actor" "$w.ap")" "$w.ap" unrouted-ok
  # Proves no write:repository: PATCH a repository that cannot exist ('~' is not allowed in a
  # repository name, so neither a repository nor a rename redirect has it), with a name the body
  # validation refuses. The repository scope is checked before the repository lookup; a token
  # that had the scope would get 404 from the lookup (and 422 from validation after it), never
  # a change.
  judge_scope absent write:repository \
    "$(call_write PATCH "$FORGE/api/v1/repos/$owner/sec-scope-probe~none" '{"name":"~"}' "$w.wrepo")" "$w.wrepo"
  # Proves no write:organization: PATCH an organization that cannot exist ('~' again), with a
  # visibility the body validation refuses. The organization scope is checked before the
  # organization lookup; a token with the scope would get 404 (or 422), never a change.
  judge_scope absent write:organization \
    "$(call_write PATCH "$FORGE/api/v1/orgs/sec-scope-probe~none" '{"visibility":"~"}' "$w.worg")" "$w.worg"
  # Proves read:repository: GET the repository itself (a public-only token is refused here too).
  judge_scope present read:repository "$(call auth "$FORGE/api/v1/repos/$repo" "$w.rrepo")" "$w.rrepo"
  # Proves read:organization: GET the organization.
  judge_scope present read:organization "$(call auth "$FORGE/api/v1/orgs/$org" "$w.rorg")" "$w.rorg"
}

# The repository's collaborators and each one's role, exactly: any extra, missing or changed one
# fails. This list changes only with security's read. "login:role", role as the forge's
# /collaborators/{login}/permission answers it (read, write, admin, owner).
EXPECTED_COLLABORATORS="artifacts-maintainer:write security:read artifacts-sec-checker:owner"

# The logins in a collaborator list, one per line (pure). Prints "BAD <reason>" instead when the
# list cannot be read or names an account the check cannot ask about safely.
collab_logins() { # <list json>
  python3 -I - "$1" <<'PY' 2>/dev/null || echo "BAD its answer could not be read"
import json, re, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list) or not all(isinstance(u, dict) and isinstance(u.get("login"), str) for u in d):
    print("BAD its answer was not a list of accounts")
    sys.exit(0)
for u in d:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", u["login"]):
        print("BAD it names an account whose name is not a valid login")
        sys.exit(0)
    print(u["login"])
PY
}

# The verdict on the collaborators (pure: the selftest drives it). <roles tsv> holds one line per
# listed login: "<login>\t<HTTP code of its permission call>\t<answer file>". Prints one line per
# problem; nothing when the collaborators are exactly EXPECTED_COLLABORATORS.
judge_collaborators() { # <repo> <list code> <list json> <roles tsv>
  local repo="$1"
  if [ "$2" != 200 ]; then
    echo "could not read the collaborators of $repo: the API answered $2"; return 0
  fi
  python3 -I - "$repo" "$3" "$4" "$EXPECTED_COLLABORATORS" <<'PY' 2>/dev/null || echo "could not read the collaborators of $repo: their answers could not be read"
import json, sys
repo, listf, tsv, expected = sys.argv[1:5]
want = dict(x.split(":", 1) for x in expected.split())
try:
    d = json.load(open(listf))
    listed = [u["login"] for u in d]
except Exception:
    print("could not read the collaborators of %s: the list was not a list of accounts" % repo)
    sys.exit(0)
if len(listed) != len(set(listed)):
    print("could not read the collaborators of %s: the list names an account twice" % repo)
have = {}
for line in open(tsv):
    login, code, f = line.rstrip("\n").split("\t")
    try:
        p = json.load(open(f)) if code == "200" else None
    except Exception:
        p = None
    ok = isinstance(p, dict) and isinstance(p.get("permission"), str) \
        and isinstance(p.get("user"), dict) and p["user"].get("login") == login
    if not ok:
        print("could not read the role of collaborator %s on %s: its permission call answered %s" % (login, repo, code))
        have[login] = None
    else:
        have[login] = p["permission"]
for login in listed:
    if login not in have:
        print("could not read the role of collaborator %s on %s: it was not asked" % (login, repo))
for login in sorted(set(listed) - set(want)):
    print("%s has a collaborator not on the expected list: %s (%s)" % (repo, login, have.get(login) or "role unread"))
for login in sorted(set(want) - set(listed)):
    print("%s is missing the expected collaborator %s (%s)" % (repo, login, want[login]))
for login in sorted(set(want) & set(listed)):
    if have.get(login) is not None and have[login] != want[login]:
        print("%s: collaborator %s is %s, expected %s" % (repo, login, have[login], want[login]))
PY
}

# Reads the collaborators of <repo> and each one's role; prints the verdict's problems.
check_collaborators() { # <repo>
  local repo="$1" code logins login n=0
  code=$(call auth "$FORGE/api/v1/repos/$repo/collaborators?limit=50" "$work/collab.json")
  : > "$work/collab.tsv"
  if [ "$code" = 200 ]; then
    logins=$(collab_logins "$work/collab.json")
    case "$logins" in BAD\ *)
      echo "could not read the collaborators of $repo: ${logins#BAD }"; return 0 ;; esac
    # A page holds at most 50 (the forge's default MAX_RESPONSE_ITEMS): a full page already has
    # more than the expected list, so it fails below as extra collaborators, never passes.
    for login in $logins; do
      n=$((n + 1))
      printf '%s\t%s\t%s\n' "$login" \
        "$(call auth "$FORGE/api/v1/repos/$repo/collaborators/$login/permission" "$work/perm.$n.json")" \
        "$work/perm.$n.json" >> "$work/collab.tsv"
    done
  fi
  judge_collaborators "$repo" "$code" "$work/collab.json" "$work/collab.tsv"
}

# The runners that can take a job of the repository, and the one that took this one. sec.yml's
# two labels: SEC_LABEL (privileged, the image job only) and SEC_UNPRIV_LABEL (every other job). A
# runner carrying either can be handed a job, and its SEC_* secrets. Any label that starts with
# SEC_LABEL, in any letter case, counts as a sec label.
SEC_LABEL="runcove-sec"
SEC_UNPRIV_LABEL="runcove-sec-unpriv"

# One authenticated GET that also keeps the response headers; prints the HTTP code.
call_headers() { # <url> <outfile> <headers file>
  local code
  code=$(printf 'header = "Authorization: token %s"\n' "$SEC_CHECK_TOKEN" \
    | curl -sS --config - --max-time 30 -D "$3" -o "$2" -w '%{http_code}' "$1" 2>/dev/null)
  printf '%s' "${code:-000}"
}

# The verdict on the runners (pure: the selftest drives it). Prints one line per problem; nothing
# when proven. The list is GET /repos/{repo}/actions/runners?visible=true (Forgejo 17.0.1,
# routers/api/v1/shared/runners.go ListRunners with models/actions/runner.go FindRunnerOptions):
# with visible=true it is every runner that can be handed a job of this repository: those bound
# to the repository (repo_id), those of its owner (owner_id, repo_id 0) and the instance's global
# ones (both 0). Runners of other owners never get its jobs, so they are not in it. Proven when:
#   - the answer is one whole page (X-Total-Count equals the number listed) of well-formed runners;
#   - every runner of the repository's owner carries exactly ["runcove-sec"] or exactly
#     ["runcove-sec-unpriv"], and at least one carries each;
#   - no runner is bound to the repository itself (the runbook registers none), no global runner
#     (owner_id 0) is listed at all, whatever its labels, and no runner that
#     is not the owner's carries a sec label: any label starting with "runcove-sec" in any letter
#     case, with or without a ":<anything>" suffix (labels match a job's runs-on exactly,
#     models/actions/run_job.go ItRunsOn, but a lookalike is refused too);
#   - <runner name> (the runner that took this job, SEC_RUNNER_NAME) names exactly one of the
#     owner's runners, and that runner carries <job label>, the label this job runs on.
# A 403 is never "no runners": a scope refusal names the scope the token lacks, and the forge's
# reqOwner refusal names the role the token's account lacks.
judge_runners() { # <repo> <code> <list json> <headers file> <repo json> <runner name> <job label>
  local repo="$1" code="$2" msg scope
  if [ "$code" != 200 ]; then
    msg=$(api_message "$3")
    if [ "$code" = 403 ] && [ "${msg#"$REFUSAL_PREFIX"}" != "$msg" ]; then
      scope="${msg#"$REFUSAL_PREFIX"}"; scope="${scope#[}"; scope="${scope%]}"
      echo "could not list the runners of $repo: SEC_CHECK_TOKEN lacks $scope, which listing them needs ($code: $msg)"
    elif [ "$code" = 403 ] && [ "$msg" = "user should be the owner of the repo" ]; then
      echo "could not list the runners of $repo: SEC_CHECK_TOKEN's account is not an owner of $repo (listing runners needs an owner of the repository: an owner of its organization; $code: $msg)"
    else
      echo "could not list the runners of $repo: the API answered $code${msg:+ ($msg)}"
    fi
    return 0
  fi
  python3 -I - "$repo" "$3" "$4" "$5" "$6" "${7:-}" "$SEC_LABEL" "$SEC_UNPRIV_LABEL" <<'PY' 2>/dev/null || echo "could not list the runners of $repo: its answer could not be read"
import json, re, sys
repo, listf, headf, repof, name, job_label, priv, unpriv = sys.argv[1:9]
LABELS = (priv, unpriv)
def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)
try:
    d = json.load(open(listf))
except Exception:
    print("could not list the runners of %s: its answer could not be read" % repo)
    sys.exit(0)
ok = isinstance(d, list) and all(
    isinstance(r, dict) and is_int(r.get("id")) and is_int(r.get("owner_id")) and is_int(r.get("repo_id"))
    and isinstance(r.get("name"), str) and isinstance(r.get("labels"), list)
    and all(isinstance(x, str) for x in r["labels"]) for r in d)
if not ok:
    print("could not list the runners of %s: its answer was not a list of runners" % repo)
    sys.exit(0)
total = None
for line in open(headf, encoding="latin-1"):
    k, _, v = line.partition(":")
    if k.strip().lower() == "x-total-count":
        total = v.strip()
if total is None or not total.isdigit():
    print("could not list the runners of %s: the answer has no X-Total-Count, so it may not be all of them" % repo)
    sys.exit(0)
if int(total) != len(d):
    print("could not list the runners of %s: X-Total-Count says %s but the page holds %d" % (repo, total, len(d)))
    sys.exit(0)
try:
    r = json.load(open(repof))
    repo_id, owner_id = r["id"], r["owner"]["id"]
    if not (is_int(repo_id) and is_int(owner_id) and owner_id > 0):
        raise ValueError
except Exception:
    print("could not judge the runners of %s: the repository's id or owner id could not be read" % repo)
    sys.exit(0)
def shown(x):
    return "%s (id %d)" % (x["name"] if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", x["name"]) else "a runner with an unprintable name", x["id"])
def carries(x):
    return any(l.lower().startswith(priv) for l in x["labels"])
own = [x for x in d if x["owner_id"] == owner_id and x["repo_id"] == 0]
for x in d:
    if x["repo_id"] != 0:
        print("runner %s is registered to %s itself; only the organization's own runners may serve it" % (shown(x), repo))
    elif x["owner_id"] == owner_id:
        if x["labels"] not in ([priv], [unpriv]):
            print("runner %s of the organization carries %s, not exactly [\"%s\"] or exactly [\"%s\"]" % (shown(x), json.dumps(x["labels"][:10]), priv, unpriv))
    elif carries(x):
        print("runner %s is not the organization's own runner but carries a sec label (%s), so it could take this repository's jobs" % (shown(x), priv))
    elif x["owner_id"] == 0:
        print("runner %s is a global runner of the instance, whatever its labels; only the organization's own runners may serve %s" % (shown(x), repo))
    else:
        print("runner %s belongs to another owner yet is listed for %s" % (shown(x), repo))
for lab in LABELS:
    if not any(x["labels"] == [lab] for x in own):
        print("no runner of the organization of %s carries %s: nothing proves which runner takes its jobs on it" % (repo, lab))
if job_label not in LABELS:
    print("the label this job runs on is %s, not one of %s" % (json.dumps(job_label[:64]), ", ".join(LABELS)))
if not name:
    print("SEC_RUNNER_NAME is not set: cannot tell which runner took this job (the dedicated runner's config sets it under runner.envs)")
elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
    print("SEC_RUNNER_NAME is not a runner name")
else:
    mine = [x for x in own if x["name"] == name]
    if not mine:
        print("this job's runner %s is not one of the organization's own runners of %s" % (name, repo))
    elif len(mine) > 1:
        print("the organization names %d runners %s: cannot tell which one took this job" % (len(mine), name))
    elif job_label in LABELS and mine[0]["labels"] != [job_label]:
        print("this job's runner %s carries %s, not the label this job runs on (%s)" % (name, json.dumps(mine[0]["labels"][:10]), job_label))
PY
}

# Lists the runners that can take a job of <repo> and judges them against this job's runner.
check_runners() { # <repo> <job label>
  local repo="$1" code
  code=$(call_headers "$FORGE/api/v1/repos/$repo/actions/runners?visible=true&limit=50" \
    "$work/runners.json" "$work/runners.h")
  [ -s "$work/r.json" ] || printf '{}' > "$work/r.json"
  judge_runners "$repo" "$code" "$work/runners.json" "$work/runners.h" "$work/r.json" "${SEC_RUNNER_NAME:-}" "$2"
}

# The owners of the repository's organization, exactly: an owner can read and change every
# repository, team, runner and secret of it, so any extra or missing one fails. A fixed list, never
# read from the forge: the operator's own forge account and the checker account, which owns the
# organization so that it may list the runners (judge_runners). This list changes only with
# security's read.
EXPECTED_OWNERS="jeremy artifacts-sec-checker"

# The total a list answer's X-Total-Count header gives, or nothing (pure).
total_count() { # <headers file>
  python3 -I - "$1" <<'PY' 2>/dev/null
import sys
for line in open(sys.argv[1], encoding="latin-1"):
    k, _, v = line.partition(":")
    if k.strip().lower() == "x-total-count" and v.strip().isdigit():
        print(v.strip())
PY
}

# Why a refused organization call was refused: names the scope the token lacks or the membership its
# account lacks, else the code (pure). <what> is "the teams of o" or "the Owners team of o".
org_refusal() { # <what> <code> <answer file>
  local what="$1" code="$2" msg scope
  msg=$(api_message "$3")
  if [ "$code" = 403 ] && [ "${msg#"$REFUSAL_PREFIX"}" != "$msg" ]; then
    scope="${msg#"$REFUSAL_PREFIX"}"; scope="${scope#[}"; scope="${scope%]}"
    echo "could not list $what: SEC_CHECK_TOKEN lacks $scope, which listing it needs ($code: $msg)"
  elif [ "$code" = 403 ] && { [ "$msg" = "Must be an organization member" ] || [ "$msg" = "Must be a team member" ]; }; then
    echo "could not list $what: SEC_CHECK_TOKEN's account is not a member of ${what##* of } with the reach it needs (it must be an owner; $code: $msg)"
  else
    echo "could not list $what: the API answered $code${msg:+ ($msg)}"
  fi
}

# The organization's Owners team (pure: the selftest drives it). The list is GET /orgs/{org}/teams
# (Forgejo 17.0.1, routers/api/v1/api.go:1305-1309: read:organization, reqOrgMembership;
# routers/api/v1/org/team.go:29 ListTeams sets X-Total-Count). The forge's owners are the members
# of the team named "Owners" (models/organization/org_user.go:45 IsOrganizationOwner, looked up by
# lower-case name), so it must be there with owner permission, and no other team may hold owner
# permission. Prints "ID <team id>" when found, else one line per problem.
owners_team() { # <org> <code> <teams json> <headers file>
  local org="$1"
  if [ "$2" != 200 ]; then org_refusal "the teams of $org" "$2" "$3"; return 0; fi
  python3 -I - "$org" "$3" "$(total_count "$4")" <<'PY' 2>/dev/null || echo "could not list the teams of $org: its answer could not be read"
import json, re, sys
org, listf, total = sys.argv[1:4]
def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)
try:
    d = json.load(open(listf))
except Exception:
    print("could not list the teams of %s: its answer could not be read" % org)
    sys.exit(0)
if not (isinstance(d, list) and all(isinstance(t, dict) and is_int(t.get("id")) and isinstance(t.get("name"), str)
                                    and isinstance(t.get("permission"), str) for t in d)):
    print("could not list the teams of %s: its answer was not a list of teams" % org)
    sys.exit(0)
if not total:
    print("could not list the teams of %s: the answer has no X-Total-Count, so it may not be all of them" % org)
    sys.exit(0)
if int(total) != len(d):
    print("could not list the teams of %s: X-Total-Count says %s but the page holds %d" % (org, total, len(d)))
    sys.exit(0)
def shown(t):
    return "%s (id %d)" % (t["name"] if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", t["name"]) else "a team with an unprintable name", t["id"])
owners = [t for t in d if t["name"].lower() == "owners"]
bad = False
for t in d:
    if t["permission"] == "owner" and t not in owners:
        print("%s: team %s has owner permission; only the Owners team may" % (org, shown(t))); bad = True
if len(owners) != 1:
    print("%s has no Owners team, so its owners cannot be read" % org if not owners
          else "%s names %d Owners teams" % (org, len(owners)))
elif owners[0]["permission"] != "owner":
    print("%s: the Owners team has %s, not owner permission" % (org, owners[0]["permission"][:20]))
elif not bad:
    print("ID %d" % owners[0]["id"])
PY
}

# The verdict on the Owners team's members (pure: the selftest drives it). The list is
# GET /teams/{id}/members (Forgejo 17.0.1, routers/api/v1/api.go:1348-1353 and 1365:
# read:organization, reqTeamMembership, which an organization owner passes;
# routers/api/v1/org/team.go:376 GetTeamMembers sets X-Total-Count to the team's member count).
# Prints one line per problem; nothing when the members are exactly EXPECTED_OWNERS.
judge_owners() { # <org> <code> <members json> <headers file>
  local org="$1"
  if [ "$2" != 200 ]; then org_refusal "the Owners team of $org" "$2" "$3"; return 0; fi
  python3 -I - "$org" "$3" "$(total_count "$4")" "$EXPECTED_OWNERS" <<'PY' 2>/dev/null || echo "could not list the Owners team of $org: its answer could not be read"
import json, re, sys
org, listf, total, expected = sys.argv[1:5]
try:
    d = json.load(open(listf))
except Exception:
    print("could not list the Owners team of %s: its answer could not be read" % org)
    sys.exit(0)
if not (isinstance(d, list) and all(isinstance(u, dict) and isinstance(u.get("login"), str)
                                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", u["login"]) for u in d)):
    print("could not list the Owners team of %s: its answer was not a list of accounts" % org)
    sys.exit(0)
if not total:
    print("could not list the Owners team of %s: the answer has no X-Total-Count, so it may not be all of them" % org)
    sys.exit(0)
if int(total) != len(d):
    print("could not list the Owners team of %s: X-Total-Count says %s but the page holds %d" % (org, total, len(d)))
    sys.exit(0)
listed = [u["login"] for u in d]
if len(listed) != len(set(listed)):
    print("could not list the Owners team of %s: the list names an account twice" % org)
    sys.exit(0)
want = set(expected.split())
for login in sorted(set(listed) - want):
    print("%s has an owner not on the expected list: %s" % (org, login))
for login in sorted(want - set(listed)):
    print("%s is missing the expected owner %s" % (org, login))
PY
}

# Reads <org>'s teams, then its Owners team's members; prints the verdict's problems.
check_owners() { # <org>
  local org="$1" code team
  code=$(call_headers "$FORGE/api/v1/orgs/$org/teams?limit=50" "$work/teams.json" "$work/teams.h")
  team=$(owners_team "$org" "$code" "$work/teams.json" "$work/teams.h")
  case "$team" in
    "ID "*) ;;
    *) printf '%s\n' "$team"; return 0 ;;
  esac
  code=$(call_headers "$FORGE/api/v1/teams/${team#ID }/members?limit=50" "$work/owners.json" "$work/owners.h")
  judge_owners "$org" "$code" "$work/owners.json" "$work/owners.h"
}

# A JSON check by name: prints the problem, or nothing. <file> <repo|mirrors|owner|tags> [<name>]
json_ok() {
  python3 - "$@" <<'PY' 2>/dev/null || printf 'its answer could not be read'
import json, sys
path, kind = sys.argv[1], sys.argv[2]
name = sys.argv[3] if len(sys.argv) > 3 else ""
d = json.load(open(path))
problems = []
if kind == "repo":
    if not isinstance(d, dict):
        problems.append("its answer was not an object")
    else:
        # Every field must be present and exactly right: a missing one is a failure, not a pass.
        if d.get("full_name") != name:
            problems.append("it names another repository")
        if d.get("private") is not True:
            problems.append("it is not shown as private")
        if d.get("mirror") is not False:
            problems.append("it is not shown as a non-mirror")
        if d.get("fork") is not False:
            problems.append("it is a fork, or its fork flag is missing")
        n = d.get("forks_count")
        if isinstance(n, bool) or not isinstance(n, int):
            problems.append("its fork count is missing")
        elif n != 0:
            problems.append("it has %d fork(s)" % n)
elif kind == "mirrors":
    if not (isinstance(d, list) and len(d) == 0):
        problems.append("its answer was not the one expected")
elif kind == "owner":
    if not (isinstance(d, dict) and d.get("username") == name and d.get("visibility") == "private"):
        problems.append("its answer was not the one expected")
elif kind == "tags":
    t = d.get("tags") if isinstance(d, dict) else None
    if not (isinstance(t, list) and len(t) > 0 and all(isinstance(x, str) for x in t)):
        problems.append("it lists no tags")
else:
    problems.append("its answer was not the one expected")
if problems:
    print("; ".join(problems))
PY
}

# The verdict on one repository from the answers gathered (pure: the selftest drives it).
# Prints one line per reason it is not proven private; prints nothing when it is.
judge_repo() { # <repo> <anon api code> <anon git code> <auth code> <auth json> <mirrors code> <mirrors json>
  local repo="$1" p
  case "$2" in
    404) ;;
    200) echo "$repo answers an anonymous API read (it is public)" ;;
    *)   echo "could not tell whether $repo is public: an anonymous API read answered $2" ;;
  esac
  case "$3" in
    401|404) ;;
    200) echo "$repo answers an anonymous git fetch (it is public)" ;;
    *)   echo "could not tell whether $repo can be fetched anonymously: it answered $3" ;;
  esac
  if [ "$4" != 200 ]; then
    echo "could not read the settings of $repo: the API answered $4 (is SEC_CHECK_TOKEN set and allowed?)"
  else
    p=$(json_ok "$5" repo "$repo")
    [ -z "$p" ] || echo "$repo is not shown as a private, non-mirror, non-fork repository with no forks: $p"
  fi
  if [ "$6" != 200 ]; then
    echo "could not read the push mirrors of $repo: the API answered $6 (the token's account needs admin read)"
  else
    p=$(json_ok "$7" mirrors)
    [ -z "$p" ] || echo "$repo has a push mirror, or its mirror list could not be read: $p"
  fi
}

judge_owner() { # <owner> <anon org code> <anon packages code> <auth code> <auth json>
  local owner="$1" p
  case "$2" in
    404) ;;
    200) echo "registry owner $owner answers an anonymous read (it is public)" ;;
    *)   echo "could not tell whether registry owner $owner is public: it answered $2" ;;
  esac
  case "$3" in
    401|403|404) ;;
    200) echo "the packages of $owner answer an anonymous read" ;;
    *)   echo "could not tell whether the packages of $owner are public: they answered $3" ;;
  esac
  if [ "$4" != 200 ]; then
    echo "could not read registry owner $owner: the API answered $4"
  else
    p=$(json_ok "$5" owner "$owner")
    [ -z "$p" ] || echo "registry owner $owner is not shown as private: $p"
  fi
}

# <image> <authenticated token code> <authenticated tags code> <tags json>: prints why the image
# is not shown to exist; nothing when an authenticated caller lists tags for it. Only after this
# is empty does an anonymous refusal (below) mean "hidden" and not "no such name".
judge_exists() {
  local image="$1" p
  case "$2" in
    200) ;;
    *) echo "could not prove $image exists: the registry token endpoint answered $2 to the registry account (is SEC_REGISTRY_TOKEN set and allowed?)"; return 0 ;;
  esac
  case "$3" in
    200) p=$(json_ok "$4" tags)
         [ -z "$p" ] || echo "could not prove $image exists: the authenticated tag list of $image is not usable ($p)" ;;
    404) echo "$image does not exist: the authenticated tag list answered 404 (a mistyped name would also look hidden to an anonymous caller)" ;;
    *)   echo "could not prove $image exists: the authenticated tag list answered $3" ;;
  esac
}

# <image> <anon token code> <anon tags code>: hidden when no anonymous pull can list its tags.
# Call it only for an image judge_exists accepted: its 404 is "hidden" only for a name that exists.
judge_image() {
  case "$2" in
    401|403) return 0 ;;
    200) ;;
    *) echo "could not tell whether $1 can be pulled anonymously: the token endpoint answered $2"; return 0 ;;
  esac
  case "$3" in
    401|403|404) ;;
    200) echo "$1 can be pulled by anyone" ;;
    *)   echo "could not tell whether $1 can be pulled anonymously: it answered $3" ;;
  esac
}

valid_name() { printf '%s' "$1" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*'; }

finish() { # <reasons> <what>
  if [ -n "$1" ]; then
    printf '%s\n' "$1" | while IFS= read -r line; do bad "$line"; done
    exit 1
  fi
  say "PROVEN PRIVATE: $2"
  exit 0
}

live_repo() { # <repo> <job label> [<owner>]
  local repo="$1" label="$2" owner="${3:-}" reasons
  valid_name "$repo" || { echo "usage: bad repository name" >&2; exit 64; }
  [ "$label" = "$SEC_LABEL" ] || [ "$label" = "$SEC_UNPRIV_LABEL" ] \
    || { echo "usage: the runner label must be $SEC_LABEL or $SEC_UNPRIV_LABEL" >&2; exit 64; }
  [ -z "$owner" ] || printf '%s' "$owner" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._-]*' \
    || { echo "usage: bad registry owner" >&2; exit 64; }
  [ -n "${SEC_CHECK_TOKEN:-}" ] || { bad "SEC_CHECK_TOKEN is not set: cannot read the settings"; exit 1; }
  # First, before anything else is read: the token's scopes are exactly the two it needs.
  reasons=$(check_scopes "$repo" "${owner:-${repo%%/*}}")
  if [ -n "$reasons" ]; then
    reasons=$(printf '%s\n' "$reasons" | sed 's/^/SEC_CHECK_TOKEN scopes: /')
    finish "$(printf 'SEC_CHECK_TOKEN must have exactly the scopes read:organization,read:repository; nothing else was checked\n%s' "$reasons")" ""
  fi
  reasons=$(judge_repo "$repo" \
    "$(call anon "$FORGE/api/v1/repos/$repo" "$work/a")" \
    "$(call anon "$FORGE/$repo.git/info/refs?service=git-upload-pack" "$work/g")" \
    "$(call auth "$FORGE/api/v1/repos/$repo" "$work/r.json")" "$work/r.json" \
    "$(call auth "$FORGE/api/v1/repos/$repo/push_mirrors?limit=50" "$work/m.json")" "$work/m.json")
  reasons=$(printf '%s\n%s' "$reasons" "$(check_collaborators "$repo")" | sed '/^$/d')
  reasons=$(printf '%s\n%s' "$reasons" "$(check_runners "$repo" "$label")" | sed '/^$/d')
  reasons=$(printf '%s\n%s' "$reasons" "$(check_owners "${repo%%/*}")" | sed '/^$/d')
  if [ -n "$owner" ]; then
    reasons=$(printf '%s\n%s' "$reasons" "$(judge_owner "$owner" \
      "$(call anon "$FORGE/api/v1/orgs/$owner" "$work/o")" \
      "$(call anon "$FORGE/api/v1/packages/$owner" "$work/p")" \
      "$(call auth "$FORGE/api/v1/orgs/$owner" "$work/o.json")" "$work/o.json")" | sed '/^$/d')
  fi
  finish "$reasons" "$repo (no push mirror)${owner:+ and registry owner $owner}; its jobs reach only $SEC_LABEL and $SEC_UNPRIV_LABEL runners of its owner, and this job ran on runner ${SEC_RUNNER_NAME:-}, which carries $label; the owners of ${repo%%/*} are exactly $EXPECTED_OWNERS"
}

# The registry's token endpoint as <anon|auth>: prints its HTTP code and leaves the bearer token in
# $work/bearer.<who> ("200-notoken" when it answered 200 without one). The registry account's
# credentials go to curl on stdin as a config line, never on argv.
reg_login() { # <anon|auth> <image>
  local who="$1" image="$2" code url
  url="$FORGE/v2/token?service=container_registry&scope=repository:$image:pull"
  rm -f "$work/bearer.$who"
  if [ "$who" = auth ]; then
    code=$(printf 'user = "%s:%s"\n' "$SEC_REGISTRY_USER" "$SEC_REGISTRY_TOKEN" \
      | curl -sS --config - --max-time 30 -o "$work/login.$who" -w '%{http_code}' "$url" 2>/dev/null)
  else
    code=$(curl -sS --max-time 30 -o "$work/login.$who" -w '%{http_code}' "$url" 2>/dev/null)
  fi
  code="${code:-000}"
  if [ "$code" = 200 ]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token",""))' "$work/login.$who" \
      > "$work/bearer.$who" 2>/dev/null || : > "$work/bearer.$who"
    [ -n "$(tr -d '[:space:]' < "$work/bearer.$who")" ] || code="200-notoken"
  fi
  printf '%s' "$code"
}

# The image's tag list with the bearer token of <who>; prints the HTTP code, body to <outfile>.
reg_tags() { # <anon|auth> <image> <outfile>
  local code
  code=$(printf 'header = "Authorization: Bearer %s"\n' "$(cat "$work/bearer.$1")" \
    | curl -sS --config - --max-time 30 -o "$3" -w '%{http_code}' "$FORGE/v2/$2/tags/list" 2>/dev/null)
  printf '%s' "${code:-000}"
}

live_image() { # <owner/name>
  local image="$1" tcode tags reasons
  valid_name "$image" || { echo "usage: bad image name" >&2; exit 64; }
  printf '%s' "$SEC_REGISTRY_USER" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._-]*' \
    || { echo "usage: bad registry user" >&2; exit 64; }
  [ -n "${SEC_REGISTRY_TOKEN:-}" ] || { bad "SEC_REGISTRY_TOKEN is not set: cannot prove $image exists"; exit 1; }
  # 1. It exists: an authenticated call lists its tags. Not found, or any doubt, stops here.
  tcode=$(reg_login auth "$image")
  tags=000
  [ "$tcode" != 200 ] || tags=$(reg_tags auth "$image" "$work/tags.json")
  reasons=$(judge_exists "$image" "$tcode" "$tags" "$work/tags.json")
  [ -z "$reasons" ] || finish "$reasons" ""
  # 2. It is hidden: an anonymous pull must be refused.
  tcode=$(reg_login anon "$image")
  tags=401
  [ "$tcode" != 200 ] || tags=$(reg_tags anon "$image" "$work/anon-tags.json")
  reasons=$(judge_image "$image" "$tcode" "$tags")
  finish "$reasons" "image $image (exists, and no anonymous pull)"
}

selftest() {
  local fails=0 got
  expect_has() { # <name> <text the reasons must contain> <got>
    if printf '%s' "$3" | grep -qF -- "$2"; then
      printf '  ok    %s\n' "$1"
    else
      printf '  FAIL  %s (got: %s)\n' "$1" "$3"; fails=$((fails + 1))
    fi
  }
  expect() { # <name> <want: empty|nonempty> <got>
    if { [ "$2" = empty ] && [ -z "$3" ]; } || { [ "$2" = nonempty ] && [ -n "$3" ]; }; then
      printf '  ok    %s\n' "$1"
    else
      printf '  FAIL  %s (got: %s)\n' "$1" "$3"; fails=$((fails + 1))
    fi
  }
  local d="$work"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":false,"forks_count":0}' > "$d/priv.json"
  printf '{"full_name":"o/r","private":false,"mirror":false,"fork":false,"forks_count":0}' > "$d/pub.json"
  printf '{"full_name":"o/r","mirror":false,"fork":false,"forks_count":0}' > "$d/nofield.json"
  printf '{"full_name":"o/other","private":true,"mirror":false,"fork":false,"forks_count":0}' > "$d/other.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":true,"forks_count":0}' > "$d/isfork.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":false,"forks_count":2}' > "$d/hasforks.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"forks_count":0}' > "$d/nofork.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":false}' > "$d/nocount.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":false,"forks_count":"0"}' > "$d/strcount.json"
  printf '{"full_name":"o/r","private":true,"mirror":false,"fork":"false","forks_count":0}' > "$d/strfork.json"
  printf '{"tags":["a1b2c3"]}' > "$d/tags.json"
  printf '{"name":"o/r","tags":[]}' > "$d/notags.json"
  printf '{"tags":null}' > "$d/nulltags.json"
  printf '[]' > "$d/none.json"
  printf '[{"remote_name":"github"}]' > "$d/one.json"
  printf '{"message":"x"}' > "$d/obj.json"
  printf 'not json' > "$d/junk.json"
  printf '{"username":"sec","visibility":"private"}' > "$d/oprv.json"
  printf '{"username":"sec","visibility":"limited"}' > "$d/olim.json"

  expect "private, no mirror: proven"            empty    "$(judge_repo o/r 404 401 200 "$d/priv.json" 200 "$d/none.json")"
  expect "anonymous API read answers"            nonempty "$(judge_repo o/r 200 401 200 "$d/priv.json" 200 "$d/none.json")"
  expect "anonymous git fetch answers"           nonempty "$(judge_repo o/r 404 200 200 "$d/priv.json" 200 "$d/none.json")"
  expect "anonymous read errors (000)"           nonempty "$(judge_repo o/r 000 401 200 "$d/priv.json" 200 "$d/none.json")"
  expect "settings say public"                   nonempty "$(judge_repo o/r 404 401 200 "$d/pub.json" 200 "$d/none.json")"
  expect "settings lack the private field"       nonempty "$(judge_repo o/r 404 401 200 "$d/nofield.json" 200 "$d/none.json")"
  expect "settings name another repository"      nonempty "$(judge_repo o/r 404 401 200 "$d/other.json" 200 "$d/none.json")"
  expect "settings unreadable (401)"             nonempty "$(judge_repo o/r 404 401 401 "$d/priv.json" 200 "$d/none.json")"
  expect "settings body is junk"                 nonempty "$(judge_repo o/r 404 401 200 "$d/junk.json" 200 "$d/none.json")"
  expect "settings say it is a fork"             nonempty "$(judge_repo o/r 404 401 200 "$d/isfork.json" 200 "$d/none.json")"
  expect "settings say it has forks"             nonempty "$(judge_repo o/r 404 401 200 "$d/hasforks.json" 200 "$d/none.json")"
  expect "settings lack the fork field"          nonempty "$(judge_repo o/r 404 401 200 "$d/nofork.json" 200 "$d/none.json")"
  expect "settings lack the fork count"          nonempty "$(judge_repo o/r 404 401 200 "$d/nocount.json" 200 "$d/none.json")"
  expect "settings fork count is a string"       nonempty "$(judge_repo o/r 404 401 200 "$d/strcount.json" 200 "$d/none.json")"
  expect "settings fork flag is a string"        nonempty "$(judge_repo o/r 404 401 200 "$d/strfork.json" 200 "$d/none.json")"
  expect_has "the fork reason names the fork"    "it is a fork"    "$(judge_repo o/r 404 401 200 "$d/isfork.json" 200 "$d/none.json")"
  expect_has "the forks reason names the count"  "it has 2 fork(s)" "$(judge_repo o/r 404 401 200 "$d/hasforks.json" 200 "$d/none.json")"
  expect "a push mirror exists"                  nonempty "$(judge_repo o/r 404 401 200 "$d/priv.json" 200 "$d/one.json")"
  expect "mirrors refused (403, not admin)"      nonempty "$(judge_repo o/r 404 401 200 "$d/priv.json" 403 "$d/none.json")"
  expect "mirrors answer an object, not a list"  nonempty "$(judge_repo o/r 404 401 200 "$d/priv.json" 200 "$d/obj.json")"
  expect "mirrors body is junk"                  nonempty "$(judge_repo o/r 404 401 200 "$d/priv.json" 200 "$d/junk.json")"
  expect "owner private: proven"                 empty    "$(judge_owner sec 404 404 200 "$d/oprv.json")"
  expect "owner limited (any signed-in user)"    nonempty "$(judge_owner sec 404 404 200 "$d/olim.json")"
  expect "owner public to anyone"                nonempty "$(judge_owner sec 200 404 200 "$d/oprv.json")"
  expect "owner packages readable"               nonempty "$(judge_owner sec 404 200 200 "$d/oprv.json")"
  expect "owner unreadable with the token"       nonempty "$(judge_owner sec 404 404 404 "$d/oprv.json")"
  expect "exists: tags listed"                   empty    "$(judge_exists sec/x 200 200 "$d/tags.json")"
  expect "exists: not found (404)"               nonempty "$(judge_exists sec/x 200 404 "$d/tags.json")"
  expect_has "exists: 404 says it does not exist" "does not exist" "$(judge_exists sec/x 200 404 "$d/tags.json")"
  expect "exists: token endpoint refuses (401)"  nonempty "$(judge_exists sec/x 401 000 "$d/tags.json")"
  expect "exists: token endpoint errors (000)"   nonempty "$(judge_exists sec/x 000 000 "$d/tags.json")"
  expect "exists: token without a token body"    nonempty "$(judge_exists sec/x 200-notoken 000 "$d/tags.json")"
  expect "exists: tag list errors (500)"         nonempty "$(judge_exists sec/x 200 500 "$d/tags.json")"
  expect "exists: tag list errors (000)"         nonempty "$(judge_exists sec/x 200 000 "$d/tags.json")"
  expect "exists: tag list is empty"             nonempty "$(judge_exists sec/x 200 200 "$d/notags.json")"
  expect "exists: tags is null"                  nonempty "$(judge_exists sec/x 200 200 "$d/nulltags.json")"
  expect "exists: body is junk"                  nonempty "$(judge_exists sec/x 200 200 "$d/junk.json")"
  expect "image: no anonymous token"             empty    "$(judge_image sec/x 401 401)"
  expect "image: anonymous token, tags refused"  empty    "$(judge_image sec/x 200 401)"
  expect "image: anonymous tags list answers"    nonempty "$(judge_image sec/x 200 200)"
  expect "image: token endpoint errors"          nonempty "$(judge_image sec/x 000 401)"
  expect "image: tags list errors"               nonempty "$(judge_image sec/x 200 500)"
  expect "image: anonymous token without a token" nonempty "$(judge_image sec/x 200-notoken 401)"
  # Scope probes, one verdict at a time (the forge's literal answers).
  printf '{"message":"token does not have at least one of required scope(s): [read:user]","url":"u"}' > "$d/ref-user.json"
  printf '{"message":"token does not have at least one of required scope(s): [read:issue]","url":"u"}' > "$d/ref-issue.json"
  printf '{"message":"token does not have at least one of required scope(s): [write:repository]","url":"u"}' > "$d/ref-wrepo.json"
  printf '{"message":"token does not have at least one of required scope(s): [read:repository]","url":"u"}' > "$d/ref-rrepo.json"
  printf '{"message":"token scope is limited to public repos","url":"u"}' > "$d/pubonly.json"
  printf '{"message":"The target couldn'"'"'t be found.","url":"u"}' > "$d/notfound.json"
  printf '{"message":"[Name]: AlphaDashDot","url":"u"}' > "$d/invalid.json"
  printf '404 page not found\n' > "$d/unrouted.txt"
  printf '<html>404</html>\n' > "$d/proxy404.txt"
  expect "scope absent: literal refusal"            empty    "$(judge_scope absent read:user 403 "$d/ref-user.json")"
  expect "scope absent: refusal names another scope" nonempty "$(judge_scope absent read:user 403 "$d/ref-issue.json")"
  expect "scope absent: 403 that is not a refusal"  nonempty "$(judge_scope absent read:user 403 "$d/junk.json")"
  expect "scope absent: 401"                         nonempty "$(judge_scope absent read:user 401 "$d/obj.json")"
  expect "scope absent: read answers 404"            nonempty "$(judge_scope absent read:user 404 "$d/notfound.json")"
  expect_has "scope absent: 404 is could not tell"   "could not tell" "$(judge_scope absent read:user 404 "$d/notfound.json")"
  expect_has "scope absent: 500 is could not tell"   "could not tell" "$(judge_scope absent read:user 500 "$d/obj.json")"
  expect_has "scope absent: 200 means it has it"     "has read:user" "$(judge_scope absent read:user 200 "$d/obj.json")"
  expect "scope absent: write refused for scope"     empty    "$(judge_scope absent write:repository 403 "$d/ref-wrepo.json")"
  expect_has "scope absent: write past scope (404)"  "has write:repository" "$(judge_scope absent write:repository 404 "$d/notfound.json")"
  expect_has "scope absent: write past scope (422)"  "has write:repository" "$(judge_scope absent write:repository 422 "$d/invalid.json")"
  expect "scope absent: unrouted 404 accepted"       empty    "$(judge_scope absent read:activitypub 404 "$d/unrouted.txt" unrouted-ok)"
  expect "scope absent: unrouted 404 needs the flag" nonempty "$(judge_scope absent read:activitypub 404 "$d/unrouted.txt")"
  expect "scope absent: another 404 body is not unrouted" nonempty "$(judge_scope absent read:activitypub 404 "$d/proxy404.txt" unrouted-ok)"
  expect "scope absent: JSON 404 is not unrouted"    nonempty "$(judge_scope absent read:activitypub 404 "$d/notfound.json" unrouted-ok)"
  expect "scope present: 200"                        empty    "$(judge_scope present read:repository 200 "$d/priv.json")"
  expect_has "scope present: refusal means lacks"    "lacks read:repository" "$(judge_scope present read:repository 403 "$d/ref-rrepo.json")"
  expect_has "scope present: public-only refused"    "public-only" "$(judge_scope present read:repository 403 "$d/pubonly.json")"
  expect_has "scope present: 404 is could not tell"  "could not tell" "$(judge_scope present read:repository 404 "$d/notfound.json")"
  expect_has "scope present: 000 is could not tell"  "could not tell" "$(judge_scope present read:repository 000 "$d/none.json")"
  # Collaborators: <name> <"login:role[:code]" ...> builds the list and the role answers.
  collab_fixture() {
    local name="$1" spec login role code; shift
    : > "$d/$name.tsv"
    printf '[' > "$d/$name.json"
    local sep=""
    for spec in "$@"; do
      login="${spec%%:*}"; role="${spec#*:}"; code="${role#*:}"; role="${role%%:*}"
      [ "$code" != "$role" ] || code=200
      printf '%s{"login":"%s","id":1}' "$sep" "$login" >> "$d/$name.json"; sep=","
      printf '{"permission":"%s","role_name":"%s","user":{"login":"%s"}}' "$role" "$role" "$login" > "$d/$name.$login.perm"
      printf '%s\t%s\t%s\n' "$login" "$code" "$d/$name.$login.perm" >> "$d/$name.tsv"
    done
    printf ']' >> "$d/$name.json"
  }
  collab_fixture cx artifacts-maintainer:write security:read artifacts-sec-checker:owner
  collab_fixture cextra artifacts-maintainer:write security:read artifacts-sec-checker:owner someone:read
  collab_fixture cmiss artifacts-maintainer:write artifacts-sec-checker:owner
  collab_fixture cchg artifacts-maintainer:admin security:read artifacts-sec-checker:owner
  collab_fixture cunread artifacts-maintainer:write security:read:403 artifacts-sec-checker:owner
  collab_fixture cckadmin artifacts-maintainer:write security:read artifacts-sec-checker:admin
  expect "collaborators: exactly the expected three"   empty "$(judge_collaborators o/r 200 "$d/cx.json" "$d/cx.tsv")"
  expect_has "collaborators: the checker only admin fails" "artifacts-sec-checker is admin, expected owner" "$(judge_collaborators o/r 200 "$d/cckadmin.json" "$d/cckadmin.tsv")"
  expect_has "collaborators: an extra one fails"       "not on the expected list: someone (read)" "$(judge_collaborators o/r 200 "$d/cextra.json" "$d/cextra.tsv")"
  expect_has "collaborators: a missing one fails"      "missing the expected collaborator security (read)" "$(judge_collaborators o/r 200 "$d/cmiss.json" "$d/cmiss.tsv")"
  expect_has "collaborators: a changed role fails"     "artifacts-maintainer is admin, expected write" "$(judge_collaborators o/r 200 "$d/cchg.json" "$d/cchg.tsv")"
  expect_has "collaborators: an unreadable role fails" "could not read the role of collaborator security" "$(judge_collaborators o/r 200 "$d/cunread.json" "$d/cunread.tsv")"
  expect_has "collaborators: list refused (403) fails" "could not read the collaborators" "$(judge_collaborators o/r 403 "$d/cx.json" "$d/cx.tsv")"
  expect_has "collaborators: list is junk"             "could not read the collaborators" "$(judge_collaborators o/r 200 "$d/junk.json" "$d/cx.tsv")"
  expect_has "collaborators: list is an object"        "could not read the collaborators" "$(judge_collaborators o/r 200 "$d/obj.json" "$d/cx.tsv")"
  expect "collaborators: list of logins read"          nonempty "$(collab_logins "$d/cx.json" | grep -vx 'BAD.*')"
  printf '[{"login":"a/../b"}]' > "$d/cbadname.json"
  expect_has "collaborators: an unsafe login is refused" "BAD" "$(collab_logins "$d/cbadname.json")"
  # Runners: <name> <X-Total-Count or "-" for none> <runner json objects...> builds a list answer.
  # The repository is id 7, owned by the org with id 3; a global runner has owner 0 and repo 0.
  printf '{"id":7,"full_name":"o/r","owner":{"id":3,"login":"o"}}' > "$d/rrepo.json"
  printf '{"id":7,"full_name":"o/r","owner":{"login":"o"}}' > "$d/rrepo-noowner.json"
  runner_fixture() {
    local name="$1" total="$2" sep="" r; shift 2
    printf '[' > "$d/$name.rl"
    for r in "$@"; do printf '%s%s' "$sep" "$r" >> "$d/$name.rl"; sep=","; done
    printf ']' >> "$d/$name.rl"
    if [ "$total" = - ]; then printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n' > "$d/$name.rh"
    else printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Total-Count: %s\r\n\r\n' "$total" > "$d/$name.rh"; fi
  }
  local SEC='{"id":11,"uuid":"u1","name":"dedicated-priv-1","owner_id":3,"repo_id":0,"labels":["runcove-sec"],"status":"active"}'
  local SEC2='{"id":12,"uuid":"u2","name":"dedicated-priv-2","owner_id":3,"repo_id":0,"labels":["runcove-sec"],"status":"idle"}'
  local UNP='{"id":21,"uuid":"u21","name":"dedicated-unpriv-1","owner_id":3,"repo_id":0,"labels":["runcove-sec-unpriv"],"status":"active"}'
  local GLOBAL='{"id":1,"uuid":"g1","name":"shared-1","owner_id":0,"repo_id":0,"labels":["docker-light"],"status":"idle"}'
  runner_fixture rok 2 "$SEC" "$UNP"
  runner_fixture rglobal 3 "$SEC" "$UNP" "$GLOBAL"
  runner_fixture rtwo 3 "$SEC" "$SEC2" "$UNP"
  runner_fixture rnone 0
  runner_fixture rnounp 1 "$SEC"
  runner_fixture rnopriv 1 "$UNP"
  runner_fixture rextra 2 "$UNP" '{"id":11,"uuid":"u1","name":"dedicated-priv-1","owner_id":3,"repo_id":0,"labels":["runcove-sec","docker-light"],"status":"active"}'
  runner_fixture rboth 3 "$SEC" "$UNP" '{"id":22,"uuid":"u22","name":"dedicated-both","owner_id":3,"repo_id":0,"labels":["runcove-sec","runcove-sec-unpriv"],"status":"idle"}'
  runner_fixture rother 3 "$SEC" "$UNP" '{"id":13,"uuid":"u3","name":"org-other","owner_id":3,"repo_id":0,"labels":["docker-light"],"status":"idle"}'
  runner_fixture rglobsec 3 "$SEC" "$UNP" '{"id":2,"uuid":"g2","name":"shared-2","owner_id":0,"repo_id":0,"labels":["docker-light","runcove-sec"],"status":"idle"}'
  runner_fixture rglobsecv 3 "$SEC" "$UNP" '{"id":2,"uuid":"g2","name":"shared-2","owner_id":0,"repo_id":0,"labels":["Runcove-Sec:docker://node"],"status":"idle"}'
  runner_fixture rglobunp 3 "$SEC" "$UNP" '{"id":2,"uuid":"g2","name":"shared-2","owner_id":0,"repo_id":0,"labels":["runcove-sec-unpriv"],"status":"idle"}'
  runner_fixture rgloblook 3 "$SEC" "$UNP" '{"id":2,"uuid":"g2","name":"shared-2","owner_id":0,"repo_id":0,"labels":["docker-light","runcove-sec-x"],"status":"idle"}'
  runner_fixture rorglook 3 "$SEC" "$UNP" '{"id":23,"uuid":"u23","name":"dedicated-look","owner_id":3,"repo_id":0,"labels":["runcove-sec-x"],"status":"idle"}'
  runner_fixture rrepob 3 "$SEC" "$UNP" '{"id":14,"uuid":"u4","name":"repo-bound","owner_id":0,"repo_id":7,"labels":["runcove-sec"],"status":"idle"}'
  runner_fixture rdup 3 "$SEC" "$UNP" '{"id":15,"uuid":"u5","name":"dedicated-priv-1","owner_id":3,"repo_id":0,"labels":["runcove-sec"],"status":"idle"}'
  runner_fixture rnocount - "$SEC" "$UNP"
  runner_fixture rmore 3 "$SEC" "$UNP"
  runner_fixture rnolabels 1 '{"id":11,"uuid":"u1","name":"dedicated-priv-1","owner_id":3,"repo_id":0,"labels":null,"status":"active"}'
  runner_fixture rnoowner 1 '{"id":11,"uuid":"u1","name":"dedicated-priv-1","repo_id":0,"labels":["runcove-sec"],"status":"active"}'
  runner_fixture rboolid 1 '{"id":11,"uuid":"u1","name":"dedicated-priv-1","owner_id":true,"repo_id":0,"labels":["runcove-sec"],"status":"active"}'
  printf '{"message":"user should be the owner of the repo","url":"u"}' > "$d/rnotowner.json"
  jr() { # <code> <fixture> [<runner name>] [<repo json>] [<job label>]
    judge_runners o/r "$1" "$d/$2.rl" "$d/$2.rh" "${4:-$d/rrepo.json}" "${3-dedicated-priv-1}" "${5-runcove-sec}"
  }
  expect "runners: the real shape, the image job on the privileged runner" empty "$(jr 200 rok)"
  expect "runners: the real shape, a job on the unprivileged runner"       empty "$(jr 200 rok dedicated-unpriv-1 "" runcove-sec-unpriv)"
  expect_has "runners: a global runner fails whatever its labels" "shared-1 (id 1) is a global runner of the instance" "$(jr 200 rglobal)"
  expect "runners: two privileged runners pass"              empty "$(jr 200 rtwo)"
  expect_has "runners: none at all fails"                    "no runner of the organization of o/r carries runcove-sec:" "$(jr 200 rnone)"
  expect_has "runners: no unprivileged runner fails"         "no runner of the organization of o/r carries runcove-sec-unpriv" "$(jr 200 rnounp)"
  expect_has "runners: no privileged runner fails"           "no runner of the organization of o/r carries runcove-sec:" "$(jr 200 rnopriv dedicated-unpriv-1 "" runcove-sec-unpriv)"
  expect_has "runners: an extra label fails"                 "dedicated-priv-1 (id 11) of the organization carries [\"runcove-sec\", \"docker-light\"]" "$(jr 200 rextra)"
  expect_has "runners: an org runner with both labels fails" "dedicated-both (id 22) of the organization carries [\"runcove-sec\", \"runcove-sec-unpriv\"]" "$(jr 200 rboth)"
  expect_has "runners: an org runner without one fails"      "org-other (id 13) of the organization carries [\"docker-light\"]" "$(jr 200 rother)"
  expect_has "runners: an org runner on a lookalike fails"   "dedicated-look (id 23) of the organization carries [\"runcove-sec-x\"]" "$(jr 200 rorglook)"
  expect_has "runners: a global runner with it fails"        "shared-2 (id 2) is not the organization's own runner but carries a sec label" "$(jr 200 rglobsec)"
  expect_has "runners: a lookalike label on a global fails"  "shared-2 (id 2) is not the organization's own runner but carries a sec label" "$(jr 200 rglobsecv)"
  expect_has "runners: a global runner with the unprivileged label fails" "shared-2 (id 2) is not the organization's own runner but carries a sec label" "$(jr 200 rglobunp dedicated-unpriv-1 "" runcove-sec-unpriv)"
  expect_has "runners: a global runner with runcove-sec-x fails" "shared-2 (id 2) is not the organization's own runner but carries a sec label" "$(jr 200 rgloblook)"
  expect_has "runners: a runner bound to the repo fails"     "repo-bound (id 14) is registered to o/r itself" "$(jr 200 rrepob)"
  expect_has "runners: this job's runner not listed fails"   "this job's runner nobody-sec is not" "$(jr 200 rok nobody-sec)"
  expect_has "runners: a job on unpriv that ran on the privileged runner fails" "this job's runner dedicated-priv-1 carries [\"runcove-sec\"], not the label this job runs on (runcove-sec-unpriv)" "$(jr 200 rok dedicated-priv-1 "" runcove-sec-unpriv)"
  expect_has "runners: the image job that ran on the unprivileged runner fails" "this job's runner dedicated-unpriv-1 carries [\"runcove-sec-unpriv\"], not the label this job runs on (runcove-sec)" "$(jr 200 rok dedicated-unpriv-1)"
  expect_has "runners: a job label that is no sec label fails" "the label this job runs on is \"docker-light\"" "$(jr 200 rok dedicated-priv-1 "" docker-light)"
  expect_has "runners: this job's runner named twice fails"  "names 2 runners dedicated-priv-1" "$(jr 200 rdup)"
  expect_has "runners: no runner name fails"                 "SEC_RUNNER_NAME is not set" "$(jr 200 rok "")"
  expect_has "runners: an unsafe runner name fails"          "SEC_RUNNER_NAME is not a runner name" "$(jr 200 rok 'a b')"
  expect_has "runners: no total count fails"                 "no X-Total-Count" "$(jr 200 rnocount)"
  expect_has "runners: more than one page fails"             "X-Total-Count says 3" "$(jr 200 rmore)"
  expect_has "runners: labels not a list fails"              "not a list of runners" "$(jr 200 rnolabels)"
  expect_has "runners: a missing owner_id fails"             "not a list of runners" "$(jr 200 rnoowner)"
  expect_has "runners: a boolean id fails"                   "not a list of runners" "$(jr 200 rboolid)"
  expect_has "runners: an object, not a list, fails"         "not a list of runners" "$(judge_runners o/r 200 "$d/obj.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: junk fails"                           "could not be read" "$(judge_runners o/r 200 "$d/junk.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: the repo's owner id unread fails"     "owner id" "$(jr 200 rok dedicated-priv-1 "$d/rrepo-noowner.json")"
  expect_has "runners: scope refusal names the scope"        "SEC_CHECK_TOKEN lacks read:repository" "$(judge_runners o/r 403 "$d/ref-rrepo.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: a scope refusal is never no runners"  "could not list" "$(judge_runners o/r 403 "$d/ref-rrepo.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: not an owner names the owner rule"    "is not an owner of o/r" "$(judge_runners o/r 403 "$d/rnotowner.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: another 403 is could not tell"        "could not list the runners of o/r: the API answered 403" "$(judge_runners o/r 403 "$d/junk.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: 404 is could not tell"                "could not list the runners of o/r: the API answered 404" "$(judge_runners o/r 404 "$d/notfound.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  expect_has "runners: 000 is could not tell"                "could not list the runners of o/r: the API answered 000" "$(judge_runners o/r 000 "$d/none.json" "$d/rok.rh" "$d/rrepo.json" dedicated-priv-1)"
  # The Owners team: <name> <X-Total-Count or -> <json array body> writes <name>.json and <name>.h.
  page_fixture() {
    printf '%s' "$3" > "$d/$1.json"
    if [ "$2" = - ]; then printf 'HTTP/1.1 200 OK\r\n\r\n' > "$d/$1.h"
    else printf 'HTTP/1.1 200 OK\r\nX-Total-Count: %s\r\n\r\n' "$2" > "$d/$1.h"; fi
  }
  local OWNERS='{"id":5,"name":"Owners","permission":"owner","includes_all_repositories":true}'
  local CHECK='{"id":6,"name":"check","permission":"read","includes_all_repositories":false}'
  page_fixture tok 2 "[$OWNERS,$CHECK]"
  page_fixture tnoowners 1 "[$CHECK]"
  page_fixture tsecondowner 2 "[$OWNERS,"'{"id":7,"name":"admins","permission":"owner","includes_all_repositories":true}'"]"
  page_fixture townersread 1 '[{"id":5,"name":"Owners","permission":"read","includes_all_repositories":true}]'
  page_fixture tmore 3 "[$OWNERS,$CHECK]"
  page_fixture tnocount - "[$OWNERS,$CHECK]"
  page_fixture tbadid 1 '[{"id":"5","name":"Owners","permission":"owner"}]'
  page_fixture mok 2 '[{"id":1,"login":"jeremy"},{"id":2,"login":"artifacts-sec-checker"}]'
  page_fixture mextra 3 '[{"id":1,"login":"jeremy"},{"id":2,"login":"artifacts-sec-checker"},{"id":3,"login":"someone"}]'
  page_fixture mmissing 1 '[{"id":1,"login":"jeremy"}]'
  page_fixture mswapped 2 '[{"id":1,"login":"jeremy"},{"id":4,"login":"artifacts-maintainer"}]'
  page_fixture mdup 2 '[{"id":1,"login":"jeremy"},{"id":1,"login":"jeremy"}]'
  page_fixture mmore 4 '[{"id":1,"login":"jeremy"},{"id":2,"login":"artifacts-sec-checker"}]'
  page_fixture mnocount - '[{"id":1,"login":"jeremy"},{"id":2,"login":"artifacts-sec-checker"}]'
  page_fixture mnologin 2 '[{"id":1},{"id":2,"login":"artifacts-sec-checker"}]'
  printf '{"message":"Must be an organization member","url":"u"}' > "$d/notmember.json"
  printf '{"message":"token does not have at least one of required scope(s): [read:organization]","url":"u"}' > "$d/ref-rorg.json"
  ot() { owners_team o "$1" "$d/$2.json" "$d/$2.h"; }   # <code> <teams fixture>
  om() { judge_owners o "$1" "$d/$2.json" "$d/$2.h"; }  # <code> <members fixture>
  expect_has "owners: the Owners team is found"           "ID 5" "$(ot 200 tok)"
  expect_has "owners: no Owners team fails"               "has no Owners team" "$(ot 200 tnoowners)"
  expect_has "owners: a second owner-level team fails"    "team admins (id 7) has owner permission" "$(ot 200 tsecondowner)"
  expect_has "owners: an Owners team without owner fails" "not owner permission" "$(ot 200 townersread)"
  expect_has "owners: teams over one page fails"          "X-Total-Count says 3" "$(ot 200 tmore)"
  expect_has "owners: teams without a count fails"        "no X-Total-Count" "$(ot 200 tnocount)"
  expect_has "owners: a team with a string id fails"      "not a list of teams" "$(ot 200 tbadid)"
  expect_has "owners: teams junk fails"                   "could not be read" "$(owners_team o 200 "$d/junk.json" "$d/tok.h")"
  expect_has "owners: teams scope refusal names it"       "SEC_CHECK_TOKEN lacks read:organization" "$(ot 403 ref-rorg)"
  expect_has "owners: teams, not a member, names it"      "is not a member of o" "$(ot 403 notmember)"
  expect_has "owners: teams 404 is could not tell"        "could not list the teams of o: the API answered 404" "$(ot 404 notfound)"
  expect "owners: exactly the expected two"               empty "$(om 200 mok)"
  expect_has "owners: an extra owner fails"               "o has an owner not on the expected list: someone" "$(om 200 mextra)"
  expect_has "owners: a missing owner fails"              "o is missing the expected owner artifacts-sec-checker" "$(om 200 mmissing)"
  expect_has "owners: a swapped owner fails (extra)"      "not on the expected list: artifacts-maintainer" "$(om 200 mswapped)"
  expect_has "owners: a swapped owner fails (missing)"    "missing the expected owner artifacts-sec-checker" "$(om 200 mswapped)"
  expect_has "owners: a login twice fails"                "names an account twice" "$(om 200 mdup)"
  expect_has "owners: members over one page fails"        "X-Total-Count says 4" "$(om 200 mmore)"
  expect_has "owners: members without a count fails"      "no X-Total-Count" "$(om 200 mnocount)"
  expect_has "owners: a member without a login fails"     "not a list of accounts" "$(om 200 mnologin)"
  expect_has "owners: members object fails"               "not a list of accounts" "$(judge_owners o 200 "$d/obj.json" "$d/mok.h")"
  expect_has "owners: members scope refusal names it"     "SEC_CHECK_TOKEN lacks read:organization" "$(om 403 ref-rorg)"
  expect_has "owners: members 403 other is could not tell" "could not list the Owners team of o: the API answered 403" "$(om 403 junk)"
  printf '{"message":"Must be a team member","url":"u"}' > "$d/notteam.json"
  expect_has "owners: members, not in the team, names it" "is not a member of o" "$(om 403 notteam)"
  expect_has "owners: members 000 is could not tell"      "could not list the Owners team of o: the API answered 000" "$(om 000 none)"
  # The live path refuses to pass without a token, before any call.
  got=$(SEC_CHECK_TOKEN="" SEC_FORGE="http://127.0.0.1:9" "$0" repo o/r runcove-sec 2>&1; echo "exit=$?")
  case "$got" in *"SEC_CHECK_TOKEN is not set"*"exit=1") printf '  ok    no token: fails loudly\n' ;;
    *) printf '  FAIL  no token: fails loudly (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  # And an unreachable forge is a failure, never a pass.
  got=$(SEC_CHECK_TOKEN="x" SEC_FORGE="http://127.0.0.1:9" "$0" repo o/r runcove-sec sec 2>&1; echo "exit=$?")
  case "$got" in *"NOT PROVEN PRIVATE"*"exit=1") printf '  ok    unreachable forge: fails\n' ;;
    *) printf '  FAIL  unreachable forge: fails (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  got=$(SEC_REGISTRY_TOKEN="" SEC_FORGE="http://127.0.0.1:9" "$0" image sec/x 2>&1; echo "exit=$?")
  case "$got" in *"SEC_REGISTRY_TOKEN is not set"*"exit=1") printf '  ok    image, no registry token: fails loudly\n' ;;
    *) printf '  FAIL  image, no registry token: fails loudly (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  got=$(SEC_REGISTRY_TOKEN="x" SEC_FORGE="http://127.0.0.1:9" "$0" image sec/x 2>&1; echo "exit=$?")
  case "$got" in *"NOT PROVEN PRIVATE"*"exit=1") printf '  ok    unreachable registry: fails\n' ;;
    *) printf '  FAIL  unreachable registry: fails (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  got=$("$0" repo 'bad name' runcove-sec 2>&1; echo "exit=$?")
  case "$got" in *"exit=64") printf '  ok    bad name refused\n' ;;
    *) printf '  FAIL  bad name refused (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  got=$("$0" repo o/r 2>&1; echo "exit=$?")
  case "$got" in *"exit=64") printf '  ok    repo without a runner label refused\n' ;;
    *) printf '  FAIL  repo without a runner label refused (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
  got=$(SEC_CHECK_TOKEN="x" SEC_FORGE="http://127.0.0.1:9" "$0" repo o/r docker-light sec 2>&1; echo "exit=$?")
  case "$got" in *"runner label must be"*"exit=64") printf '  ok    repo with another runner label refused\n' ;;
    *) printf '  FAIL  repo with another runner label refused (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac


  # The whole image path against a fake registry on localhost: the registry account's credentials
  # must arrive (as a Basic login, from curl's stdin), the bearer tokens must be used, and the
  # verdicts must be the right ones. One fake image per case (see the handler).
  local tok port=""
  tok="selftest-$(basename "$work")"
  cat > "$work/fake_forge.py" <<'PY'
import base64, http.server, json, os, sys, urllib.parse

CREDS = "artifacts-sec-publisher:" + os.environ["FAKE_TOKEN"]
# image -> (what an authenticated caller gets, what an anonymous caller gets) from tags/list
IMAGES = {
    "sec/priv":    (200, 401),   # exists, hidden with 401
    "sec/priv404": (200, 404),   # exists, hidden with 404 (what the forge does for some images)
    "sec/pub":     (200, 200),   # exists, anyone can list it
    "sec/missing": (404, 404),   # a typo: nothing there, and an anonymous 404 proves nothing
    "sec/broken":  (500, 401),   # the registry errors for the authenticated caller
    "sec/anonerr": (200, 500),   # the registry errors for the anonymous caller
    "sec/empty":   ("empty", 401),  # a name with no tags
}

EXACT = {"read:organization", "read:repository"}
# token kind -> (scopes, federation on)
TOKENS = {
    "exact":      (EXACT, False),
    "exactfed":   (EXACT, True),                              # federation on: activitypub refused for scope
    "extraread":  (EXACT | {"read:user"}, False),
    "writerepo":  ({"write:repository", "read:organization"}, False),
    "writeorg":   ({"read:repository", "write:organization"}, False),
    "noorg":      ({"read:repository"}, False),
    "apfed":      (EXACT | {"read:activitypub"}, True),
    "all":        ({"all"}, False),
    "publiconly": (EXACT | {"public-only"}, False),
    "odd":        (EXACT, False),                             # one probe answers 502
}
# token kind -> the collaborators of o/r and their roles
COLLABS = {
    "exact":         {"artifacts-maintainer": "write", "security": "read", "artifacts-sec-checker": "owner"},
    "collabextra":   {"artifacts-maintainer": "write", "security": "read", "artifacts-sec-checker": "owner", "someone": "write"},
    "collabmissing": {"artifacts-maintainer": "write", "artifacts-sec-checker": "owner"},
    "collabchanged": {"artifacts-maintainer": "write", "security": "write", "artifacts-sec-checker": "owner"},
}
# token kind -> the runners GET /repos/o/r/actions/runners?visible=true lists (repo 7, owned by org 3)
SEC_RUNNER = {"id": 11, "uuid": "u1", "name": "dedicated-priv-1", "owner_id": 3, "repo_id": 0, "labels": ["runcove-sec"], "status": "active"}
UNPRIV_RUNNER = {"id": 21, "uuid": "u21", "name": "dedicated-unpriv-1", "owner_id": 3, "repo_id": 0, "labels": ["runcove-sec-unpriv"], "status": "active"}
SHARED = {"id": 1, "uuid": "g1", "name": "shared-1", "owner_id": 0, "repo_id": 0, "labels": ["docker-light"], "status": "idle"}
RUNNERS = {
    "exact":          [SEC_RUNNER, UNPRIV_RUNNER],
    "runnersnone":    [SHARED],
    "runnersplain":   [SEC_RUNNER, UNPRIV_RUNNER, SHARED],
    "runnersglobal":  [SEC_RUNNER, UNPRIV_RUNNER, dict(SHARED, labels=["docker-light", "runcove-sec"])],
    "runnersglobunp": [SEC_RUNNER, UNPRIV_RUNNER, dict(SHARED, labels=["runcove-sec-unpriv"])],
    "runnersextra":   [UNPRIV_RUNNER, dict(SEC_RUNNER, labels=["runcove-sec", "docker-light"])],
}
# token kind -> the members of org o's Owners team (team 5)
OWNERS = {
    "exact":         ["jeremy", "artifacts-sec-checker"],
    "ownersextra":   ["jeremy", "artifacts-sec-checker", "someone"],
    "ownersmissing": ["artifacts-sec-checker"],
}
for k in ("collabextra", "collabmissing", "collabchanged", "runnersnone", "runnersplain", "runnersglobal", "runnersglobunp", "runnersextra", "runnersnotowner",
          "ownersextra", "ownersmissing", "ownersnotmember"):
    TOKENS[k] = (EXACT, False)
# (method, path) -> (scope it needs, the answer when the token has it)
ROUTES = {
    ("GET", "/api/v1/user"): ("read:user", (200, {"login": "x"})),
    ("GET", "/api/v1/notifications"): ("read:notification", (200, [])),
    ("GET", "/api/v1/packages/sec"): ("read:package", (200, [])),
    ("GET", "/api/v1/repos/o/r/issues"): ("read:issue", (200, [])),
    ("GET", "/api/v1/admin/cron"): ("read:admin", (403, {"message": "reqSiteAdmin"})),
    ("GET", "/api/v1/activitypub/actor"): ("read:activitypub", (200, {"id": "x"})),
    ("PATCH", "/api/v1/repos/o/sec-scope-probe~none"): ("write:repository", (404, {"message": "The target couldn't be found."})),
    ("PATCH", "/api/v1/orgs/sec-scope-probe~none"): ("write:organization", (404, {"message": "The target couldn't be found."})),
    ("GET", "/api/v1/repos/o/r"): ("read:repository", (200, {"id": 7, "owner": {"id": 3, "login": "o"}, "full_name": "o/r", "private": True, "mirror": False, "fork": False, "forks_count": 0})),
    ("GET", "/api/v1/repos/o/r/push_mirrors"): ("read:repository", (200, [])),
    ("GET", "/api/v1/orgs/sec"): ("read:organization", (200, {"username": "sec", "visibility": "private"})),
}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def plain404(self):
        data = b"404 page not found\n"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # The forge API for `repo`, with Forgejo's scope rules: the token is "<kind>.<FAKE_TOKEN>",
    # and the kind picks its scopes (and whether federation is on).
    def api(self, method, path):
        auth = self.headers.get("Authorization", "")
        with open(os.environ["FAKE_LOG"], "a") as log:
            log.write("%s %s %s\n" % ("auth" if auth else "anon", method, path))
        if not auth:
            if path.endswith("/info/refs"):
                return self.reply(401, {})
            return self.reply(404, {"message": "not found"})
        kind, _, secret = auth[len("token "):].partition(".")
        if secret != os.environ["FAKE_TOKEN"] or kind not in TOKENS:
            return self.reply(401, {"message": "user does not exist"})
        scopes, fed = TOKENS[kind]
        if path == "/api/v1/activitypub/actor" and not fed:
            return self.plain404()
        need, answer = ROUTES.get((method, path), (None, None))
        collabs = COLLABS.get(kind, COLLABS["exact"])
        if (method, path) == ("GET", "/api/v1/repos/o/r/collaborators"):
            need, answer = "read:repository", (200, [{"login": k, "id": 1} for k in collabs])
        elif method == "GET" and path.startswith("/api/v1/repos/o/r/collaborators/") and path.endswith("/permission"):
            login = path[len("/api/v1/repos/o/r/collaborators/"):-len("/permission")]
            if login in collabs:
                role = collabs[login]
                need, answer = "read:repository", (200, {"permission": role, "role_name": role, "user": {"login": login}})
        if (method, path) in (("GET", "/api/v1/orgs/o/teams"), ("GET", "/api/v1/teams/5/members")):
            if not ("all" in scopes or "read:organization" in scopes or "write:organization" in scopes):
                return self.reply(403, {"message": "token does not have at least one of required scope(s): [read:organization]", "url": "u"})
            if kind == "ownersnotmember":
                return self.reply(403, {"message": "Must be an organization member", "url": "u"})
            if path.endswith("/teams"):
                teams = [{"id": 5, "name": "Owners", "permission": "owner", "includes_all_repositories": True},
                         {"id": 6, "name": "check", "permission": "read", "includes_all_repositories": False}]
                return self.reply(200, teams, {"X-Total-Count": "2"})
            ms = [{"id": i + 1, "login": n} for i, n in enumerate(OWNERS.get(kind, OWNERS["exact"]))]
            return self.reply(200, ms, {"X-Total-Count": str(len(ms))})
        if (method, path) == ("GET", "/api/v1/repos/o/r/actions/runners"):
            # The repository group's scope (repository), then reqOwner(actions).
            if not ("all" in scopes or "read:repository" in scopes or "write:repository" in scopes):
                return self.reply(403, {"message": "token does not have at least one of required scope(s): [read:repository]", "url": "u"})
            if kind == "runnersnotowner":
                return self.reply(403, {"message": "user should be the owner of the repo", "url": "u"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rl = RUNNERS.get(kind, RUNNERS["exact"])
            if q.get("visible") != ["true"]:
                rl = [r for r in rl if r["owner_id"] or r["repo_id"]]
            return self.reply(200, rl, {"X-Total-Count": str(len(rl))})
        if need is None:
            return self.reply(404, {"message": "no fake route"})
        if kind == "odd" and path == "/api/v1/notifications":
            return self.reply(502, {"message": "bad gateway"})
        cat = need.split(":")[1]
        if not ("all" in scopes or need in scopes or "write:" + cat in scopes):
            return self.reply(403, {"message": "token does not have at least one of required scope(s): [%s]" % need, "url": "u"})
        if "public-only" in scopes and path in ("/api/v1/repos/o/r", "/api/v1/repos/o/r/push_mirrors"):
            return self.reply(403, {"message": "token scope is limited to public repos", "url": "u"})
        return self.reply(*answer)

    def do_PATCH(self):
        u = urllib.parse.urlparse(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        return self.api("PATCH", urllib.parse.unquote(u.path))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        auth = self.headers.get("Authorization", "")
        if not u.path.startswith("/v2/"):
            return self.api("GET", urllib.parse.unquote(u.path))
        if u.path == "/v2/token":
            if auth.startswith("Basic "):
                ok = base64.b64decode(auth[6:]).decode() == CREDS
                return self.reply(200, {"token": "auth-bearer"}) if ok else self.reply(401, {"errors": []})
            return self.reply(200, {"token": "anon-bearer"})
        if u.path.startswith("/v2/") and u.path.endswith("/tags/list"):
            name = u.path[len("/v2/"):-len("/tags/list")]
            who = {"Bearer auth-bearer": 0, "Bearer anon-bearer": 1}.get(auth)
            code = IMAGES.get(name, (404, 404))[who] if who is not None else 401
            if code == "empty":
                return self.reply(200, {"name": name, "tags": []})
            if code == 200:
                return self.reply(200, {"name": name, "tags": ["a1b2c3d4e5f6"]})
            return self.reply(code, {"errors": [{"code": "X"}]})
        return self.reply(404, {})

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
  FAKE_TOKEN="$tok" FAKE_LOG="$work/requests.log" python3 -I "$work/fake_forge.py" "$work/port" >/dev/null 2>&1 &
  srv=$!
  # shellcheck disable=SC2034  # the loop only counts: up to 5 s for the fake to write its port
  for n in $(seq 1 20); do
    [ -s "$work/port" ] && break
    sleep 0.25
  done
  port=$(cat "$work/port" 2>/dev/null)
  if [ -z "$port" ]; then
    printf '  FAIL  fake registry did not start\n'; fails=$((fails + 1))
  else
    # A proxy in the environment must not stand between the check and the fake.
    export NO_PROXY=127.0.0.1 no_proxy=127.0.0.1
    img() { # <name> [<token>]  -> output and "exit=<n>"
      SEC_REGISTRY_TOKEN="${2-$tok}" SEC_FORGE="http://127.0.0.1:$port" "$0" image "$1" 2>&1; echo "exit=$?"
    }
    got=$(img sec/priv)
    case "$got" in *"PROVEN PRIVATE"*"exit=0") printf '  ok    e2e: exists + anonymous 401: proven\n' ;;
      *) printf '  FAIL  e2e: exists + anonymous 401 (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/priv404)
    case "$got" in *"PROVEN PRIVATE"*"exit=0") printf '  ok    e2e: exists + anonymous 404: proven\n' ;;
      *) printf '  FAIL  e2e: exists + anonymous 404 (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/missing)
    case "$got" in *"does not exist"*"exit=1") printf '  ok    e2e: image missing (anonymous 404 too): fails\n' ;;
      *) printf '  FAIL  e2e: image missing (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/pub)
    case "$got" in *"can be pulled by anyone"*"exit=1") printf '  ok    e2e: exists but anonymous pull allowed: fails\n' ;;
      *) printf '  FAIL  e2e: anonymous pull allowed (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/broken)
    case "$got" in *"could not prove sec/broken exists"*"exit=1") printf '  ok    e2e: authenticated call errors (500): fails\n' ;;
      *) printf '  FAIL  e2e: authenticated call errors (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/priv wrong-token)
    case "$got" in *"could not prove sec/priv exists"*"exit=1") printf '  ok    e2e: registry login refused (wrong token): fails\n' ;;
      *) printf '  FAIL  e2e: registry login refused (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/anonerr)
    case "$got" in *"could not tell whether sec/anonerr"*"exit=1") printf '  ok    e2e: anonymous call errors (500): fails\n' ;;
      *) printf '  FAIL  e2e: anonymous call errors (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(img sec/empty)
    case "$got" in *"lists no tags"*"exit=1") printf '  ok    e2e: name with no tags: fails\n' ;;
      *) printf '  FAIL  e2e: name with no tags (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    # The token itself is never printed.
    case "$(img sec/pub; img sec/missing; img sec/priv wrong-token)" in *"$tok"*|*wrong-token*)
      printf '  FAIL  e2e: a token was printed\n'; fails=$((fails + 1)) ;;
      *) printf '  ok    e2e: no token is printed\n' ;; esac
    # The scope check of `repo`, against the fake forge with Forgejo's scope rules: each token kind
    # is a scope list. A failing scope list must stop the check before any other read (no anonymous
    # call is made at all).
    repo_as() { # <token kind>  -> output and "exit=<n>"
      : > "$work/requests.log"
      SEC_RUNNER_NAME="${2-dedicated-priv-1}" SEC_CHECK_TOKEN="$1.$tok" SEC_FORGE="http://127.0.0.1:$port" "$0" repo o/r "${3-runcove-sec}" sec 2>&1; echo "exit=$?"
    }
    scope_case() { # <name> <token kind> <text the output must contain>
      local out
      out=$(repo_as "$2")
      case "$out" in *"$3"*"exit=1")
        if grep -q '^anon ' "$work/requests.log"; then
          printf '  FAIL  %s: another read ran after the scope check failed\n' "$1"; fails=$((fails + 1))
        else
          printf '  ok    %s\n' "$1"
        fi ;;
        *) printf '  FAIL  %s (got: %s)\n' "$1" "$out"; fails=$((fails + 1)) ;; esac
    }
    got=$(repo_as exact)
    case "$got" in *"PROVEN PRIVATE: o/r"*"exit=0") printf '  ok    scopes: exactly read:organization,read:repository: proven\n' ;;
      *) printf '  FAIL  scopes: exact (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    if [ "$(grep -c '^auth ' "$work/requests.log")" -ge 10 ] && [ "$(head -n 10 "$work/requests.log" | grep -c '^auth ')" -eq 10 ]; then
      printf '  ok    scopes: the ten scope probes run first\n'
    else
      printf '  FAIL  scopes: the scope probes did not run first\n'; fails=$((fails + 1))
    fi
    got=$(repo_as exactfed)
    case "$got" in *"PROVEN PRIVATE: o/r"*"exit=0") printf '  ok    scopes: exact, federation on (activitypub refused for scope): proven\n' ;;
      *) printf '  FAIL  scopes: exact, federation on (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    scope_case "scopes: an extra read scope (read:user) fails"   extraread  "has read:user"
    scope_case "scopes: write:repository fails"                  writerepo  "has write:repository"
    scope_case "scopes: write:organization fails"                writeorg   "has write:organization"
    scope_case "scopes: no read:organization fails"              noorg      "lacks read:organization"
    scope_case "scopes: read:activitypub with federation on fails" apfed    "has read:activitypub"
    scope_case "scopes: an all-scope token fails"                all        "has read:admin"
    scope_case "scopes: a public-only token fails"               publiconly "public-only"
    scope_case "scopes: an unexpected status is could not tell"  odd        "could not tell whether the token has read:notification: a call that needs it answered 502"
    collab_case() { # <name> <token kind> <text the output must contain>
      local out
      out=$(repo_as "$2")
      case "$out" in *"$3"*"exit=1") printf '  ok    %s\n' "$1" ;;
        *) printf '  FAIL  %s (got: %s)\n' "$1" "$out"; fails=$((fails + 1)) ;; esac
    }
    collab_case "e2e collaborators: an extra one fails"   collabextra   "not on the expected list: someone (write)"
    collab_case "e2e collaborators: a missing one fails"  collabmissing "missing the expected collaborator security (read)"
    collab_case "e2e collaborators: a changed role fails" collabchanged "collaborator security is write, expected read"
    # The runner proof, end to end: the list is asked for with visible=true, and each way it can
    # fail stops the check.
    collab_case "e2e runners: no runner carries the label fails"    runnersnone     "no runner of the organization of o/r carries runcove-sec"
    collab_case "e2e runners: a global runner with no sec label fails" runnersplain "shared-1 (id 1) is a global runner of the instance"
    collab_case "e2e runners: a shared runner with the label fails" runnersglobal   "shared-1 (id 1) is not the organization's own runner but carries a sec label"
    collab_case "e2e runners: a shared runner with the unprivileged label fails" runnersglobunp "shared-1 (id 1) is not the organization's own runner but carries a sec label"
    collab_case "e2e runners: an extra label fails"                 runnersextra    "carries [\"runcove-sec\", \"docker-light\"]"
    collab_case "e2e runners: the account is not an owner fails"    runnersnotowner "is not an owner of o/r"
    collab_case "e2e owners: an extra owner fails"                  ownersextra     "o has an owner not on the expected list: someone"
    collab_case "e2e owners: a missing owner fails"                 ownersmissing   "o is missing the expected owner jeremy"
    collab_case "e2e owners: the account is not a member fails"     ownersnotmember "is not a member of o"
    got=$(repo_as exact dedicated-unpriv-1 runcove-sec-unpriv)
    case "$got" in *"PROVEN PRIVATE: o/r"*"exit=0") printf '  ok    e2e runners: a job on the unprivileged runner, proven\n' ;;
      *) printf '  FAIL  e2e runners: a job on the unprivileged runner (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(repo_as exact dedicated-priv-1 runcove-sec-unpriv)
    case "$got" in *"not the label this job runs on (runcove-sec-unpriv)"*"exit=1") printf '  ok    e2e runners: a job on unpriv that ran on the privileged runner fails\n' ;;
      *) printf '  FAIL  e2e runners: a job on unpriv that ran on the privileged runner (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(repo_as exact other-sec)
    case "$got" in *"this job's runner other-sec is not"*"exit=1") printf '  ok    e2e runners: this job on another runner fails\n' ;;
      *) printf '  FAIL  e2e runners: this job on another runner (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(repo_as exact "")
    case "$got" in *"SEC_RUNNER_NAME is not set"*"exit=1") printf '  ok    e2e runners: no runner name fails\n' ;;
      *) printf '  FAIL  e2e runners: no runner name (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    got=$(repo_as exact)
    case "$got" in *"PROVEN PRIVATE: o/r"*"runner dedicated-priv-1"*"exit=0") printf '  ok    e2e runners: the proof names the runner\n' ;;
      *) printf '  FAIL  e2e runners: the proof names the runner (got: %s)\n' "$got"; fails=$((fails + 1)) ;; esac
    if grep -qx 'auth GET /api/v1/orgs/o/teams' "$work/requests.log" && grep -qx 'auth GET /api/v1/teams/5/members' "$work/requests.log"; then
      printf '  ok    e2e owners: the teams and the Owners members were read\n'
    else
      printf '  FAIL  e2e owners: the teams or the Owners members were not read\n'; fails=$((fails + 1))
    fi
    if grep -qx 'auth GET /api/v1/repos/o/r/actions/runners' "$work/requests.log"; then
      printf '  ok    e2e runners: the runner list was read\n'
    else
      printf '  FAIL  e2e runners: the runner list was not read\n'; fails=$((fails + 1))
    fi
    case "$(repo_as extraread; repo_as writerepo; repo_as odd)" in *"$tok"*)
      printf '  FAIL  scopes: a token was printed\n'; fails=$((fails + 1)) ;;
      *) printf '  ok    scopes: no token is printed\n' ;; esac
  fi
  kill "$srv" 2>/dev/null; srv=""

  if [ "$fails" -ne 0 ]; then echo "$SELF selftest: $fails FAILED"; exit 2; fi
  echo "$SELF selftest: all passed"
  exit 0
}

work=$(mktemp -d)
trap '[ -z "$srv" ] || kill "$srv" 2>/dev/null; rm -rf "$work"' EXIT
command -v python3 >/dev/null 2>&1 || { bad "python3 is missing: cannot read the forge's answers"; exit 1; }
command -v curl >/dev/null 2>&1 || { bad "curl is missing: cannot ask the forge"; exit 1; }

case "${1:-}" in
  --selftest) selftest ;;
  repo)  [ $# -ge 3 ] && [ $# -le 4 ] || { echo "usage: $0 repo <owner/name> <runner label> [<registry-owner>]" >&2; exit 64; }
         live_repo "$2" "$3" "${4:-}" ;;
  image) [ $# -eq 2 ] || { echo "usage: $0 image <owner/name>" >&2; exit 64; }
         live_image "$2" ;;
  *) echo "usage: $0 repo <owner/name> <runner label> [<registry-owner>] | image <owner/name> | --selftest" >&2; exit 64 ;;
esac
