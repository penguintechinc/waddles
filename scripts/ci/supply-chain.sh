#!/usr/bin/env bash
# Supply-chain helper for the container pipeline: sign, SBOM, attest, verify.
#
# Usage:
#   supply-chain.sh digest <repo> <tag>             print the sha256 digest a tag resolves to
#   supply-chain.sh sign   <repo@sha256:...>        cosign signature (keyless unless COSIGN_KEY set)
#   supply-chain.sh sbom   <repo@sha256:...> <out>  syft SPDX-JSON SBOM; fails on zero packages
#   supply-chain.sh attest <repo@sha256:...> <sbom> cosign SPDX attestation (predicate = SBOM)
#   supply-chain.sh verify <repo@sha256:...>        fail-closed: >=1 signature, >=1 SPDX
#                                                   attestation with >=1 package, and (keyless
#                                                   only) >=1 GitHub SLSA provenance attestation
#
# Keyless identity is pinned to this repo's container workflow on branch/tag refs and to
# GitHub Actions OIDC (override only via SUPPLY_CHAIN_IDENTITY_REGEXP for forks/tests).
# COSIGN_KEY / COSIGN_PUB_KEY select key-based mode; the release pipeline never sets them,
# scripts/ci/test-supply-chain.sh does, against a throwaway local registry.
#
# Every subcommand reports how many items it examined and exits non-zero on zero.
set -euo pipefail

OIDC_ISSUER="https://token.actions.githubusercontent.com"
IDENTITY_RE="${SUPPLY_CHAIN_IDENTITY_REGEXP:-^https://github\.com/penguintechinc/waddles/\.github/workflows/containers\.yml@refs/(heads|tags)/.+$}"
REPO_SLUG="${GITHUB_REPOSITORY:-penguintechinc/waddles}"

fail() { echo "FAIL: $*" >&2; exit 1; }

# Refuses anything not pinned to an immutable sha256 digest (mutable tags forbidden).
require_digest() {
  case "$1" in
    *@sha256:*) ;;
    *) fail "ref must be pinned by digest (repo@sha256:...), got '$1'" ;;
  esac
}

# Prints the verification identity arguments for cosign (key-based or keyless).
identity_args() {
  if [ -n "${COSIGN_PUB_KEY:-}" ]; then
    printf '%s\n' --key "$COSIGN_PUB_KEY"
  else
    printf '%s\n' --certificate-identity-regexp "$IDENTITY_RE" --certificate-oidc-issuer "$OIDC_ISSUER"
  fi
}

cmd_digest() {
  local repo="${1:?repo}" tag="${2:?tag}" d
  d="$(docker buildx imagetools inspect "${repo}:${tag}" --format '{{.Manifest.Digest}}')" \
    || fail "cannot resolve ${repo}:${tag}"
  case "$d" in
    sha256:*) printf '%s\n' "$d" ;;
    *) fail "unexpected digest for ${repo}:${tag}: '${d}'" ;;
  esac
}

cmd_sign() {
  local ref="${1:?ref}"
  require_digest "$ref"
  if [ -n "${COSIGN_KEY:-}" ]; then
    cosign sign --yes --key "$COSIGN_KEY" "$ref"
  else
    cosign sign --yes "$ref"
  fi
  echo "signed: ${ref}"
}

cmd_sbom() {
  local ref="${1:?ref}" out="${2:?output path}" n
  require_digest "$ref"
  syft "$ref" -o "spdx-json=${out}" >/dev/null
  # syft emits the image itself as a CONTAINER-purpose root package; count only real components.
  n="$(jq '[.packages[] | select(.primaryPackagePurpose != "CONTAINER")] | length' "$out")"
  [ "$n" -ge 1 ] || fail "SBOM for ${ref} lists 0 packages"
  echo "sbom: ${n} packages -> ${out}"
}

cmd_attest() {
  local ref="${1:?ref}" sbom="${2:?sbom path}"
  require_digest "$ref"
  if [ -n "${COSIGN_KEY:-}" ]; then
    cosign attest --yes --key "$COSIGN_KEY" --type spdxjson --predicate "$sbom" "$ref"
  else
    cosign attest --yes --type spdxjson --predicate "$sbom" "$ref"
  fi
  echo "attested: spdxjson SBOM on ${ref}"
}

cmd_verify() {
  local ref="${1:?ref}" sigs atts pkgs
  local -a id
  require_digest "$ref"
  id=()
  while IFS= read -r line; do id+=("$line"); done < <(identity_args)

  sigs="$(cosign verify "${id[@]}" "$ref" --output json | jq 'length')"
  [ "$sigs" -ge 1 ] || fail "no verified signatures on ${ref}"
  echo "signatures verified: ${sigs} (${ref})"

  # cosign prints one entry per verified attestation; decode each DSSE payload's predicate.
  local att_json
  att_json="$(cosign verify-attestation "${id[@]}" --type spdxjson "$ref" --output json)"
  atts="$(printf '%s\n' "$att_json" | jq -s 'length')"
  [ "$atts" -ge 1 ] || fail "no verified SPDX attestations on ${ref}"
  pkgs="$(printf '%s\n' "$att_json" | jq -s \
    '[.[] | .payload | @base64d | fromjson | [.predicate.packages[] | select(.primaryPackagePurpose != "CONTAINER")] | length] | max // 0')"
  [ "$pkgs" -ge 1 ] || fail "SPDX attestation on ${ref} lists 0 packages"
  echo "SPDX attestations verified: ${atts} (max ${pkgs} packages)"

  if [ -z "${COSIGN_PUB_KEY:-}" ]; then
    local prov
    prov="$(gh attestation verify "oci://${ref}" --repo "$REPO_SLUG" --format json \
      | jq 'length')"
    [ "$prov" -ge 1 ] || fail "no GitHub provenance attestation for ${ref}"
    echo "SLSA provenance verified: ${prov} (${ref})"
  fi
  echo "verify: PASS ${ref}"
}

main() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    digest) cmd_digest "$@" ;;
    sign) cmd_sign "$@" ;;
    sbom) cmd_sbom "$@" ;;
    attest) cmd_attest "$@" ;;
    verify) cmd_verify "$@" ;;
    *) sed -n '2,15p' "$0" >&2; exit 2 ;;
  esac
}

main "$@"
