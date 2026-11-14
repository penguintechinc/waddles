> WIP (backup push): code now uses profiles `compiled`/`scripted` (+ `all` for local/dev only), a single
> language table in `core/bundle_active_set/src/executor_profile.rs` parity-tested against
> `hub_api/cli/seed_core_bundles.py`, split ON by default, and optional per-executor Kata
> `runtimeClassName`. The body below still says python/native and is pending a rewrite.

# Language-partitioned bundle executors

Python (componentize-py) bundles take minutes each to JIT-compile and, on a single shared
`svc-bundles-executor`, starved every other bundle (alpha 2026-11-14: ~160 bundles after each
restart, `pool_exhausted: no free instance slot within EXECUTOR_POOL_WAIT_MS`, 120 s load
timeouts). Executors can now be **partitioned by language**: a Python-only executor
Deployment next to the primary one. A deployment that sets nothing behaves exactly as before.

## Profiles

`EXECUTOR_PROFILE` (executor env, default `all`):

| profile | hosts |
|---|---|
| `all` | every bundle (today's behavior; also what a stage assumes for an old executor) |
| `python` | `app_versions.language = python` only |
| `native` | everything else: `rust`, `javascript`, `typescript`, `csharp`, `other` |

A partitioned profile never guesses: a language outside that set is **not loaded on that
session** and is reported loudly (ERROR log + `waddles_executor_profile_mismatch_total
{reason="unknown_language"}`). `all` keeps loading it, as before. Decision table and tests:
`core/bundle_active_set/src/executor_profile.rs`.

## How a session registers (investigation)

1. The executor dials each stage's host-api listener (`svc-process-rust:8301`,
   `svc-action-rust:8302`) and sends `hello` (`core/bundle_executor/src/lib.rs` ->
   `Executor::hello`).
2. The stage handshake (`host_api::run_connection_with_limits` in `core/svc_process` and
   `core/svc_action`) validates it and registers the connection in `ConnectionRegistry`
   (`register` -> a unique session id).
3. **Profile carrier.** `HelloBody` belongs to the pinned external `penguin-bundle-host`
   0.5.0 crate and has no profile field, so the profile rides in the informational
   `executor_version` as SemVer-style build metadata: `0.1.0+profile=python`. `all` appends
   nothing. Old executor => no marker => `all`; old stage ignores the suffix. A marker with an
   unknown value **refuses the connection** (`hello` answered with an `error`,
   `detail = unknown_executor_profile`). A typed field in `penguin-bundle-host` is the
   follow-up; this carrier then retires.

## How bundles reach sessions

* **Changelog consumer** (`changelog_consumer.rs` in both stages) is the loader. Every tick it
  calls `bundle_active_set::plan_sessions_eligible`: a `Load` is planned **only for live
  sessions whose profile accepts the bundle's language** (`ActiveBundleRow.language`, read from
  `app_versions.language`, `NOT NULL` since migration 0022 -- the authoritative source). A
  session holding a scope it is not eligible for gets an `Unload`. The divergence check
  (`any_session_diverged_with_profiles`) is profile-aware, otherwise a native session would
  "diverge" on every Python bundle forever and force a full resync every tick.
* **Invokes.** `svc-action` dispatch already picks a session *holding the digest*
  (`pick_session_with_digest`) -- holders are only ever eligible sessions. `svc-process` picked
  any live session and lazily `ensure_loaded` onto it; it now uses
  `ConnectionRegistry::pick_dispatch_for(language)` so it can never JIT-compile a Python bundle
  on the native executor. Nested `tool.invoke` (both stages) uses `active_for(target language)`.
  A legacy env-pinned bundle with no resolvable language is eligible only on `all` sessions.
* **Defense in depth.** `RegistrySink::load` refuses (`InvokeError::ProfileMismatch`,
  `waddles_executor_profile_mismatch_total{reason="mismatch"}`) a load onto a session whose
  profile does not accept the language.
* Session dies => `on_session_removed` drops its state; the next sync reloads the bundle onto a
  **matching** live session only (never spills Python onto native or vice versa).
* Connector bundles are not touched: the connector link and `CONNECTOR_PIN_DIGEST_*` stay on the
  primary executor; the Python Deployment has no connector link.

## Metrics (OTel, both stages)

| metric | labels | meaning |
|---|---|---|
| `waddles_executor_sessions` (gauge) | `stage`, `profile` | live executor sessions per profile (0 included) |
| `waddles_executor_profile_loads_total` | `stage`, `profile`, `result` | load outcomes per session profile |
| `waddles_executor_profile_mismatch_total` | `stage`, `reason`, `profile`, `language` | bundles that could not be placed: `mismatch` (guard refused), `unknown_language`, `no_matching_session` (e.g. python executor down) |

Existing `*_bundle_zero_session_total` still fires for any active bundle loaded on zero live
sessions.

## Helm

* `pipeline.rustDataPlane.bundleExecutorPython.enabled` (default `false`; **`true` on
  `values-dal2alpha.yaml` only**) renders `svc-bundles-executor-python` (Deployment +
  CiliumNetworkPolicy + enforced Tetragon exec allowlist) with `EXECUTOR_PROFILE=python`,
  default resources requests 2 CPU / 8Gi, limits 8 CPU / 24Gi, `EXECUTOR_MAX_CONCURRENT_COMPILES=6`,
  `EXECUTOR_POOL_WAIT_MS=2000`, same probes/image/stages/mTLS client identity as the primary,
  no connector link, its **own** `precompile-cache` emptyDir.
* While it is enabled the primary renders `EXECUTOR_PROFILE=native`; otherwise
  `pipeline.rustDataPlane.bundleExecutor.profile` (default `all`).
* The twin has its own component label (`svc-bundles-executor-python`) so the two selectors are
  disjoint. HPA/PDB: register key `bundleExecutorPython` (fixed 1 replica unless an
  `autoscaling.services.bundleExecutorPython` entry is added).
* dal2alpha tuning (owner decision 2026-11-14): `EXECUTOR_MAX_CONCURRENT_COMPILES` 2 -> 6 and
  `EXECUTOR_POOL_WAIT_MS` 500 -> 2000 on the dal2alpha overlay for both executors; beta/gamma/
  prod keep the chart defaults.
* Nothing dials "the executor" by Service name (it has none): executors dial out, so a second
  Deployment needs no consumer changes. The profile is **routing metadata, not an authorization
  boundary** -- both executors use the same mTLS client identity (one trust domain).

## Not changed

Compile-cache persistence (still `emptyDir`, lost on restart -- separate, undecided item).
