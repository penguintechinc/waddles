#!/usr/bin/env bash
# merge-release-into-prs.sh - bring open PR branches up to date with their release/* base by merging
# origin/<base> into each head in a throwaway worktree. Clean merges get a (signed) merge commit and a
# plain push; conflicts are aborted and reported, never pushed. Never force-pushes, never touches main.
#
# Usage: scripts/ops/merge-release-into-prs.sh [--dry-run] <pr> [<pr>...]
#   --dry-run    do the merge locally and report the result; never push
#   -h, --help   print this header and exit
# Env: REPO (default `gh repo view`), REPO_DIR (default: this checkout's top level),
#      BASE (optional: require every PR's base to equal it; default: each PR's own release/* base),
#      GIT_SIGN (default 1: `git commit -S`; 0 only for tests), COMMIT_TRAILERS (optional message
#      paragraph appended to the merge commit, e.g. Co-Authored-By lines), TMPDIR (worktree parent).
# Refuses: PR not OPEN (skipped), cross-repo heads (skipped), base not release/*, head main/release/*.
# Exit: 0 every PR examined is up to date or merged-in; 1 any conflict, refusal or push failure, or
#       zero PRs examined; 2 usage.
set -euo pipefail

DRY=0
PRS=""
usage() { echo "usage: merge-release-into-prs.sh [--dry-run] <pr> [<pr>...]" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    -*) echo "unknown flag $1" >&2; usage ;;
    *) case "$1" in *[!0-9]*) echo "bad PR number '$1'" >&2; usage ;; esac; PRS="$PRS $1" ;;
  esac
  shift
done
[ -n "$PRS" ] || usage

GIT_SIGN="${GIT_SIGN:-1}"
REPO_DIR="${REPO_DIR:-$(git rev-parse --show-toplevel)}"
cd "$REPO_DIR"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
case "$REPO" in */*) ;; *) echo "FAIL: cannot determine repo (set REPO=owner/name)" >&2; exit 1 ;; esac
sign_flag="--no-gpg-sign"; [ "$GIT_SIGN" = 1 ] && sign_flag="-S"

git fetch -q origin

WT=""
# cleanup removes the throwaway worktree (ours, detached; nothing in it is ever kept).
cleanup() { if [ -n "$WT" ] && [ -d "$WT" ]; then git worktree remove --force "$WT" >/dev/null 2>&1 || true; WT=""; fi; }
trap cleanup EXIT

examined=0; merged=0; uptodate=0; skipped=0; conflicts=0; failed=0
for pr in $PRS; do
  examined=$((examined + 1))
  if ! info="$(gh pr view "$pr" -R "$REPO" --json headRefName,baseRefName,state,isCrossRepository)"; then
    echo "#$pr FAIL gh pr view"; failed=$((failed + 1)); continue
  fi
  head="$(jq -r .headRefName <<<"$info")"; base="$(jq -r .baseRefName <<<"$info")"
  state="$(jq -r .state <<<"$info")"; cross="$(jq -r .isCrossRepository <<<"$info")"
  if [ "$state" != OPEN ]; then echo "#$pr SKIP state=$state"; skipped=$((skipped + 1)); continue; fi
  if [ "$cross" = true ]; then echo "#$pr SKIP cross-repository head ($head)"; skipped=$((skipped + 1)); continue; fi
  case "$head" in main|release/*) echo "#$pr REFUSE protected head '$head'"; failed=$((failed + 1)); continue ;; esac
  case "$base" in release/*) ;; *) echo "#$pr REFUSE base '$base' is not release/*"; failed=$((failed + 1)); continue ;; esac
  if [ -n "${BASE:-}" ] && [ "$base" != "$BASE" ]; then
    echo "#$pr REFUSE base '$base' != BASE '$BASE'"; failed=$((failed + 1)); continue
  fi
  if ! git fetch -q origin "+refs/heads/$head:refs/remotes/origin/$head" "+refs/heads/$base:refs/remotes/origin/$base"; then
    echo "#$pr FAIL fetch $head/$base"; failed=$((failed + 1)); continue
  fi
  if git merge-base --is-ancestor "origin/$base" "origin/$head"; then
    echo "#$pr up-to-date ($head already contains $base)"; uptodate=$((uptodate + 1)); continue
  fi

  WT="$(mktemp -d "${TMPDIR:-/tmp}/merge-release-into-prs.XXXXXX")"
  rmdir "$WT"
  git worktree add -q --detach "$WT" "origin/$head"
  if git -C "$WT" merge -q --no-ff --no-commit "origin/$base" >/dev/null 2>&1; then
    msg="Merge $base into $head"
    if [ -n "${COMMIT_TRAILERS:-}" ]; then
      git -C "$WT" commit -q "$sign_flag" -m "$msg" -m "$COMMIT_TRAILERS"
    else
      git -C "$WT" commit -q "$sign_flag" -m "$msg"
    fi
    new="$(git -C "$WT" rev-parse HEAD)"
    if [ "$DRY" = 1 ]; then
      echo "#$pr DRY-RUN would push merge $new to $head"; merged=$((merged + 1))
    elif git -C "$WT" push -q origin "HEAD:refs/heads/$head"; then
      echo "#$pr MERGED-IN $new ($base -> $head)"; merged=$((merged + 1))
    else
      echo "#$pr FAIL push to $head rejected (remote moved?); not forcing"; failed=$((failed + 1))
    fi
  else
    files="$(git -C "$WT" diff --name-only --diff-filter=U | tr '\n' ' ')"
    git -C "$WT" merge --abort
    echo "#$pr CONFLICT ($head): ${files:-unknown}"; conflicts=$((conflicts + 1))
  fi
  cleanup
done

echo "examined=$examined merged=$merged up_to_date=$uptodate skipped=$skipped conflicts=$conflicts failed=$failed dry_run=$DRY"
[ "$examined" -gt 0 ] || { echo "FAIL: zero PRs examined" >&2; exit 1; }
[ "$conflicts" -eq 0 ] && [ "$failed" -eq 0 ]
