#!/usr/bin/env bash
# Self-test for scripts/ops/prune-agent-worktrees.sh: temp git repo + fake gh on PATH.
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")/../ops" && pwd)/prune-agent-worktrees.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok   - $1"; }
bad() { fail=$((fail+1)); echo "FAIL - $1"; }

mkdir "$T/bin"
cat > "$T/bin/gh" <<'GH'
#!/usr/bin/env bash
case "$*" in
  *"--state open"*) [ -z "${FAKE_GH_FAIL:-}" ] || { echo "boom" >&2; exit 1; }
                    printf '%s' "${FAKE_GH_OPEN:-}" ;;
  *) echo 0 ;;
esac
GH
chmod +x "$T/bin/gh"

setup() { # fresh repo in $T/repo with merged/dirty/skipme/openpr/unmerged worktrees
  rm -rf "$T/repo" "$T/origin.git"
  git init -q --bare "$T/origin.git"
  git init -q "$T/repo"; cd "$T/repo"
  git config user.email t@t; git config user.name t; git config commit.gpgsign false
  git checkout -q -b release/v3.0.X
  echo a > f; git add f; git commit -q -m base
  git remote add origin "$T/origin.git"; git push -q origin release/v3.0.X
  git fetch -q origin
  mkdir -p .worktrees
  for b in merged dirty skipme openpr; do git worktree add -q .worktrees/$b -b fix/$b; done
  echo x > .worktrees/dirty/untracked.txt
  git worktree add -q .worktrees/unmerged -b fix/unmerged
  (cd .worktrees/unmerged && echo u > u && git add u && git commit -q -m u)
}
run() { PATH="$T/bin:$PATH" bash "$SCRIPT" "$@" 2>&1; }
has() { [ -d "$T/repo/.worktrees/$1" ]; }

setup; cd "$T/repo"
out="$(run --apply --skip-branch fix/skipme)" || true
has merged && bad "clean merged worktree removed" || ok "clean merged worktree removed"
has dirty && ok "dirty skipped" || bad "dirty skipped"
has unmerged && ok "unmerged skipped" || bad "unmerged skipped"
has skipme && ok "--skip-branch honored" || bad "--skip-branch honored"
echo "$out" | grep -q 'user-skip' && ok "user-skip reason reported" || bad "user-skip reason reported"

setup; cd "$T/repo"
PRUNE_SKIP_BRANCHES="fix/skipme" run --apply >/dev/null || true
has skipme && ok "env PRUNE_SKIP_BRANCHES honored" || bad "env PRUNE_SKIP_BRANCHES honored"

setup; cd "$T/repo"
run --apply --skip-path '*/skipme' >/dev/null || true
has skipme && ok "--skip-path honored" || bad "--skip-path honored"
has merged && bad "skip-path leaves others eligible" || ok "skip-path leaves others eligible"

setup; cd "$T/repo"
out="$(FAKE_GH_OPEN=$'fix/openpr\n' run --apply)" || true
has openpr && ok "open-PR branch skipped" || bad "open-PR branch skipped"
echo "$out" | grep -q 'open-pr' && ok "open-pr reason reported" || bad "open-pr reason reported"

setup; cd "$T/repo"
if FAKE_GH_FAIL=1 run --apply >/dev/null; then bad "gh failure aborts"; else ok "gh failure aborts (nonzero)"; fi
has merged && ok "gh failure removed nothing" || bad "gh failure removed nothing"
FAKE_GH_FAIL=1 run --apply --no-pr-check >/dev/null || true
has merged && bad "--no-pr-check proceeds" || ok "--no-pr-check proceeds past gh failure"

setup; cd "$T/repo"
out="$(run --dry-run)" || true
has merged && ok "--dry-run removes nothing" || bad "--dry-run removes nothing"
echo "$out" | grep -q 'WOULD REMOVE' && ok "--dry-run reports candidates" || bad "--dry-run reports candidates"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
