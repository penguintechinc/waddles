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
#   changed-paths.sh ... --globs <file> --area <rust|python|node|none>
#       also emits match=true|false: true when the change set (a) is "all on"
#       (CI edits, fail-open, --all), (b) hits the area's shared-dependency set
#       (rust: Cargo.lock / rust-toolchain* / .cargo / deny.toml; python:
#       requirements/constraints/pyproject/lockfiles; node: package manifests),
#       or (c) matches any glob in the file (workflow-specific dependency closure;
#       `**` is treated as `*`, which already crosses `/` in a case pattern).
#   changed-paths.sh --tier-base <ref> ...
#       PATH GATING APPLIES ONLY TO release/**: for any other base (main, empty,
#       unknown) every area and match are forced on regardless of the diff,
#       even an empty or docs-only one (fail closed).
# Areas: rust python node go helm docker docs_only any_code
set -euo pipefail

AREAS=(rust python node go helm docker docs_only any_code)

declare -A HIT
for a in "${AREAS[@]}"; do HIT[$a]=false; done

globs_from=""
area="none"
tier_base="__unset__"
MATCH=false

emit() {
  local a
  if [ -n "${globs_from}" ]; then
    echo "match=${MATCH}"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "match=${MATCH}" >>"${GITHUB_OUTPUT}"; fi
  fi
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
  MATCH=true
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
    --globs) globs_from="$2"; shift 2 ;;
    --area) area="$2"; shift 2 ;;
    --tier-base) tier_base="$2"; shift 2 ;;
    *) echo "::error::changed-paths.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [ "${tier_base}" != "__unset__" ]; then
  tb="${tier_base#refs/heads/}"
  case "${tb}" in
    release/*) ;;
    *)
      echo "changed-paths: base '${tb:-<empty>}' is not release/** -> every area on (no path gating)" >&2
      force_all="true"
      ;;
  esac
fi

if [ "${force_all}" = "true" ]; then
  echo "changed-paths: --all requested -> every area on" >&2
  all_on
  emit
  exit 0
fi

# Change set is kept NUL-delimited in a file so unusual names (spaces, unicode,
# quotes) are never quoted/mangled. --no-renames makes a rename report BOTH the
# old and new path (moving a file OUT of a gated area must still trigger it).
list0="$(mktemp)"
trap 'rm -f "${list0}"' EXIT
if [ -n "${files_from}" ]; then
  tr '\n' '\0' <"${files_from}" >"${list0}"
elif [ -n "${base}" ] && [ -n "${head}" ] \
  && [ "${base}" != "0000000000000000000000000000000000000000" ]; then
  # Callers pass exact endpoints (see .github/actions/changed-paths), so a plain
  # two-dot diff is correct and works on shallow clones (no merge base needed). If
  # the base is stale it can only OVER-include files, which is the safe direction.
  if git -c core.quotePath=false diff --no-renames --name-only -z "${base}" "${head}" >"${list0}" 2>/dev/null; then
    # A path containing a newline cannot be represented safely: fail closed.
    if [ "$(tr -cd '\n' <"${list0}" | wc -c)" -ne 0 ]; then
      echo "changed-paths: unparseable path (embedded newline) -> failing OPEN (every area on)" >&2
      all_on
      emit
      exit 0
    fi
  else
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

if [ ! -s "${list0}" ]; then
  # An empty diff means "nothing to prove" but is also what a bad ref can look
  # like; fail open rather than skip everything on an unverifiable empty set.
  echo "changed-paths: empty change set -> failing OPEN (every area on)" >&2
  all_on
  emit
  exit 0
fi

non_docs=0
while IFS= read -r -d '' f; do
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

  # --- shared-dependency closure (any job of that area is affected) ----------
  case "${area}:${f}" in
    rust:*Cargo.lock|rust:*rust-toolchain*|rust:.cargo/*|rust:*/.cargo/*|rust:deny.toml|rust:*/deny.toml|\
    python:*requirements*.txt|python:*requirements*.in|python:*constraints*.txt|python:*pyproject.toml|\
    python:*poetry.lock|python:*uv.lock|python:*Pipfile.lock|\
    node:package.json|node:*/package.json|node:package-lock.json|node:*/package-lock.json)
      MATCH=true ;;
  esac
  if [ -n "${globs_from}" ] && [ "${MATCH}" = false ]; then
    while IFS= read -r g; do
      g="${g%%#*}"; g="$(echo "${g}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      [ -z "${g}" ] && continue
      g="${g//\*\*/*}"
      # shellcheck disable=SC2254
      case "${f}" in ${g}) MATCH=true; break ;; esac
    done <"${globs_from}"
  fi

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
    *Pipfile|*Pipfile.lock|*poetry.lock|*uv.lock|alembic/*|alembic.ini|bundles/python/*|hub_api/*|libs/*|openapi/*|\
    .flake8|.coveragerc|.bandit|.bandit.yaml)
      HIT[python]=true ;;
  esac

  # --- Node / frontend --------------------------------------------------------
  case "${f}" in
    *.js|*.jsx|*.mjs|*.cjs|*.ts|*.tsx|*.vue|*.css|*.scss|*.html|package.json|*/package.json|\
    package-lock.json|*/package-lock.json|website/*|admin/hub_module/frontend/*|\
    tests/e2e/*|tests/integration/*|tests/smoke/*|tests/api/*)
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
done <"${list0}"

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
