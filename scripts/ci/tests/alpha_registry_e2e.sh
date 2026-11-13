#!/usr/bin/env bash
# Real-registry end-to-end check of the changed-only alpha flow (no mocks of the registry).
#
# Starts a throwaway local registry, pushes one tiny image per alpha image labelled with a
# historical revision sha, then runs `alpha_plan.py plan` + `retag` against it and asserts:
#   * the plan's build set equals changed_images.py for that revision range,
#   * every reused image is retagged to the new tag with an IDENTICAL digest,
#   * a deployed image missing from the registry is rebuilt (put in the build set).
# Needs docker + buildx + git. Exits non-zero (never skips) if any prerequisite is missing.
#
# Usage: scripts/ci/tests/alpha_registry_e2e.sh [BASE_SHA]   (default: the repo's first parent
# of HEAD~20, i.e. a recent real range)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"
for t in docker git python3; do command -v "$t" >/dev/null || { echo "FAIL: missing $t" >&2; exit 1; }; done
docker buildx version >/dev/null

BASE_SHA="$(git rev-parse "${1:-HEAD~20}")"
HEAD_SHA="$(git rev-parse HEAD)"
WORK="$(mktemp -d)"
NAME="alpha-e2e-registry-$$"
trap 'docker rm -f "${NAME}" >/dev/null 2>&1 || true; rm -rf "${WORK}"' EXIT

docker run -d --rm --name "${NAME}" -p 127.0.0.1::5000 "${E2E_REGISTRY_IMAGE:-registry:2.8.3}" >/dev/null
PORT="$(docker port "${NAME}" 5000/tcp | head -1 | sed 's/.*://')"
REG="localhost:${PORT}/waddles"
for _ in $(seq 1 30); do curl -fsS "http://localhost:${PORT}/v2/" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://localhost:${PORT}/v2/" >/dev/null

mapfile -t IMAGES < <(python3 - <<'PY'
import sys
sys.path.insert(0, "scripts/ci")
import changed_images as ci
m = ci.load_map()
for n, i in sorted(m.images.items()):
    print(n, i.tag_suffix, sep="|")
PY
)
[ "${#IMAGES[@]}" -gt 0 ] || { echo "FAIL: zero images in map" >&2; exit 1; }

printf 'FROM scratch\nCOPY marker /marker\n' > "${WORK}/Dockerfile"
OLD_TAG="alpha-1000000000"; NEW_TAG="alpha-2000000000"
SKIP_PUSH="${E2E_MISSING_IMAGE:-hub-webui}"
pushed=0
for row in "${IMAGES[@]}"; do
  n="${row%%|*}"; sfx="${row#*|}"
  [ "${n}" = "${SKIP_PUSH}" ] && continue
  echo "${n}" > "${WORK}/marker"
  docker buildx build -q --push \
    --label "org.opencontainers.image.revision=${BASE_SHA}" \
    -t "${REG}/${n}:${OLD_TAG}${sfx}" "${WORK}" >/dev/null
  pushed=$((pushed + 1))
done
echo "pushed ${pushed} images (skipped ${SKIP_PUSH} to prove missing-image rebuild)"

printf '{"global":{"imageTag":"%s"}}' "${OLD_TAG}" > "${WORK}/values.json"
python3 scripts/ci/alpha_plan.py plan --head "${HEAD_SHA}" --tag "${NEW_TAG}" --registry "${REG}" \
  --helm-values "${WORK}/values.json" --endpoint-a tcp://buildkitd.example:1234 --endpoint-b "" \
  --out "${WORK}/plan.json"

python3 - "${WORK}/plan.json" "${BASE_SHA}" "${SKIP_PUSH}" <<'PY'
import json, subprocess, sys
sys.path.insert(0, "scripts/ci")
import changed_images as ci
plan = json.load(open(sys.argv[1])); base, skip = sys.argv[2], sys.argv[3]
m = ci.load_map()
exp = {n for n, d in ci.compute_plan(m, {n: base for n in m.images}, plan["head"],
        lambda b, h: ci.git_diff_names(b, h)).items() if d.build} | {skip}
got = set(plan["build"])
assert got == exp, f"build set mismatch: got={sorted(got)} want={sorted(exp)}"
assert skip in got, "missing registry image must be rebuilt"
print(f"OK plan: examined={plan['examined']} build={len(got)} reuse={len(plan['reuse'])}")
PY

if python3 -c "import json,sys; sys.exit(0 if json.load(open('${WORK}/plan.json'))['retag'] else 1)"; then
  python3 scripts/ci/alpha_plan.py retag --plan "${WORK}/plan.json"
  python3 - "${WORK}/plan.json" <<'PY'
import json, subprocess, sys
sys.path.insert(0, "scripts/ci")
import alpha_plan as ap
rows = json.load(open(sys.argv[1]))["retag"]
bad = 0
for r in rows:
    a, b = ap.registry_inspect(r["src"]), ap.registry_inspect(r["dst"])
    ok = a is not None and b is not None and a.digest == b.digest == r["digest"]
    bad += (not ok)
print(f"OK retag digest equality: examined={len(rows)} failed={bad}")
sys.exit(1 if (bad or not rows) else 0)
PY
else
  echo "no reused images in this range; retag step not exercised" >&2
  exit 1
fi
echo "PASS alpha_registry_e2e"
