# bundle-compiler

Waddles WASM app-bundle compiler (spec `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
SS4.6, SS9.3; plan `docs/superpowers/plans/2026-09-14-rust-data-plane-m2a-compiler-sdks.md`).

Turns a bundle author's source into a signed, content-addressed WASI 0.2
component implementing `wit/waddle-bundle/stage.wit`'s `waddle:bundle/stage@1.0.0`
world. Runs as two containers of one Kubernetes Job, sharing a `/work`
`emptyDir`:

| Container | Trust | Subcommand | Does |
|---|---|---|---|
| `build` | Untrusted, gVisor, zero credentials/network | `bundle-compiler build` | Validates `bundle.yaml` v2, scans source (D34 order: legacy-DAL import check -> SAST/dependency-audit/secrets -> Skauswatch), compiles with the manifest's declared language's toolchain |
| `publisher` | Trusted, holds bucket/signing/DB credentials, never executes bundle code | `bundle-compiler publish` | Re-validates the component from bytes, computes both digests, signs the sidecar, uploads, writes `app_versions`, notifies hub-api |

## This wave's scope

Implemented and tested:
- `manifest` -- thin adapter over the landed `penguin-bundle-host::manifest`
  crate (31-rule validator).
- `scan` -- legacy-DAL import rejection (D21b), SAST/dependency-audit/secrets
  orchestration with a non-zero-denominator gate on every scanner, and the
  Skauswatch hand-off client.
- `build` -- the `LanguageBuilder` trait and `run_build`, wiring manifest
  validation and every scan above, in the D34-mandated order, before any
  per-language builder (which executes bundle code) runs.

Stubbed with TODOs (M2a plan Tasks 8-10, 11-18): the real
`componentize-py`/`cargo component`/`jco` build recipes, the WIT
import-allowlist validator, publisher-side digest computation, Ed25519
sidecar signing, bucket upload, the `waddles_publisher` Postgres write, and
the hub-api callback. Each stub module's doc comment names the plan task
that replaces it.

## Dependency note

`penguin-bundle-host` is pinned via a git `rev` (not a branch) on
`penguin-libs`' `release/rust-bundle-host/v0.1.x` -- see the comment on
that dependency in `Cargo.toml`. This is the first git dependency in this
repo; flagged for review pending a private-registry or path-vendoring
mechanism.

## Running tests

```bash
cargo test --locked
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

The SAST scan tests shell out to real `gitleaks` and `semgrep` binaries by
default (`scan::sast::ScannerConfig::default()`); tests requiring a
scanner not installed on the host can construct a `ScannerConfig` pointing
at a fixture stub (see `tests/scan_test.rs`).
