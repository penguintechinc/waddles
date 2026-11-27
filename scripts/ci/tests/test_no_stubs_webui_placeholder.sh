#!/usr/bin/env bash
# Proves check-no-stubs.sh blocks webui "not available yet" placeholder usages
# (<NotAvailableYet> / <ServedFeature> JSX, `import ... NotAvailableYet`, and the
# literal "Missing server endpoints") unless allowlisted with an issue, and
# ignores the component definition file and its own unit test. Runs in a
# throwaway sandbox repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SB="$(mktemp -d)"
trap 'rm -rf "${SB}"' EXIT
FE="admin/hub_module/frontend/src"

mkdir -p "${SB}/scripts/ci" "${SB}/.ci" "${SB}/${FE}/pages" "${SB}/${FE}/components/ui/__tests__" "${SB}/core"
cp "${REPO_ROOT}/scripts/ci/check-no-stubs.sh" "${REPO_ROOT}/scripts/ci/check_no_stubs_filter.py" \
   "${REPO_ROOT}/scripts/ci/semgrep_findings.py" "${REPO_ROOT}/scripts/ci/check_no_stubs_placeholder.py" "${REPO_ROOT}/scripts/ci/semgrep-silent-fallback.yml" "${SB}/scripts/ci/"
cp "${REPO_ROOT}/.ci/silent-fallback-baseline.json" "${SB}/.ci/"
echo "[]" > "${SB}/.ci/stub-allowlist.yml"

cat > "${SB}/${FE}/pages/Clean.tsx" <<'T'
export const Clean = () => <div>ok</div>;
T
# Definition file legitimately contains the JSX; must never be flagged.
cat > "${SB}/${FE}/components/ui/NotAvailableYet.tsx" <<'T'
import { NotAvailableYet as Self } from './NotAvailableYet';
export const X = () => <NotAvailableYet feature="a" />;
export const Y = () => <summary>Missing server endpoints</summary>;
T
# The component's own unit test imports it and asserts the literal; exempt.
cat > "${SB}/${FE}/components/ui/__tests__/NotAvailableYet.test.tsx" <<'T'
import { NotAvailableYet } from '../NotAvailableYet';
it('x', () => { render(<NotAvailableYet feature="a" />); getByText(/Missing server endpoints/); });
T

run() { (cd "${SB}" && bash scripts/ci/check-no-stubs.sh 2>&1); }

echo "case 1: clean tree passes (definition file + its own test exempt)"
set +e; out="$(run)"; rc=$?; set -e
[ "${rc}" -eq 0 ] || { echo "FAIL: clean tree should pass" >&2; echo "${out}" >&2; exit 1; }
echo "${out}" | grep -qE "webui_placeholder_files_scanned=[1-9]" || { echo "FAIL: webui files-examined count missing/zero" >&2; exit 1; }

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
echo "case 5: multi-line NotAvailableYet import is blocked even with JSX allowlisted"
sed -i 's/issue: ""/issue: TBD-file/' "${SB}/.ci/stub-allowlist.yml"
cat > "${SB}/${FE}/pages/Members.tsx" <<'T'
import {
  Button,
  NotAvailableYet,
} from '../components/ui';
export const M = () => <Button />;
T
set +e; out="$(run)"; rc=$?; set -e
[ "${rc}" -ne 0 ] || { echo "FAIL: NotAvailableYet import should fail the gate" >&2; exit 1; }
echo "${out}" | grep -q "pages/Members.tsx:3: placeholder-import" || { echo "FAIL: Members.tsx import not reported" >&2; echo "${out}" >&2; exit 1; }
rm "${SB}/${FE}/pages/Members.tsx"

echo "case 6: 'Missing server endpoints' literal in a page is blocked"
cat > "${SB}/${FE}/pages/Hand.tsx" <<'T'
export const H = () => <p>Missing server endpoints</p>;
T
set +e; out="$(run)"; rc=$?; set -e
[ "${rc}" -ne 0 ] || { echo "FAIL: placeholder literal should fail the gate" >&2; exit 1; }
echo "${out}" | grep -q "pages/Hand.tsx:1: placeholder-text" || { echo "FAIL: Hand.tsx literal not reported" >&2; echo "${out}" >&2; exit 1; }
if echo "${out}" | grep -q "components/ui/"; then echo "FAIL: definition or its test flagged" >&2; echo "${out}" >&2; exit 1; fi
rm "${SB}/${FE}/pages/Hand.tsx"

echo "case 7: clean page passes again after offenders removed"
run >/dev/null || { echo "FAIL: clean page should pass" >&2; exit 1; }

echo "case 8: zero webui files examined fails"
rm -rf "${SB:?}/${FE}"
mkdir -p "${SB}/${FE}"
echo 'fn main() {}' > "${SB}/core/main.rs"
set +e; out="$(run)"; rc=$?; set -e
[ "${rc}" -ne 0 ] || { echo "FAIL: zero webui files should fail" >&2; exit 1; }
echo "${out}" | grep -q "zero webui files scanned" || { echo "FAIL: zero-files message missing" >&2; echo "${out}" >&2; exit 1; }
echo "PASS: 8 cases"
