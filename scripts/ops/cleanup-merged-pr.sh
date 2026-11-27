#!/usr/bin/env bash
# cleanup-merged-pr.sh - after specific PRs merge into release/*, delete their head branch (remote +
# local) and any local worktrees on it, only when provably safe. PR-targeted sibling of
# prune-agent-worktrees.sh (which sweeps ALL agent worktrees but never deletes worktree-less branches).
#
# Usage: scripts/ops/cleanup-merged-pr.sh [--dry-run] <pr> [<pr>...]
#   --dry-run    print what would be removed; change nothing
#   -h, --help   print this header and exit
# Env: REPO (default `gh repo view`), REPO_DIR (default: this checkout's top level), DRY_RUN=1 (= --dry-run).
# Safety: PR state MERGED; base is release/*; head is never main/release/*; a worktree is removed only
# when clean, not the current checkout, and its HEAD is an ancestor of origin/<base>; the remote branch
# only when its tip is an ancestor of origin/<base>; the local branch only when its tip is an ancestor of
# origin/<base> (containment proven first, so unmerged work is never deleted). Never --force pushes.
# Exit: 0 all PRs fully cleaned; 1 anything kept/skipped or zero PRs examined; 2 usage.
set -euo pipefail

DRY="${DRY_RUN:-0}"
PRS=""
usage() { echo "usage: cleanup-merged-pr.sh [--dry-run] <pr> [<pr>...]" >&2; exit 2; }
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

# run executes a mutating command, or only prints it in dry-run mode.
run() { if [ "$DRY" = 1 ]; then echo "DRY: $*"; else "$@"; fi; }

REPO_DIR="${REPO_DIR:-$(git rev-parse --show-toplevel)}"
cd "$REPO_DIR"
HERE_TOP="$(git rev-parse --show-toplevel)"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
case "$REPO" in */*) ;; *) echo "FAIL: cannot determine repo (set REPO=owner/name)" >&2; exit 1 ;; esac
git fetch -q --prune origin

examined=0; kept=0; removed_wt=0; deleted_remote=0; deleted_local=0
for pr in $PRS; do
  examined=$((examined + 1))
  if ! info="$(gh pr view "$pr" -R "$REPO" --json state,baseRefName,headRefName)"; then
    echo "#$pr FAIL gh pr view"; kept=$((kept + 1)); continue
  fi
  state="$(jq -r .state <<<"$info")"; base="$(jq -r .baseRefName <<<"$info")"; head="$(jq -r .headRefName <<<"$info")"
  if [ "$state" != MERGED ]; then echo "#$pr SKIP state=$state"; kept=$((kept + 1)); continue; fi
  case "$base" in release/*) ;; *) echo "#$pr SKIP base=$base not release/*"; kept=$((kept + 1)); continue ;; esac
  case "$head" in main|release/*|'') echo "#$pr SKIP protected head '$head'"; kept=$((kept + 1)); continue ;; esac
  git rev-parse --verify -q "origin/$base" >/dev/null || { echo "#$pr SKIP origin/$base missing"; kept=$((kept + 1)); continue; }

  # Worktrees checked out on this branch (porcelain 'branch refs/heads/<head>').
  wts="$(git worktree list --porcelain | awk -v b="refs/heads/$head" '/^worktree /{sub(/^worktree /,""); p=$0} $0=="branch "b{print p}')"
  while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    if [ "$wt" = "$HERE_TOP" ]; then echo "#$pr KEEP current checkout $wt"; kept=$((kept + 1)); continue; fi
    if [ -n "$(git -C "$wt" status --porcelain)" ]; then echo "#$pr KEEP dirty worktree $wt"; kept=$((kept + 1)); continue; fi
    if ! git merge-base --is-ancestor "$(git -C "$wt" rev-parse HEAD)" "origin/$base"; then
      echo "#$pr KEEP worktree HEAD not in $base: $wt"; kept=$((kept + 1)); continue
    fi
    run git worktree remove "$wt"; echo "#$pr removed worktree $wt"; removed_wt=$((removed_wt + 1))
  done <<EOF
$wts
EOF

  # Remote branch: only when its tip is already contained in the base.
  if git ls-remote --exit-code --heads origin "$head" >/dev/null 2>&1; then
    if git merge-base --is-ancestor "origin/$head" "origin/$base"; then
      run git push -q origin --delete "$head"; echo "#$pr deleted remote $head"; deleted_remote=$((deleted_remote + 1))
    else echo "#$pr KEEP remote $head not in $base"; kept=$((kept + 1)); fi
  else echo "#$pr remote $head already gone"; fi

  # Local branch: deleted only when its tip is contained in origin/<base> (`git branch -d` alone would
  # compare against a just-deleted upstream). git itself refuses if a kept worktree still has it out.
  if git show-ref --verify -q "refs/heads/$head"; then
    if ! git merge-base --is-ancestor "refs/heads/$head" "origin/$base"; then
      echo "#$pr KEEP local $head not in $base"; kept=$((kept + 1))
    elif [ "$DRY" = 1 ]; then echo "DRY: git branch -D $head (contained in origin/$base)"
    elif git branch -D "$head" >/dev/null 2>&1; then echo "#$pr deleted local $head"; deleted_local=$((deleted_local + 1))
    else echo "#$pr KEEP local $head (git branch refused; checked out in a kept worktree?)"; kept=$((kept + 1)); fi
  fi
done
run git worktree prune

echo "examined=$examined worktrees_removed=$removed_wt remote_deleted=$deleted_remote local_deleted=$deleted_local kept=$kept dry_run=$DRY"
[ "$examined" -gt 0 ] || { echo "FAIL: zero PRs examined" >&2; exit 1; }
[ "$kept" -eq 0 ]
