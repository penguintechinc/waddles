#!/usr/bin/env bash
# Tests for scripts/ci/changed-paths.sh. Pure bash, no network, no GitHub.
# Every "skip" assertion has a matching "fail open" assertion: the classifier
# must never skip a job that a real change could affect.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="${here}/../changed-paths.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

fails=0
checks=0

indent() {
  local line
  while IFS= read -r line; do
    echo "        got: ${line}"
  done <<<"$1"
}

# run_files <expected "k=v k=v ..."> <path> [<path> ...]
run_files() {
  local expected="$1"; shift
  printf '%s\n' "$@" >"${tmp}/files"
  local out
  out="$(env -u GITHUB_OUTPUT "${script}" --files "${tmp}/files" 2>/dev/null)"
  local kv
  for kv in ${expected}; do
    checks=$((checks + 1))
    if ! grep -qx -- "${kv}" <<<"${out}"; then
      fails=$((fails + 1))
      echo "FAIL: files=[$*] expected ${kv}"
      indent "${out}"
    fi
  done
}

expect_out() {
  local desc="$1" expected="$2" out="$3" kv
  for kv in ${expected}; do
    checks=$((checks + 1))
    if ! grep -qx -- "${kv}" <<<"${out}"; then
      fails=$((fails + 1))
      echo "FAIL: ${desc}: expected ${kv}"
      indent "${out}"
    fi
  done
}

ALL_ON="rust=true python=true node=true go=true helm=true docker=true any_code=true docs_only=false"

# ---- skips (the point of the exercise) --------------------------------------
run_files "docs_only=true any_code=false rust=false python=false node=false go=false helm=false docker=false" \
  docs/superpowers/plans/x.md README.md CHANGELOG.md .TODO .PLAN docs/img/a.png
run_files "rust=false python=false node=false go=false helm=false docker=false docs_only=true" \
  docs/superpowers/specs/y.md
run_files "python=true rust=false node=false go=false docs_only=false any_code=true" \
  hub_api/services/foo.py
run_files "python=true rust=false node=false docs_only=false" \
  alembic/versions/0070_x.py
run_files "python=true rust=false docs_only=false" \
  services/foo/requirements.txt
run_files "python=true rust=false docs_only=false" \
  hub_api/requirements.in
run_files "rust=true python=false node=false go=false docs_only=false any_code=true" \
  core/svc_process/src/lib.rs
run_files "rust=true python=false node=false docs_only=false" \
  core/svc_process/Cargo.lock
run_files "rust=true python=false docs_only=false" \
  wit/waddle-bundle/tool.wit
run_files "rust=true python=false docs_only=false" \
  scripts/db/bundle_economy_store.sql
run_files "rust=true python=false docs_only=false" \
  proto/waddles/hub/internal/v1/identity.proto
run_files "node=true rust=false python=false docs_only=false" \
  website/src/App.tsx
run_files "node=true rust=false python=false docs_only=false" \
  admin/hub_module/frontend/package-lock.json
run_files "go=true rust=false python=false node=false docs_only=false" \
  tools/foo/main.go
run_files "helm=true rust=false python=false docs_only=false" \
  k8s/helm/waddlebot/values.yaml

# ---- docs never mask code in a mixed change ---------------------------------
run_files "python=true docs_only=false any_code=true rust=false" \
  docs/a.md hub_api/foo.py
run_files "rust=true python=true docs_only=false" \
  README.md core/x/src/lib.rs hub_api/foo.py

# ---- CI edits exercise every area -------------------------------------------
run_files "${ALL_ON}" .github/workflows/rust-build-all.yml
run_files "${ALL_ON}" scripts/ci/check-no-stubs.sh
run_files "${ALL_ON}" .github/scripts/npm-audit.mjs
run_files "${ALL_ON}" .github/actions/changed-paths/action.yml docs/a.md

# ---- fail open ---------------------------------------------------------------
# Unclassified, non-doc file -> treated as code, never silently skipped.
run_files "any_code=true docs_only=false" some/new/thing.xyz
# Empty change set is unverifiable -> everything on.
: >"${tmp}/empty"
out="$(env -u GITHUB_OUTPUT "${script}" --files "${tmp}/empty" 2>/dev/null)"
expect_out "empty change set" "${ALL_ON}" "${out}"
# No arguments -> everything on.
out="$(env -u GITHUB_OUTPUT "${script}" 2>/dev/null)"
expect_out "no args" "${ALL_ON}" "${out}"
# --all -> everything on.
out="$(env -u GITHUB_OUTPUT "${script}" --all 2>/dev/null)"
expect_out "--all" "${ALL_ON}" "${out}"
# Zero SHA (new branch push) -> everything on.
out="$(env -u GITHUB_OUTPUT "${script}" --base 0000000000000000000000000000000000000000 --head HEAD 2>/dev/null)"
expect_out "zero base sha" "${ALL_ON}" "${out}"

# Unresolvable refs -> everything on (run inside a scratch repo).
git init -q "${tmp}/repo"
(
  cd "${tmp}/repo"
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
)
out="$(cd "${tmp}/repo" && env -u GITHUB_OUTPUT "${script}" --base deadbeefdeadbeefdeadbeefdeadbeefdeadbeef --head HEAD 2>/dev/null)"
expect_out "unresolvable base" "${ALL_ON}" "${out}"

# Real git diff, docs-only then code (exact endpoints, as the CI action passes).
(
  cd "${tmp}/repo"
  mkdir -p docs core/x/src
  echo a >docs/a.md
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m docs
  echo b >core/x/src/lib.rs
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m code
)
out="$(cd "${tmp}/repo" && env -u GITHUB_OUTPUT "${script}" --base HEAD~2 --head HEAD~1 2>/dev/null)"
expect_out "git diff docs-only" "docs_only=true rust=false python=false any_code=false" "${out}"
out="$(cd "${tmp}/repo" && env -u GITHUB_OUTPUT "${script}" --base HEAD~1 --head HEAD 2>/dev/null)"
expect_out "git diff rust" "rust=true python=false docs_only=false" "${out}"


# ---- release-PR path gating: match output + main short-circuit ----------------
# gate_case <desc> <expected match> <tier-base|-> <area> <glob-file-content> <paths...>
# Table-driven: every row states whether a workflow with those globs/area runs.
printf '%s\n' 'core/svc_ingest/**' 'proto/**' >"${tmp}/globs"
gate_case() {
  local desc="$1" want="$2" tb="$3" ar="$4"; shift 4
  printf '%s\n' "$@" >"${tmp}/files"
  local args=(--files "${tmp}/files" --globs "${tmp}/globs" --area "${ar}")
  if [ "${tb}" != "-" ]; then args+=(--tier-base "${tb}"); fi
  local out
  out="$(env -u GITHUB_OUTPUT "${script}" "${args[@]}" 2>/dev/null)"
  expect_out "${desc}" "match=${want}" "${out}"
}
#          desc                              match base               area   paths
gate_case "python-only diff skips rust job"   false release/v3.0.X      rust   hub_api/services/foo.py
gate_case "own-crate diff runs rust job"      true  release/v3.0.X      rust   core/svc_ingest/src/lib.rs
gate_case "path-dep (proto) diff runs job"    true  release/v3.0.X      rust   proto/hub.proto
gate_case "Cargo.lock runs ALL rust jobs"     true  release/v3.0.X      rust   Cargo.lock
gate_case "crate Cargo.lock runs rust job"    true  refs/heads/release/v3.0.X rust core/other/Cargo.lock
gate_case "rust-toolchain runs rust job"      true  release/v3.0.X      rust   rust-toolchain.toml
gate_case "Cargo.lock does NOT run py job"    false release/v3.0.X      python Cargo.lock
gate_case "requirements runs python job"      true  release/v3.0.X      python hub_api/requirements.txt
gate_case "workflow diff runs everything"     true  release/v3.0.X      rust   .github/workflows/anything.yml
gate_case "action diff runs everything"       true  release/v3.0.X      none   .github/actions/changed-paths/action.yml
gate_case "scripts/ci diff runs everything"   true  release/v3.0.X      none   scripts/ci/changed-paths.sh
gate_case "docs-only on release skips"        false release/v3.0.X      rust   docs/a.md
# main (and anything not release/**) is NEVER gated, even for docs-only/unrelated diffs.
gate_case "main PR docs-only runs everything" true  main                rust   docs/a.md
gate_case "main PR unrelated runs everything" true  main                python core/zzz/x.rs
gate_case "refs/heads/main runs everything"   true  refs/heads/main     none   README.md
gate_case "empty base runs everything"        true  ""                  rust   docs/a.md
gate_case "unknown base runs everything"      true  feature/x           rust   docs/a.md
# main with an EMPTY diff: all areas + match on, docs_only off.
: >"${tmp}/files"
out="$(env -u GITHUB_OUTPUT "${script}" --files "${tmp}/files" --globs "${tmp}/globs" --area rust --tier-base main 2>/dev/null)"
expect_out "main PR empty diff -> all on" "match=true ${ALL_ON}" "${out}"
out="$(env -u GITHUB_OUTPUT "${script}" --files "${tmp}/files" --globs "${tmp}/globs" --area rust --tier-base release/v3.0.X 2>/dev/null)"
expect_out "release PR empty diff fails open" "match=true ${ALL_ON}" "${out}"
# Detector failure (bad base ref) on a release PR -> everything runs.
out="$(cd "${tmp}/repo" && env -u GITHUB_OUTPUT "${script}" --base deadbeefdeadbeef --head HEAD --globs "${tmp}/globs" --area rust --tier-base release/v3.0.X 2>/dev/null)"
expect_out "release PR undiffable base -> fail open" "match=true ${ALL_ON}" "${out}"
# openapi/ change counts as python (spec drift job).
run_files "python=true" openapi/v1.yaml


# ---- gate-bypass hardening: renames, odd paths (real git repos) ----------------
rr="${tmp}/repo2"
mkdir -p "${rr}"
(
  cd "${rr}"
  git init -q . && mkdir -p hub_api core/svc_ingest/src docs
  echo 'print(1)' >hub_api/a.py
  printf 'x\n' >"core/svc_ingest/src/we ird \"q\" \303\251.rs"
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m base
  git mv hub_api/a.py docs/a.md          # rename OUT of the python area
  git mv "core/svc_ingest/src/we ird \"q\" \303\251.rs" docs/b.md  # rename OUT of rust glob
  git -c user.email=t@t -c user.name=t commit -q -am mv
)
out="$(cd "${rr}" && env -u GITHUB_OUTPUT "${script}" --base HEAD~1 --head HEAD --globs "${tmp}/globs" --area python --tier-base release/v3.0.X 2>/dev/null)"
expect_out "rename out of python area still runs python" "python=true docs_only=false" "${out}"
out="$(cd "${rr}" && env -u GITHUB_OUTPUT "${script}" --base HEAD~1 --head HEAD --globs "${tmp}/globs" --area rust --tier-base release/v3.0.X 2>/dev/null)"
expect_out "odd (space/unicode/quote) old path matched by glob" "match=true rust=true" "${out}"
# Newline in a path -> unparseable -> fail closed.
(
  cd "${rr}"
  printf 'x\n' >"$(printf 'nl\nname.md')"
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m nl
)
out="$(cd "${rr}" && env -u GITHUB_OUTPUT "${script}" --base HEAD~1 --head HEAD --globs "${tmp}/globs" --area rust --tier-base release/v3.0.X 2>/dev/null)"
expect_out "newline path fails closed" "match=true ${ALL_ON}" "${out}"

# GITHUB_OUTPUT is written in name=value form.
printf '%s\n' docs/a.md >"${tmp}/files"
: >"${tmp}/gh_out"
GITHUB_OUTPUT="${tmp}/gh_out" "${script}" --files "${tmp}/files" >/dev/null 2>&1
checks=$((checks + 1))
if ! grep -qx 'rust=false' "${tmp}/gh_out" || ! grep -qx 'docs_only=true' "${tmp}/gh_out"; then
  fails=$((fails + 1))
  echo "FAIL: GITHUB_OUTPUT contents:"; sed 's/^/        /' "${tmp}/gh_out"
fi

# Unknown argument is a hard error, not a silent fall-through.
checks=$((checks + 1))
if "${script}" --bogus >/dev/null 2>&1; then
  fails=$((fails + 1)); echo "FAIL: unknown argument should exit non-zero"
fi

echo "changed-paths tests: ${checks} checks, ${fails} failed"
[ "${fails}" -eq 0 ]
