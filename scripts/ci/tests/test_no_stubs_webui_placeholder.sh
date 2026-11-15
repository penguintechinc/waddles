#!/usr/bin/env bash
# Proves check-no-stubs.sh blocks webui "not available yet" placeholder usages
# (<NotAvailableYet> / <ServedFeature>) unless allowlisted with an issue, and
# ignores the component definition file. Runs in a throwaway sandbox repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SB="$(mktemp -d)"
trap 'rm -rf "${SB}"' EXIT
FE="admin/hub_module/frontend/src"

mkdir -p "${SB}/scripts/ci" "${SB}/.ci" "${SB}/${FE}/pages" "${SB}/${FE}/components/ui" "${SB}/core"
cp "${REPO_ROOT}/scripts/ci/check-no-stubs.sh" "${REPO_ROOT}/scripts/ci/check_no_stubs_filter.py" \
   "${REPO_ROOT}/scripts/ci/semgrep_findings.py" "${REPO_ROOT}/scripts/ci/semgrep-silent-fallback.yml" "${SB}/scripts/ci/"
cp "${REPO_ROOT}/.ci/silent-fallback-baseline.json" "${SB}/.ci/"
echo "[]" > "${SB}/.ci/stub-allowlist.yml"

cat > "${SB}/${FE}/pages/Clean.tsx" <<'T'
export const Clean = () => <div>ok</div>;
T
# Definition file legitimately contains the JSX; must never be flagged.
cat > "${SB}/${FE}/components/ui/NotAvailableYet.tsx" <<'T'
export const X = () => <NotAvailableYet feature="a" />;
T

run() { (cd "${SB}" && bash scripts/ci/check-no-stubs.sh 2>&1); }

echo "case 1: clean tree passes"
run >/dev/null || { echo "FAIL: clean tree should pass" >&2; exit 1; }

cat > "${SB}/${FE}/pages/Join.tsx" <<'T'
export const Join = () => (
  <NotAvailableYet feature="community-open-join" />
);
T
cat > "${SB}/${FE}/pages/Gated.tsx" <<'T'
export const G = () => <ServedFeature feature="x"><div /></ServedFeature>;
T

echo "case 2: placeholder usage is blocked"
set +e; out="$(run)"; rc=$?; set -e
[ "${rc}" -ne 0 ] || { echo "FAIL: placeholder usage should fail the gate" >&2; exit 1; }
echo "${out}" | grep -q "pages/Join.tsx:2" || { echo "FAIL: Join.tsx not reported" >&2; echo "${out}" >&2; exit 1; }
echo "${out}" | grep -q "pages/Gated.tsx:1" || { echo "FAIL: Gated.tsx not reported" >&2; exit 1; }
if echo "${out}" | grep -q "components/ui/NotAvailableYet.tsx"; then echo "FAIL: definition file flagged" >&2; exit 1; fi

echo "case 3: allowlisted with issue passes"
cat > "${SB}/.ci/stub-allowlist.yml" <<A
- path: ${FE}/pages/Join.tsx
  pattern: "<(NotAvailableYet|ServedFeature)"
  issue: TBD-file
- path: ${FE}/pages/Gated.tsx
  pattern: "<(NotAvailableYet|ServedFeature)"
  issue: TBD-file
A
run >/dev/null || { echo "FAIL: allowlisted usage should pass" >&2; exit 1; }

echo "case 4: allowlist entry without issue fails"
sed -i 's/issue: TBD-file/issue: ""/' "${SB}/.ci/stub-allowlist.yml"
if run >/dev/null; then echo "FAIL: empty issue should fail" >&2; exit 1; fi
echo "PASS: 4 cases"
