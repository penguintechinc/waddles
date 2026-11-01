#!/usr/bin/env bash
# changed-paths.sh -- classify the files changed by a PR / merge-group / push into
# CI "areas" so heavy jobs can skip when nothing applicable changed (no Rust builds
# for a docs-only change, no Python security jobs when no Python/requirements file
# changed, ...).
#
# Why job-level `if:` and NOT workflow-level `paths:`: ci-gate.yml is the required
# check and aggregates every sibling check on the head SHA. A path-filtered workflow
# that does not start reports nothing at all, which is fragile for merge-queue
# gating. A job skipped by `if:` still reports (conclusion "skipped"), which ci-gate
# treats as completed-and-not-failed. So: every workflow keeps running, only the
# heavy jobs inside it are skipped.
#
# FAILS OPEN. If the diff cannot be computed (shallow clone, unknown base, force
# push, manual dispatch, scheduled run) every area is reported as changed so
# nothing is ever silently skipped. A change to CI itself (.github/workflows/**,
# scripts/ci/**, .github/scripts/**) also turns every area on, so CI edits are
# always exercised by the jobs they affect.
#
# Usage:
#   changed-paths.sh --base <sha> --head <sha>    # diff base...head
#   changed-paths.sh --files <file-with-paths>    # one path per line (used by tests)
#   changed-paths.sh --all                        # force everything on
# Output: "name=true|false" lines. When GITHUB_OUTPUT is set they are appended to
# it as well, so a job can expose them as outputs.
#
# Areas: rust python node go helm docker docs_only any_code
set -euo pipefail

AREAS=(rust python node go helm docker docs_only any_code)

declare -A HIT
for a in "${AREAS[@]}"; do HIT[$a]=false; done

emit() {
  local a
  for a in "${AREAS[@]}"; do
    echo "${a}=${HIT[$a]}"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      echo "${a}=${HIT[$a]}" >>"${GITHUB_OUTPUT}"
    fi
  done
}

all_on() {
  local a
  for a in "${AREAS[@]}"; do HIT[$a]=true; done
  HIT[docs_only]=false
}

base=""
head=""
files_from=""
force_all="false"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) base="$2"; shift 2 ;;
    --head) head="$2"; shift 2 ;;
    --files) files_from="$2"; shift 2 ;;
    --all) force_all="true"; shift ;;
    *) echo "::error::changed-paths.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [ "${force_all}" = "true" ]; then
  echo "changed-paths: --all requested -> every area on" >&2
  all_on
  emit
  exit 0
fi

changed=""
if [ -n "${files_from}" ]; then
  changed="$(cat "${files_from}")"
elif [ -n "${base}" ] && [ -n "${head}" ] \
  && [ "${base}" != "0000000000000000000000000000000000000000" ]; then
  # Callers pass exact endpoints (see .github/actions/changed-paths), so a plain
  # two-dot diff is correct and works on shallow clones (no merge base needed). If
  # the base is stale it can only OVER-include files, which is the safe direction.
  if ! changed="$(git diff --name-only "${base}" "${head}" 2>/dev/null)"; then
    echo "changed-paths: cannot diff ${base}..${head} -> failing OPEN (every area on)" >&2
    all_on
    emit
    exit 0
  fi
else
  echo "changed-paths: no usable base/head -> failing OPEN (every area on)" >&2
  all_on
  emit
  exit 0
fi

if [ -z "${changed}" ]; then
  # An empty diff means "nothing to prove" but is also what a bad ref can look
  # like; fail open rather than skip everything on an unverifiable empty set.
  echo "changed-paths: empty change set -> failing OPEN (every area on)" >&2
  all_on
  emit
  exit 0
fi

non_docs=0
while IFS= read -r f; do
  [ -z "${f}" ] && continue

  # --- CI edits exercise everything -----------------------------------------
  case "${f}" in
    .github/workflows/*|.github/actions/*|.github/scripts/*|scripts/ci/*)
      all_on
      HIT[docs_only]=false
      non_docs=1
      continue
      ;;
  esac

  # --- docs-only classification ---------------------------------------------
  # Markdown / text / licenses / images anywhere, plus docs/** and the planning
  # scratch files. (docs/**/*.py or docs/**/*.yaml would still be code, so only
  # doc-ish extensions count.)
  is_doc=0
  case "${f}" in
    *.md|*.mdx|*.txt|*.rst|*.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp) is_doc=1 ;;
    LICENSE|LICENSE.*|CONTRIBUTORS|CODEOWNERS|.github/CODEOWNERS|.TODO|.PLAN|.FUTURE) is_doc=1 ;;
  esac
  # Python/Node dependency manifests are .txt/.json but are NOT docs.
  case "${f}" in
    *requirements*.txt|*constraints*.txt) is_doc=0 ;;
  esac
  if [ "${is_doc}" -eq 1 ]; then
    continue
  fi
  non_docs=1

  # --- Rust -------------------------------------------------------------------
  case "${f}" in
    core/*|sdk/waddle-sdk-rs/*|bundles/rust/*|wit/*|proto/*|*.rs|*Cargo.toml|*Cargo.lock|*rust-toolchain*|\
    scripts/db/*.sql|hub_api/tests/fixtures/*.json|Dockerfile.rust|*/Dockerfile.rust|bundles/Dockerfile.core-bundles)
      HIT[rust]=true ;;
  esac

  # --- Python -----------------------------------------------------------------
  case "${f}" in
    *.py|*.pyi|*requirements*.txt|*requirements*.in|*constraints*.txt|*pyproject.toml|*setup.cfg|\
    *Pipfile|*Pipfile.lock|*poetry.lock|*uv.lock|alembic/*|alembic.ini|bundles/python/*|hub_api/*|libs/*|\
    .flake8|.coveragerc|.bandit|.bandit.yaml)
      HIT[python]=true ;;
  esac

  # --- Node / frontend --------------------------------------------------------
  case "${f}" in
    *.js|*.jsx|*.mjs|*.cjs|*.ts|*.tsx|*.vue|*.css|*.scss|*.html|package.json|*/package.json|\
    package-lock.json|*/package-lock.json|website/*|admin/hub_module/frontend/*|admin/hub_module/backend/*|\
    admin/marketplace_module/backend/*|tests/e2e/*|tests/integration/*|tests/smoke/*|tests/api/*)
      HIT[node]=true ;;
  esac

  # --- Go ---------------------------------------------------------------------
  case "${f}" in
    *.go|go.mod|*/go.mod|go.sum|*/go.sum|go.work|go.work.sum)
      HIT[go]=true ;;
  esac

  # --- Helm / k8s -------------------------------------------------------------
  case "${f}" in
    k8s/*|*/Chart.yaml|*/values*.yaml|*/templates/*|docker-compose*.yml)
      HIT[helm]=true ;;
  esac

  # --- Container builds -------------------------------------------------------
  case "${f}" in
    *Dockerfile*|*.dockerignore|docker-compose*.yml|.version|migrations/*|services/*|\
    processing/*|trigger/*|action/*|admin/*|core/*|libs/flask_core/*)
      HIT[docker]=true ;;
  esac
done <<<"${changed}"

if [ "${non_docs}" -eq 0 ]; then
  HIT[docs_only]=true
fi
if [ "${HIT[rust]}" = true ] || [ "${HIT[python]}" = true ] || [ "${HIT[node]}" = true ] \
  || [ "${HIT[go]}" = true ] || [ "${HIT[helm]}" = true ] || [ "${HIT[docker]}" = true ]; then
  HIT[any_code]=true
fi
# A change that matched no known area but is also not a doc (a new top-level file
# type we have not classified) must not be silently skipped: treat it as code.
if [ "${HIT[docs_only]}" = false ] && [ "${HIT[any_code]}" = false ]; then
  echo "changed-paths: non-doc change matched no known area -> any_code on (fail open)" >&2
  HIT[any_code]=true
fi

emit
