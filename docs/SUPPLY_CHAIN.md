# Supply Chain: Image Signing, SBOM, Provenance

Every Waddles container image pushed by `.github/workflows/containers.yml` is signed,
carries an SPDX SBOM attestation and a SLSA build-provenance attestation, and is verified
fail-closed before the pipeline goes green. Production admission (Kyverno) can reject
anything that lacks them.

## What is produced

| Artifact | Tool | Attached as | Subject |
|---|---|---|---|
| Signature | cosign, keyless (GitHub OIDC -> Fulcio, logged to Rekor) | OCI signature | the digest `${VERSION}` resolves to |
| SBOM | syft, SPDX-JSON | cosign attestation, predicate `https://spdx.dev/Document` | same digest |
| SLSA provenance | `actions/attest-build-provenance` | GitHub attestation in GHCR | same digest |

- Push path only (`push`, release/main/tag). PR and merge-queue builds never receive
  `id-token: write` or `attestations: write`.
- Multi-arch: the signed subject is the manifest list. Per-arch tags
  (`${VERSION}-amd64`, `${VERSION}-arm64`) are not signed; pin deployments to the tag or
  the list digest.
- Job: `supply-chain` (one matrix entry per module). Self-test job: `supply-chain-selftest`.

## Pipeline flow

```
build-platform ─┐
merge-manifests ┴─> supply-chain[module]
                      resolve digest -> sign -> SBOM -> attest SBOM -> attest provenance
                      -> verify (signature AND SBOM AND provenance, each must be >= 1)
```

Helper: `scripts/ci/supply-chain.sh {digest|sign|sbom|attest|verify}`. Every subcommand
prints how many items it examined and exits non-zero on zero.

## Verify an image (consumers)

```bash
REF=ghcr.io/penguintechinc/waddles/hub-api@sha256:<digest>
ID='^https://github\.com/penguintechinc/waddles/\.github/workflows/containers\.yml@refs/(heads|tags)/.+$'
cosign verify --certificate-identity-regexp "$ID" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$REF"
cosign verify-attestation --type spdxjson \
  --certificate-identity-regexp "$ID" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$REF"
gh attestation verify "oci://$REF" --repo penguintechinc/waddles
```

## Admission (Kyverno)

- Template: `k8s/helm/waddlebot/templates/supply-chain/kyverno-verify-images.yaml`
- Values: `supplyChain.verifyImages` in `values.yaml` (off), `values-production.yaml`
  (`enabled: true`, `enforce: true`).
- `enforce: false` renders `Audit` (report only) and `mutateDigest: false`. Kyverno rejects
  `mutateDigest: true` under `Audit`.
- Requires Kyverno CRDs in the target cluster. Additive to the mandatory baseline
  (Pod Security Admission, Cilium, Tetragon); never a replacement.
- The `subjectRegExp` in the chart must equal the default `IDENTITY_RE` in
  `scripts/ci/supply-chain.sh`. `k8s/helm/waddlebot/tests/test_supply_chain_verify_images_render.py`
  enforces parity.

## Tests

| Command | Proves | Denominator |
|---|---|---|
| `make test-supply-chain` | sign / SBOM / attest / verify on a local registry with ephemeral keys; unsigned, wrong-key, zero-package and tag-ref cases fail | 8/8 self-test checks |
| same | Kyverno policy renders: off by default, Audit vs Enforce, identity admits release and refuses look-alikes | 6 pytest cases |
| `scripts/ci/container-smoke.sh <module> <image> <port>` | image boots, `/health` answers 200 or 503; hub-api gets a Postgres sidecar, hub-webui gets `HUB_API_URL` | per image, every CI run |

The keyless path (OIDC, Rekor, GitHub attestations) can only run inside GitHub Actions. It
is exercised on the first post-merge push to the release branch; a failure there is a
pipeline failure, not a silent skip.

## Known gaps

- Kyverno `ClusterPolicy` is deprecated upstream (Kyverno 1.19 CLI warns). Migrate to
  `ImageValidatingPolicy` when the minimum supported Kyverno version allows it.
- Python requirements in several services are plain pins without `--hash`. House standard is
  `uv pip compile --generate-hashes`; not yet applied to those services.
- Debian OS-package pins (`perl-base=5.36.0-7+deb12u4` in the hub and marketplace
  Dockerfiles) are temporary. Drop them once the upstream base digest includes the fix.
