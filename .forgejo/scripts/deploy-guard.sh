#!/usr/bin/env bash
# deploy-guard.sh <base-sha> [<ref>]
#
# Decides whether it is safe for THIS run's deploy bump to land on <ref>
# (default HEAD -- which is origin/main once ci.yml's bump_and_push has
# already run `git switch -C main origin/main`). "Safe" means every commit
# <ref> has picked up since <base-sha> (the commit this workflow run was
# triggered from) is itself one of this workflow's own `deploy:` bumps --
# anything else is a run that is already ahead of this one, and this one
# must stand down rather than overwrite it.
#
# Exit 0: safe to deploy.
# Exit 1: <ref> already carries a newer, non-bump commit -- stand down and
#         let that run's deploy win.
# Exit 2: git itself failed or disagreed (unknown revision, <base-sha> is not
#         even an ancestor of <ref>, ...) -- never treated as "nothing newer".
#
# Called from ci.yml's bump_and_push, after `git switch -C main origin/main`,
# so the one retry re-runs this too instead of skipping it.
set -u

base="${1:?usage: deploy-guard.sh <base-sha> [<ref>]}"
ref="${2:-HEAD}"

# The exact subject bump_and_push commits -- a human commit that merely
# carries "[skip ci]" somewhere in its body must still count as "newer".
bump_subject_re='^deploy: artifacts [0-9a-f]{12} \[skip ci\]$'

if ! merge_base_err=$(git merge-base --is-ancestor "$base" "$ref" 2>&1); then
    echo "deploy-guard: ${base} is not an ancestor of ${ref}: ${merge_base_err}" >&2
    exit 2
fi

if ! log=$(git log --format='%H%x09%s' "${base}..${ref}"); then
    echo "deploy-guard: git log ${base}..${ref} failed" >&2
    exit 2
fi

if [ -z "$log" ]; then
    exit 0
fi

newer=$(printf '%s\n' "$log" | while IFS=$'\t' read -r commit subject; do
    if ! printf '%s\n' "$subject" | grep -qE "$bump_subject_re"; then
        printf '%s %s\n' "$commit" "$subject"
    fi
done)

if [ -n "$newer" ]; then
    echo "${ref} already has a newer commit; that run's deploy wins:" >&2
    echo "$newer" >&2
    exit 1
fi

exit 0
