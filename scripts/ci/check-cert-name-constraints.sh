#!/usr/bin/env bash
# Gate for the #1382 class: a service-leaf Certificate whose SAN falls OUTSIDE
# the internal CA's permitted X.509 nameConstraint subtrees.
#
# Regression (alpha 2026-11-16): the host-api-tls leaf gained a bare
# `<fullname>-svc-streaming` DNS SAN the name-constrained internal CA does not
# permit. rustls-webpki checks EVERY SAN against the CA's permitted subtrees
# (not only the dialled name), so svc-bundles-executor rejected the host-API
# certs with NameConstraintViolation, crashlooped, and blocked `helm --wait` on
# every alpha deploy.
#
# The check is cryptographic, not a string compare: for each rendered
# cert-manager Certificate it mints a real P-256 CA carrying the issuer's
# nameConstraints, signs the leaf with the rendered CN/dnsNames/uris/ipAddresses,
# and `openssl verify` (which enforces nameConstraints) must accept it -- for
# EVERY environment the chart renders. The logic lives in the render test; this
# wrapper is the named, standalone gate wired into pr-validation.yml.
#
# helm AND openssl are required. The underlying test `pytest.skip`s when either
# is missing -- a skip exits 0 and would silently disable this gate
# (critical-rules.md Verification Integrity). We fail LOUD instead.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

missing=""
command -v helm >/dev/null 2>&1 || command -v helm3 >/dev/null 2>&1 || missing="helm"
command -v openssl >/dev/null 2>&1 || missing="${missing:+${missing} }openssl"
if [ -n "${missing}" ]; then
  echo "FAIL: ${missing} not found -- required for the cert name-constraints gate; it must not be skipped." >&2
  exit 1
fi

TEST="k8s/helm/waddlebot/tests/test_cert_name_constraints_render.py"

# -rs surfaces any skip reason; -ra plus the grep below turns a skip (which
# pytest exits 0 on) into a hard failure here -- a skipped env is zero leaves
# examined for that env, which is not a pass.
log="$(mktemp)"
trap 'rm -f "${log}"' EXIT
set +e
python3 -m pytest -q -p no:cacheprovider -rs "${TEST}" | tee "${log}"
status="${PIPESTATUS[0]}"
set -e
if [ "${status}" -ne 0 ]; then
  echo "FAIL: cert name-constraints gate failed (exit ${status})." >&2
  exit "${status}"
fi
if grep -Eq '[0-9]+ skipped' "${log}"; then
  echo "FAIL: cert name-constraints gate reported skipped tests -- a skipped env examines zero leaves." >&2
  exit 1
fi
echo "cert name-constraints gate: PASS (every rendered leaf validates against its CA's nameConstraints, all envs)."
