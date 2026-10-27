# Licensing

Single source of truth: [`docs/LICENSE.md`](LICENSE.md) (custom AGPL-3.0 text).

| Component | License | Where |
|---|---|---|
| Platform (core services, bundles, hub, libs) | Custom AGPL-3.0 — `docs/LICENSE.md` canonical | `/LICENSE`, `libs/LICENSE`, `sdk/libs/LICENSE`, `services/core-community/libs/LICENSE` (verbatim copies), Rust `Cargo.toml` `license = "AGPL-3.0-or-later"` |
| SDK / client libraries | MIT | `sdk/LICENSE`, `sdk/waddle-sdk-rs`, `libs/flask_core` twin, `admin/marketplace_module/backend` |
| Commercial / Premium | Proprietary commercial counterpart to the AGPL | `Premium/LICENSE`, `Premium/PREMIUM_LICENSE.md` |

## Custom Terms (summary)

- AGPL-3.0 applies to internal and individual non-commercial use
- Commercial use (resale, bundling, SaaS) needs an explicit commercial license from the owner
- Contributor-employer GPL-2.0 grant for the community portion — full text in `docs/LICENSE.md`
- Cargo `AGPL-3.0-or-later` is the nearest SPDX id; the extra terms above are in `docs/LICENSE.md` and prevail

## Exceptions

- `core/bundle_compiler` — `Proprietary` (internal, `publish = false`)
- Third-party `rcon` crate — GPL-3.0, AGPL-3.0-compatible
- Rust workspace crates set `[licenses.private] ignore = true` in `deny.toml` (AGPL is not in the permissive dependency allowlist); third-party dependency licenses are still checked

## Source Headers

- Entrypoints (`core/*/src/main.rs`, AGPL crates) carry `// SPDX-License-Identifier: AGPL-3.0-or-later`
- No per-file header mandate elsewhere — do not mass-add
