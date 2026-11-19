#!/usr/bin/env bash
# Prune stale agent worktrees under .claude/worktrees/ and .worktrees/.
# Default is --dry-run; pass --apply to remove. A worktree is removed only when
# not live-locked, clean (or stale-index-only), and fully contained on origin.
set -euo pipefail

BASE="${BASE_REF:-origin/release/v3.0.X}"
MODE=dry
case "${1:-}" in
  ""|--dry-run) MODE=dry ;;
  --apply) MODE=apply ;;
  *) echo "usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
git rev-parse --verify -q "$BASE" >/dev/null || { echo "missing $BASE" >&2; exit 1; }

examined=0; removed=0; skipped=0
declare -a SKIP_LINES=()
declare -a SKIP_REASONS=()   # bash 3.2-safe: parallel arrays, tallied below

skip() { # wt branch reason
  skipped=$((skipped + 1))
  SKIP_REASONS+=("$3")
  SKIP_LINES+=("$3 | $2 | $1")
}

# Untracked noise that never counts as dirty.
NOISE='(^|/)(target|__pycache__|node_modules|\.pytest_cache)(/|$)'

classify_dirty() { # wt -> echoes "clean" | "dirty"
  local wt="$1" line rest=() staged_paths=()
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if [ "${line:0:2}" = "??" ] && printf '%s' "${line:3}" | grep -Eq "$NOISE"; then
      continue
    fi
    rest+=("$line")
  done < <(git -C "$wt" status --porcelain 2>/dev/null)
  if [ "${#rest[@]}" -eq 0 ]; then echo clean; return; fi
  # Stale-index exception: staged-only, index equals BASE for every staged path.
  for line in "${rest[@]}"; do
    [ "${line:1:1}" = " " ] && [ "${line:0:1}" != "?" ] || { echo dirty; return; }
    staged_paths+=("${line:3}")
  done
  git -C "$wt" diff --cached HEAD --quiet && { echo dirty; return; }
  if git -C "$wt" diff --cached --quiet "$BASE" -- "${staged_paths[@]}" 2>/dev/null; then
    echo clean
  else
    echo dirty
  fi
}

cur=""
while IFS= read -r wt; do
  case "$wt" in
    "$ROOT"/.claude/worktrees/*|"$ROOT"/.worktrees/*) ;;
    *) continue ;;
  esac
  examined=$((examined + 1))
  branch="$(git -C "$wt" symbolic-ref -q --short HEAD 2>/dev/null || true)"
  label="${branch:-detached}"

  case "$branch" in
    release/*|main|integration/*) skip "$wt" "$label" "protected-branch"; continue ;;
  esac

  # Live PR work — never touch.
  case "$wt" in */agent-ab086688067da2db0) skip "$wt" "$label" "live-pr-work"; continue ;; esac
  [ "$branch" = "fix/precompile-queue-recovery" ] && { skip "$wt" "$label" "live-pr-work"; continue; }

  # 1. lock check
  gd="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null || true)"
  if [ -z "$gd" ]; then skip "$wt" "$label" "unreadable-worktree"; continue; fi
  if [ -f "$gd/locked" ]; then
    pid="$(grep -Eo 'pid [0-9]+' "$gd/locked" | head -1 | awk '{print $2}' || true)"
    [ -n "$pid" ] || pid="$(grep -Eo '[0-9]{2,}' "$gd/locked" | head -1 || true)"
    if [ -z "$pid" ]; then skip "$wt" "$label" "locked-no-pid"; continue; fi
    if ps -p "$pid" >/dev/null 2>&1; then skip "$wt" "$label" "locked-live-pid"; continue; fi
    [ "$MODE" = apply ] && git worktree unlock "$wt"
  fi

  # 2. clean check
  if [ "$(classify_dirty "$wt")" != clean ]; then skip "$wt" "$label" "dirty"; continue; fi

  # 3. contained on origin
  head="$(git -C "$wt" rev-parse HEAD)"
  contained=0
  git merge-base --is-ancestor "$head" "$BASE" && contained=1
  if [ "$contained" -eq 0 ] && [ -n "$branch" ] \
     && git rev-parse --verify -q "origin/$branch" >/dev/null \
     && git merge-base --is-ancestor "$head" "origin/$branch"; then
    contained=1
  fi
  if [ "$contained" -eq 0 ]; then skip "$wt" "$label" "unpushed-or-unmerged"; continue; fi

  if [ "$MODE" = dry ]; then
    echo "WOULD REMOVE $wt ($label)"
    removed=$((removed + 1))
    continue
  fi

  if ! git worktree remove --force "$wt" 2>"${TMPDIR:-/tmp}/prune-wt.$$.err"; then
    skip "$wt" "$label" "remove-failed: $(head -1 "${TMPDIR:-/tmp}/prune-wt.$$.err" | cut -c1-60)"
    continue
  fi
  removed=$((removed + 1))
  echo "REMOVED $wt ($label)"

  # Branch cleanup
  if [ -n "$branch" ]; then
    if git rev-parse --verify -q "refs/heads/$branch" >/dev/null \
       && git merge-base --is-ancestor "refs/heads/$branch" "$BASE"; then
      git branch -D "$branch" >/dev/null && echo "  deleted local $branch"
    fi
    if git rev-parse --verify -q "origin/$branch" >/dev/null \
       && git merge-base --is-ancestor "origin/$branch" "$BASE"; then
      n="$(gh pr list --head "$branch" --state merged --json number -q 'length' 2>/dev/null || echo 0)"
      if [ "${n:-0}" -gt 0 ]; then
        git push origin --delete "$branch" >/dev/null 2>&1 && echo "  deleted remote $branch"
      fi
    fi
  fi
done < <(git worktree list --porcelain | awk '/^worktree /{sub(/^worktree /,""); print}')

[ "$MODE" = apply ] && git worktree prune

echo "== mode=$MODE base=$BASE =="
echo "examined=$examined removed=$removed skipped=$skipped"
if [ "$skipped" -gt 0 ]; then
  echo "skip reasons:"
  printf '%s\n' "${SKIP_REASONS[@]}" | sed 's/: .*//' | sort | uniq -c
  echo "skipped worktrees (reason | branch | path):"
  printf '%s\n' "${SKIP_LINES[@]}"
fi
[ "$examined" -gt 0 ] || { echo "FAIL: zero worktrees examined" >&2; exit 1; }
