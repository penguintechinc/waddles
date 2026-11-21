# App Bundle SDK Design Specification

Status: **DRAFT — for review, not implementation.** Extends
`docs/plans/2026-08-26-v3-scbm-apps-design.md` (the "Apps" and "Deployment" sections) with
the packaging, coexistence, and lifecycle model needed before any bundle SDK code is written.
Every "today" claim below cites the real file/line it is grounded in; every new field or
table is a **proposal**, called out as such.

---

## 1. Overview

**An App Bundle *is* the App** — not a grouping of multiple Apps. A bundle is the packaged
combination of `{config + spec + script}` for each pipeline stage (`ingest` / `process` /
`action`) plus one top-level manifest (`bundle.yaml`). A bundle may implement 1, 2, or all 3
pipeline stages; it omits directories for stages it doesn't touch. This is the authoring-time
counterpart to the runtime `AppManifest` dataclass already defined in
`libs/flask_core/flask_core/app_manifest.py:93-114`.

A bundle may additionally declare a 4th component, `presentation` (§3.2a) — a per-community
HTML/JS overlay rendered client-side in an OBS browser source by `svc-presentation`, not an
async pipeline stage. It sits outside the ingest→process→action queue chain entirely: no
`entrypoint`, no Valkey produce/consume boundary of its own (though it may `consume` a stream
for live data), just a served overlay.

Pipeline: **ingest →[Valkey queue]→ process →[Valkey queue]→ action** — a queue at *every*
stage boundary, not just the first. `svc-ingest` / `svc-process` / `svc-action`
(`docs/plans/2026-08-26-v3-scbm-apps-design.md:496-511`) are the three generic stage-runner
containers — they carry no bundle-specific code, only the loader that reads each activated
bundle's manifest and dispatches to its stage script. process→action is a second queue hop,
never a direct call: each stage publishes onto the next stage's own Valkey stream (§3.2's
`produces`/`consumes`, §7.2's isolation keys) and only that stream connects them. A slow or
long-running `action` handler backs up its *own* action-stage queue — bounded, spilling to the
DLQ on overflow (`stream_pipeline.py:368-438`) — without back-pressuring or blocking `process`
or `ingest`. Every stage is fully decoupled and scales independently.

**What changes from the current single-winner model**: `app_binding.py`'s `resolve_app`
(`app_binding.py:92-133`) picks exactly **one** App per `(tenant, community, feature)` slot.
This spec replaces that with **coexistence** — a community activates a *set* of bundles per
Feature (e.g. 4 giveaway bundles running side by side), each isolated by its own stream
namespace, config, and state, with conflicts resolved by an explicit
`compatible_with`/`incompatible_with` declaration rather than the binding ladder picking a
winner.

Term is always **ingest** — never receiver/integration.

---

## 2. Bundle anatomy

```
giveaway-classic/
├── bundle.yaml                  # top-level manifest (§3)
├── ingest/
│   ├── handler.py                # entry point (§4)
│   ├── config.yaml               # default config for this stage
│   └── spec.yaml                 # stage contract: consumes/produces, scopes
├── process/
│   ├── handler.py
│   ├── config.yaml
│   └── spec.yaml
├── action/
│   ├── handler.py
│   ├── config.yaml
│   └── spec.yaml
└── presentation/                 # optional 4th component (§3.2a) -- no script stage
    ├── overlay.html               # html_entrypoint
    ├── overlay.js                 # assets
    └── overlay.css                # assets
```

A bundle implementing only `process` + `action` (e.g. a pure moderation-decision bundle
triggered by another bundle's ingest) omits the `ingest/` directory entirely — `bundle.yaml`'s
`stages` map only lists what exists, mirroring how `surfaces` today is an optional, possibly-
partial tuple (`app_manifest.py:110`, `parse_manifest` never requires all three). `presentation/`
follows the same optionality — most bundles have no overlay and omit the directory and the
`stages.presentation` entry entirely.

---

## 3. Manifest schema (`bundle.yaml`)

### 3.1 Field table

| Field | Type | Today (file:line) | Status |
|---|---|---|---|
| `app_id` | str, `<github-org>.<repo>.<bundle-name>` | `app_manifest.py:104`, `_APP_ID_RE` line 57 | unchanged |
| `name` | str | `app_manifest.py:105` | unchanged |
| `version` | SemVer str | `app_manifest.py:106`, `_SEMVER_RE` line 61-66 | unchanged |
| `feature` | removed — the owner is the first two segments of `app_id` | `app_manifest.py:107` | unchanged |
| `module` | str, one of `KNOWN_MODULES` | `app_manifest.py:108`, set at line 35-47 | unchanged |
| `provider` | `builtin` \| `thirdparty` | `app_manifest.py:109`, `KNOWN_PROVIDERS` line 53 | unchanged — **authorship** axis |
| `is_default` | bool | `app_manifest.py:113` | unchanged |
| `config_schema` | dict | `app_manifest.py:112` | unchanged |
| `permissions` | tuple[str] | `app_manifest.py:111` | **renamed** to `requires_scopes` in bundle.yaml (design doc's own term, `2026-08-26-v3-scbm-apps-design.md:409`) — reconciliation in §3.3 |
| `surfaces` | tuple of stage **names** only, `{ingest,process,action,presentation}` | `app_manifest.py:110,63` | **superseded** by `stages` map (§3.2) — `AppManifest.surfaces` stays as a derived compat field; now may include `presentation` (§3.2a), a non-script surface |
| `execution_model` | `native` \| `thirdparty` | — | **NEW** — how the bundle runs, orthogonal to `provider` (a `builtin` bundle may still front a `thirdparty` endpoint, e.g. wrapping a SaaS API) |
| `compatible_with` | list[app_id], optional | — | **NEW** (§7) |
| `incompatible_with` | list[app_id], optional | — | **NEW** (§7) |
| `platform_compatibility` | object (§3.4) | — | **NEW** |
| `stages` | map, keyed by stage name (§3.2) | — | **NEW** — richer replacement for `surfaces`; may include a `presentation` entry (§3.2a) alongside/instead of `ingest`/`process`/`action` |

### 3.2 `stages` map

```yaml
stages:
  ingest:
    entrypoint: "ingest/handler.py:on_event"   # native only
    consumes: []                                # ingest has no upstream stream
    produces: ["giveaway.entry_detected"]
    config: "ingest/config.yaml"
    spec: "ingest/spec.yaml"
  process:
    entrypoint: "process/handler.py:on_event"
    consumes: ["giveaway.entry_detected"]
    produces: ["giveaway.winner_selected"]
    config: "process/config.yaml"
    spec: "process/spec.yaml"
  action:
    entrypoint: "action/handler.py:on_event"
    consumes: ["giveaway.winner_selected"]
    produces: []
    config: "action/config.yaml"
    spec: "action/spec.yaml"
```

Third-party stage — no `entrypoint`, an endpoint instead (fields lifted directly from
`marketplace_modules` columns added in `059_marketplace_consolidation.sql:15-22` and consumed
by `vendorExecutionService.executeCommand()`, `vendorExecutionService.js:25-102`):

```yaml
stages:
  action:
    execution_model: thirdparty
    communication_model: webhook_push        # or rest_pull
    webhook_url: "https://vendor.example.com/hook"
    webhook_secret: "${SECRET:vendor_hmac}"   # penguin-sal reference, never inline
    webhook_timeout_ms: 5000
    consumes: ["giveaway.winner_selected"]
    produces: []
```

Per the design doc (`2026-08-26-v3-scbm-apps-design.md:416-417`): *"a first-party App may
span stages; a third-party App never does — it is reached over the network from whichever
stage invokes it."* A `thirdparty`-`execution_model` stage entry is therefore always a
single-stage block; a bundle mixing native `process` + thirdparty `action` is valid (native
stage calls out to the endpoint at the process→action stream boundary), but a single stage
block is never half-native/half-network.

### 3.2a `presentation` — the 4th bundle component

**NEW.** `presentation` is a `stages` map entry like `ingest`/`process`/`action`, but it is not
a pipeline stage — it declares a client-side HTML/JS overlay, not an async event script. It has
no `entrypoint`, is never invoked by a stage-runner, and sits outside the
ingest→process→action queue chain entirely (§1). Concretely: a giveaway bundle's `action` stage
posts the winner announcement to chat (§9); its `presentation` component is the spinning-wheel
overlay a streamer drops into OBS as a browser source so viewers watching the stream see the
draw happen live.

```yaml
stages:
  presentation:
    html_entrypoint: "presentation/overlay.html"
    assets:
      - "presentation/overlay.js"
      - "presentation/overlay.css"
    browser_source_path: "/presentation/waddles.bot.giveaway.giveaway-classic"
    consumes: ["giveaway.winner_selected"]   # optional -- live data, not a pipeline hop
```

| Field | Type | Meaning |
|---|---|---|
| `html_entrypoint` | str, **required** | path to the overlay's HTML file — the asset `svc-presentation` serves at `browser_source_path` |
| `assets` | list[str], optional | the overlay's JS/CSS/image files, served alongside `html_entrypoint` |
| `browser_source_path` | str, **required** | the per-community route `svc-presentation` serves this overlay at — the URL a streamer pastes into an OBS browser source |
| `consumes` | list[str], optional | Valkey stream(s) the overlay's own client-side JS polls/subscribes to for live data (e.g. `giveaway.winner_selected`) — the overlay's read path into the pipeline, not a stage hop |

Not used by a `presentation` entry: `entrypoint`, `produces`, `config`, `spec`, and the
thirdparty-only fields (`execution_model`/`communication_model`/`webhook_url`/`api_base_url`/
`secret_ref`/`timeout_ms`) — those describe an invoked stage, and `presentation` is served, not
invoked. `parse_manifest` rejects a `presentation` entry missing `html_entrypoint` or declaring
a script `entrypoint`, and rejects `html_entrypoint` on any of `ingest`/`process`/`action` —
each surface's shape is enforced, not just documented (`app_manifest.py`'s
`_compile_stage_spec`).

`svc-presentation` is a new, 4th stage-runner-style container alongside `svc-ingest`/
`svc-process`/`svc-action` (§1, §2), but it does not participate in the poll/reconcile code
distribution model of §6.2 the same way — it serves static HTML/JS/CSS per activated bundle at
a per-community `browser_source_path` rather than executing an `entrypoint` per event. Exact
distribution/caching mechanics for `presentation` assets are out of scope for this subsection;
they follow the same `app_catalog`/`app_activations` lifecycle (§5) as any other component.

### 3.3 `permissions` → `requires_scopes` reconciliation

`AppManifest.permissions` (`app_manifest.py:111`) is the only scopes-shaped field that exists
today. The design doc's own example manifest (`2026-08-26-v3-scbm-apps-design.md:409`) already
called this `requires_scopes`. This spec adopts `requires_scopes` as the bundle.yaml field
name and treats `AppManifest.permissions` as the field `parse_manifest` populates from it —
either rename `permissions` → `requires_scopes` on `AppManifest` itself, or keep `permissions`
internally and alias at parse time. Pick one at implementation time (not decided here).

**Enforcement**: `requires_scopes` MUST be a subset of the declared Feature's
`FeatureContract.requires_scopes` (`feature_contract.py:86`). A bundle cannot claim scopes its
Feature contract doesn't grant — checked at `parse_manifest` time once the Feature contract is
resolvable, new validation not present in `app_manifest.py` today.

### 3.4 `platform_compatibility`

```yaml
platform_compatibility:
  tested_with: "v3.0.x"      # informational, matches release/v{Major}.{Minor}.X naming
  min_version: "3.0.0"        # SemVer, inclusive
  max_version: "3.999.999"    # SemVer, inclusive; omit for open-ended (discouraged)
```

Modeled on npm `engines`. `min_version`/`max_version` reuse the existing `_SEMVER_RE`
(`app_manifest.py:61-66`) — no new version grammar. `tested_with` is free-text matching the
repo's own release-branch convention (`release/v{Major}.{Minor}.X`, see root `CLAUDE.md`).

There is currently **no single canonical "running platform version"** to compare against —
`libs/flask_core/flask_core/__init__.py:15` has `__version__ = "2.0.0"`, which is already
stale relative to the `release/v3.0.X` branch this spec lives on. Establishing that source is
itself part of the implementation work, not assumed here.

**Enforcement policy — OPEN DECISION, see §10.** Proposed default: block install/available/
activate transitions when the running platform version is outside `[min_version,
max_version]`; warn (log + surface in an admin UI) but allow when it's in-range but does not
match `tested_with`. Flagged because "block vs warn" at each boundary is a product decision,
not an engineering one.

### 3.5 bundle.yaml → `AppManifest` mapping

`bundle.yaml` is the authoring artifact; `AppManifest` (`app_manifest.py:93-114`,
`slots=True, frozen=True`) is the runtime object `parse_manifest` builds and `AppRegistry`
indexes (`app_registry.py:62-81`). Today's `parse_manifest` (`app_manifest.py:123-206`)
validates 7 things in order (semver, namespacing, module, feature-prefix, provider, surfaces)
— this spec adds three more validation steps to run in the same function: `execution_model`
membership, `compatible_with`/`incompatible_with` are known `app_id`s (checked against the
registry, not the manifest in isolation), and `platform_compatibility` semver well-formedness.
`stages` compiles down to `surfaces` (stage-name tuple, unchanged shape, for any code that
still only needs "does this bundle touch process") plus a new `stage_specs: Dict[str,
StageSpec]` field carrying entrypoint/consumes/produces/config per stage — `AppManifest` gains
this field; it does not replace `surfaces`.

---

## 4. Per-stage script contract

| Stage | Entry point receives | Emits onto | Config/secrets | Scopes |
|---|---|---|---|---|
| `ingest` | raw platform event (Twitch/Discord/etc. payload or webhook body) + tenant/community context + this instance's `config.yaml` merged with runtime overrides (§5) | this bundle's isolated `process`-stage stream (§7.2) | `penguin-sal` reference in `config.yaml`, never inline value | `requires_scopes` declared for the `ingest` stage, always ⊆ bundle-level `requires_scopes` |
| `process` | one event off this bundle's isolated `process` stream, tenant/community context, config | this bundle's isolated `action`-stage stream, or the DLQ on poison (`stream_pipeline.py:368-438`, `move_to_dlq`) | same | same |
| `action` | one event off this bundle's isolated `action` stream | nothing (terminal) — or, for `thirdparty` execution, a network call per §8 | same | same |

`presentation` (§3.2a) is deliberately **not** in this table — it has no entry point, receives
no event off a queue, and emits nothing. It is served static HTML/JS/CSS, optionally reading
live data by having its own client-side JS `consume` a stream directly; it does not participate
in the `entrypoint`/DLQ/idempotency contract below.

Every handler is `async def` (`entrypoint: "module.py:function"`, awaited by the stage-runner
loader) per `backend-python.md`. Consumers **must be idempotent** — at-least-once delivery is
explicit platform policy (`2026-08-26-v3-scbm-apps-design.md:561-563`); every event envelope
carries an id and handlers dedupe on it.

Tenant/community context is injected by the stage-runner from the resolved `AppInstallation`
row (§5), never read from the event payload — same tenant-trust boundary as
`security.md`'s "client cannot set tenant" rule.

---

## 5. Lifecycle: install → available → activate

Three tiers, narrowing global → tenant → community, with a hard **subset** invariant:
`activated ⊆ available ⊆ installed`.

| Tier | Scope | Meaning | Closest thing that exists today |
|---|---|---|---|
| **installed** | global/platform | which bundles exist on the platform at all | `AppRegistry` (`app_registry.py:49-110`) is in-memory only, load-once at startup (docstring lines 16-21) — no persisted "is this app_id permitted on this cluster" row |
| **available** | tenant | which installed bundles a tenant may activate | **gap** — `059_marketplace_consolidation.sql:22,64-65` added `tenant_id` to `marketplace_modules`/`marketplace_subscriptions`, but that scopes *ownership/submission*, not an explicit tenant allowlist |
| **activated** | community, a **SET** | which available bundles a community has turned on | `hub_module_installations` (`000_create_base_schema.sql:259-269`) already stores one row per `(community_id, module_id)` with its own `is_enabled` — the table itself is already set-shaped; the single-winner narrowing happens one layer up, in `resolve_app` |

### 5.1 Proposed schema (supersedes `hub_module_installations`, per
`2026-08-26-v3-scbm-apps-design.md:293-294`: *"Renaming `hub_module_installations` to
`app_installations` is part of P1... only the name stops lying."*)

```sql
-- GLOBAL: installed
CREATE TABLE app_catalog (
    app_id                  TEXT PRIMARY KEY,          -- <github-org>.<repo>.<bundle-name>
    manifest_version        TEXT NOT NULL,              -- bundle.yaml `version`
    module                  TEXT NOT NULL,
    feature                 TEXT NOT NULL,
    provider                TEXT NOT NULL,               -- builtin | thirdparty
    execution_model         TEXT NOT NULL,               -- native | thirdparty
    is_default               BOOLEAN DEFAULT FALSE,
    compatible_with          TEXT[] DEFAULT '{}',
    incompatible_with        TEXT[] DEFAULT '{}',
    platform_compatibility   JSONB NOT NULL,             -- {tested_with,min_version,max_version}
    status                   TEXT DEFAULT 'active',       -- active | deprecated | yanked
    installed_at              TIMESTAMPTZ DEFAULT NOW()
);

-- TENANT: available
CREATE TABLE app_tenant_availability (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER NOT NULL REFERENCES tenants(id),
    app_id         TEXT NOT NULL REFERENCES app_catalog(app_id),
    available      BOOLEAN DEFAULT TRUE,
    config_defaults JSONB DEFAULT '{}',                  -- tenant-level override of bundle.yaml defaults
    UNIQUE(tenant_id, app_id)
);

-- COMMUNITY: activated (the set)
CREATE TABLE app_activations (
    id            SERIAL PRIMARY KEY,
    community_id  INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id),   -- denormalized, for ACL/stream scoping
    app_id        TEXT NOT NULL REFERENCES app_catalog(app_id),
    enabled       BOOLEAN DEFAULT TRUE,
    config        JSONB DEFAULT '{}',
    activated_by  INTEGER,
    activated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(community_id, app_id)
);
```

Enforcement (application layer, at write time — not expressible as a single SQL constraint
across three tables): inserting into `app_tenant_availability` requires the `app_id` to exist
in `app_catalog`; inserting into `app_activations` requires an *available* row for that
`(tenant_id, app_id)`. Exact migration path (rename vs new tables, backfill from
`hub_module_installations`/`marketplace_subscriptions`) is an **open decision**, §10.

### 5.2 Resolution: `resolve_app` → `resolve_apps`

`resolve_app` (`app_binding.py:92-133`) returns one `AppManifest`, narrowest-scope-wins.
Replace with:

```python
async def resolve_apps(
    feature: str, *, tenant: str, community: Optional[int],
    installations: InstallationLookup, registry: Optional[AppRegistry] = None,
) -> Sequence[AppManifest]:
    """Every *enabled, activated* App implementing `feature`, visible at (tenant, community).
    Community-scoped rows + tenant-wide rows both included (union, not override) — the
    binding ladder's narrowest-wins semantics no longer apply. Falls back to the Feature's
    shipped default only when the resulting set is empty."""
```

`InstallationLookup.find` (`app_binding.py:79-89`) is unchanged — it already returns
`Sequence[AppInstallation]`; `resolve_app` was the layer that collapsed it to one. `BindingError`
(`app_binding.py:33-39`) still applies to the empty-set-and-no-default case.

---

## 6. Distribution & control plane

How a bundle gets from "global admin installs it" to "`svc-ingest` runs its code for event N"
is three separate planes, each hitting a different DB tier for a different reason — conflating
them is how a routing lookup on the hot path ends up competing with an admin write for the
same connection pool.

### 6.1 Control plane (writes) — hub-api, primary

The global admin installs/uninstalls/updates bundles through **hub-api**
(`2026-08-26-v3-scbm-apps-design.md:681`, "admin + marketplace + tenant management"). hub-api
is the sole writer to `app_catalog` (§5.1): it writes install/uninstall/update to the
**primary/write** DB node, and stores/removes the bundle artifact (scripts + config + spec +
manifest, §2).

Install/uninstall/update is a plain REST surface on hub-api, not a separate control path —
**hub-webui and the CLI are both just REST clients of the same hub-api endpoints**, neither
owning install logic independently. This is the existing three-surface model
(`2026-08-26-v3-scbm-apps-design.md:867-877`), which already lists REST's consumers as
"browsers, mobile, CLI" (line 874) — the bundle SDK adds new REST routes under that surface,
not a new surface.

Uninstall is **global** — it removes the row from `app_catalog`, the installed set. Any
`app_tenant_availability`/`app_activations` rows still referencing that `app_id` are now
dangling; cascade vs. deny-if-referenced is an open decision, §10.

### 6.2 Code distribution (pull) — stage-runner poll + reconcile

Each stage-runner container (`svc-ingest`/`svc-process`/`svc-action`,
`2026-08-26-v3-scbm-apps-design.md:496-511`) **polls** hub-api on an interval for the current
global installed-bundle set, then reconciles locally: fetch and install newly-installed
bundles' code for its own stage, remove uninstalled ones' code. Polling, not push, so a
restarting or newly-scaled-out runner self-heals to the current installed set on its own first
poll — hub-api never needs to know a given runner replica exists.

Proposed endpoints (new; versioned per `backend.md`'s `/api/v{major}/{endpoint}` rule):

| Endpoint | Returns | Consumer |
|---|---|---|
| `GET /api/v1/apps/installed?stage=ingest` | every installed bundle with an `ingest` stage: `{app_id, version, artifact_hash}` | `svc-ingest` (and `process`/`action` with their own `stage=`) |
| `GET /api/v1/apps/{app_id}/artifact` | the bundle artifact for one `app_id` (script + config + spec + manifest for the requested stage) | any stage-runner, only for a hash it does not already hold |

`artifact_hash` per bundle is what lets a runner skip re-fetching unchanged bundles on every
poll — it fetches only when the hash it sees differs from what it has cached. `stage=` scopes
the response so a runner loads only the stage(s) it serves: `svc-ingest` installs each
bundle's `ingest` component only, never its `process`/`action` code.

### 6.3 Routing reads (hot path) — read replica

Per-community activation/routing — `app_activations` (§5.1) plus which activated bundles fan
out for a `(tenant, community, feature)` via `resolve_apps` (§5.2) — is read on **every
event**, not on a poll interval. Stage-runners read this from the **read replica**, never the
primary, so a routing lookup never loads the write node hub-api holds.

Per `backend.md`'s Database Tier Architecture ("every container has its own DB account scoped
to its tables/operations... Levels: read-only, read-write, scoped read-write (RLS), admin
(DDL)") and `security.md`'s checklist ("per-service DB accounts (no shared creds)"):
stage-runners get a **read-only** account scoped to the read replica; hub-api holds the
**write** account on the primary. Neither can do the other's job — a compromised stage-runner
cannot write `app_catalog`, and hub-api's write path never contends with the routing hot path
for read-replica connections.

Cache routing reads with a short TTL, invalidated on the stage-runner's own poll cycle (§6.2) —
the poll that refreshes the installed set is also the natural point to drop a stale routing
cache.

### 6.4 Two read paths, deliberately different

| | Installed set (code) | Routing (data) |
|---|---|---|
| What | which bundles exist, per stage | which activated bundles fan out for this `(tenant, community, feature)` |
| Frequency | rare — changes only on admin install/uninstall | every event |
| Path | poll hub-api (§6.2) | read replica (§6.3) |

Routing on every event through hub-api would hammer a low-volume admin API with the platform's
entire event throughput; code artifacts have no business in a per-event query path either.
These are two mechanisms on purpose, not two instances of one — installed-set is rare and is
code, so it goes through the API that serves artifacts; routing is frequent and is data, so it
goes straight to the tier built for high-read-volume queries, bypassing hub-api entirely.

---

## 7. Coexistence and conflict resolution

### 7.1 Fan-out

For a given inbound event, `svc-ingest` calls `resolve_apps(feature, tenant, community, ...)`
and runs **every** returned bundle's `ingest` stage independently — each publishes onto its
own isolated `process` stream (§7.2), so `svc-process` and `svc-action` fan out identically at
their own stage boundary. Four giveaway bundles activated in one community means four
independent ingest→process→action runs per matching chat event, not one shared run.

### 7.2 Isolation keys

Today's stream naming is **two generations behind this proposal** — worth stating precisely
because both existing schemes are visible in the codebase and neither is per-app:

| Scheme | Source | Shape |
|---|---|---|
| Legacy, actually in code | `stream_pipeline.py:64-67,78-104` | `{stream_prefix}:{stream}` — default prefix `"waddlebot:stream"`, stream names like `"events:inbound"` — no tenant, no community, no app |
| Design-doc target, **not yet implemented** | `2026-08-26-v3-scbm-apps-design.md:571-573` | `waddles:t:{tenant}:{stage}` — tenant-scoped, but still one shared stream per stage across every bundle |
| **This proposal** | — | `waddles:t:{tenant}:c:{community}:app:{app_id}:{stage}` |

`{stage}` takes all three values independently — `ingest`, `process`, and `action` each get
their own stream under this key. The process→action queue hop (§1) is the stream at
`waddles:t:{tenant}:c:{community}:app:{app_id}:action` — `process` publishes there (its
`produces`), `action` consumes from there (its `consumes`); it is a stream like every other
boundary, never a direct call between the two stage-runner containers.

Config/state follow the same shape: `waddles:t:{tenant}:c:{community}:app:{app_id}:cfg` and
`:state`. Tenant-wide activations (`community_id IS NULL` in `AppInstallation`,
`app_binding.py:63,58-60`) use a literal `c:_tenant` segment rather than omitting the segment,
so every key is uniformly parseable. Consumer group naming: `{app_id}:{stage}-group`, per the
existing `create_consumer_group` mechanism (`stream_pipeline.py:440-493`) — unchanged
mechanism, new group-name convention.

This reconciles, not replaces, the design doc's tenant ACL-user scheme
(`2026-08-26-v3-scbm-apps-design.md:586-599`, `waddles-t-{tenant}-{stage}`) — that stays a
per-(tenant, stage) credential; the stream *key* underneath it now also carries community and
app_id.

### 7.3 Conflict enforcement

`compatible_with`/`incompatible_with` (§3.1) are `app_id` lists, package-manager
`Provides`/`Conflicts`-style. Coexistence is the default (empty `incompatible_with` = no
restriction). Enforcement point: `app_activations` insert/update (§5.1) — before activating
`app_id` X in a community, check every currently-`enabled=true` row in that community for
`app_id` Y where `Y in X.incompatible_with OR X.app_id in Y.incompatible_with` (symmetric
check — a declares b incompatible is enough, regardless of which side declares it). A hit
blocks the activation with a typed error (same `REASON_*`-coded pattern as `ManifestError`/
`RegistryError`), not a silent skip. The community must deactivate the conflicting bundle
first — mutual exclusion is a manual choice, never auto-resolved.

`compatible_with` is currently unused by the enforcement rule above (conflict detection only
needs `incompatible_with`) — it is carried for future use as an explicit "these are known to
work together" allowlist, e.g. gating an integration test matrix. Whether it should instead
drive something enforced (like requiring at least one `compatible_with` entry present) is an
open question, not decided here.

---

## 8. Execution models

| | Native | Third-party |
|---|---|---|
| `execution_model` | `native` | `thirdparty` |
| Runs | in-process inside the stage-runner container, loaded via `entrypoint` | never in-process — always a network hop (`2026-08-26-v3-scbm-apps-design.md:446-472`) |
| Stage scope | may span all 3 stages | always exactly one stage per manifest block (§3.2) |
| Transport | direct function call from the stage-runner's dispatch loop | `webhook_push` (we call them, HMAC-SHA256, `webhook_secret`) or `rest_pull` (we call their API, `api_key`/`oauth2_client_credentials` bearer, or HMAC fallback) — both implemented today in `vendorExecutionService.executeCommand()` (`vendorExecutionService.js:25-102`) |
| Auth | `requires_scopes` checked against the caller's JWT scopes, in-process | HMAC over payload (`webhook_push`) or bearer/HMAC per `auth_type` (`rest_pull`) — `059_marketplace_consolidation.sql:17-18,27-30` |
| Failure mode | exception → DLQ (`stream_pipeline.py:368-438`) | timeout (`webhook_timeout_ms`, default 5000ms) + non-2xx → DLQ; inherits network latency/failure the native path doesn't have |
| Isolation | shares the stage-runner process — sandboxing is an **open decision**, §10 | already isolated by construction (separate process/network boundary) |

---

## 9. Worked example: `giveaway` bundle

```
giveaway-classic/
├── bundle.yaml
├── ingest/{handler.py, config.yaml, spec.yaml}
├── process/{handler.py, config.yaml, spec.yaml}
└── action/{handler.py, config.yaml, spec.yaml}
```

`bundle.yaml`:

```yaml
app_id: waddles.bot.giveaway.giveaway-classic
name: "Giveaway (Classic)"
version: "1.0.0"
feature: waddles.bot.giveaway
module: bot
provider: builtin
execution_model: native
is_default: true
requires_scopes: ["bot.command:write", "bot.chat:read"]
compatible_with: []
incompatible_with: []
platform_compatibility:
  tested_with: "v3.0.x"
  min_version: "3.0.0"
  max_version: "3.999.999"
stages:
  ingest:
    entrypoint: "ingest/handler.py:on_event"
    consumes: []
    produces: ["giveaway.entry_detected"]
    config: "ingest/config.yaml"
    spec: "ingest/spec.yaml"
  process:
    entrypoint: "process/handler.py:on_event"
    consumes: ["giveaway.entry_detected"]
    produces: ["giveaway.winner_selected"]
    config: "process/config.yaml"
    spec: "process/spec.yaml"
  action:
    entrypoint: "action/handler.py:on_event"
    consumes: ["giveaway.winner_selected"]
    produces: []
    config: "action/config.yaml"
    spec: "action/spec.yaml"
```

Stage specs (`spec.yaml`), abbreviated:

- `ingest/spec.yaml`: watches chat for `!giveaway enter`; emits `giveaway.entry_detected {user_uuid, community_id, ts}` (PII-tokenized per `backend-database.md` — chat username resolved via the identity table, never carried in the event itself).
- `process/spec.yaml`: accumulates entrants per active giveaway window in this instance's isolated state key (`waddles:t:{tenant}:c:{community}:app:waddles.bot.giveaway.giveaway-classic:state`); on window close, picks a winner, emits `giveaway.winner_selected {winner_uuid, giveaway_id}`.
- `action/spec.yaml`: posts the winner announcement via the platform's outbound chat action.

Two queue hops, not one: `ingest.produces["giveaway.entry_detected"]` ==
`process.consumes["giveaway.entry_detected"]` is the ingest→process stream
(`...:giveaway-classic:process`); `process.produces["giveaway.winner_selected"]` ==
`action.consumes["giveaway.winner_selected"]` is the *second*, independent process→action
stream (`...:giveaway-classic:action`, §7.2). `action` can take as long as posting the
announcement takes without ever blocking `process` from picking the next window's winner —
it only backs up its own `:action` stream.

### 9.1 Coexistence — 4 giveaway bundles in one community

Community `C1` activates `giveaway-classic`, `giveaway-raffle`, `giveaway-milestone`,
`giveaway-sub-only` — 4 rows in `app_activations`, all `enabled=true`, all implementing
`waddles.bot.giveaway`. `resolve_apps("waddles.bot.giveaway", tenant=T, community=C1, ...)`
returns all 4. Each gets its own isolated stream set:

```
waddles:t:T:c:C1:app:waddles.bot.giveaway.giveaway-classic:{ingest,process,action,dlq}
waddles:t:T:c:C1:app:waddles.bot.giveaway.giveaway-raffle:{ingest,process,action,dlq}
waddles:t:T:c:C1:app:waddles.bot.giveaway.giveaway-milestone:{ingest,process,action,dlq}
waddles:t:T:c:C1:app:waddles.bot.giveaway.giveaway-sub-only:{ingest,process,action,dlq}
```

A `!giveaway enter` chat event is evaluated by all 4 ingest handlers independently — no shared
state, no interference, per §7.1/§7.2.

### 9.2 Conflict — 2 incompatible Twitch bundles

`waddles.bot.twitch-chat.official-eventsub` and `waddles.bot.twitch-chat.legacy-irc-bridge`
both implement `waddles.bot.twitch-chat` but cannot both hold the platform's single Twitch
IRC/EventSub connection for one channel. Each declares:

```yaml
# official-eventsub/bundle.yaml
incompatible_with: [waddles.bot.twitch-chat.legacy-irc-bridge]
```

Activating `legacy-irc-bridge` in a community that already has `official-eventsub` enabled is
rejected at the `app_activations` write (§7.3) — the community must deactivate one before the
other can be turned on.

---

## 10. Open decisions (for the user — not decided here)

| # | Decision | Why it's open |
|---|---|---|
| 1 | **Native-script sandboxing model** — subprocess, WASM, or a separate pod per activation, vs. today's implicit in-process trust | Community/third-party-authored *native* scripts running inside the shared `svc-ingest`/`svc-process`/`svc-action` process is a different trust boundary than the existing "vendor code never runs in our process" guarantee (`2026-08-26-v3-scbm-apps-design.md:460-465`) — security-sensitive, needs an explicit call |
| 2 | **Config precedence** — bundle default (`config.yaml`) → tenant (`app_tenant_availability.config_defaults`) → community (`app_activations.config`) — confirm this is strictly narrowest-wins-overrides, and whether deep-merge or full-replace at each layer |
| 3 | **`bundle.yaml` ↔ `AppManifest` reconciliation** — rename `permissions`→`requires_scopes` on the dataclass itself, or alias at parse time (§3.3); same question for `surfaces` vs the new `stage_specs` field |
| 4 | **`compatible_with` as declared IDs vs. an exclusive "provides" capability** — current proposal is explicit `app_id` lists (package-manager style); an alternative is a `provides: [capability]` + "only one provider of a given exclusive capability may be active," which scales better as the catalog grows but is a bigger schema change |
| 5 | **Exact `app_installations`/`app_catalog`/`app_tenant_availability` migration** — new tables (as drafted, §5.1) vs. renaming `hub_module_installations` in place and adding the two new tiers around it; backfill plan for existing `hub_module_installations`/`marketplace_subscriptions` rows |
| 6 | **`platform_compatibility` enforcement policy** (§3.4) — block-out-of-range/warn-untested-in-range is a proposed default, not confirmed; also needs a canonical "running platform version" source, which doesn't exist today (`flask_core.__version__` is stale) |
| 7 | **Uninstall cascade policy** (§6.1) — when hub-api removes an `app_id` from `app_catalog`, whether dangling `app_tenant_availability`/`app_activations` rows referencing it cascade-delete or block the uninstall until the tenant/community deactivates first |
| 8 | **Poll interval + artifact store location** (§6.2) — how often a stage-runner polls hub-api for the installed set, and whether the bundle artifact itself lives in the DB (BLOB/JSONB), object storage, or an OCI registry (image-layer-style distribution) |

---

## 11. What changes from today (gap list)

| Today | Becomes |
|---|---|
| `AppManifest.surfaces` — tuple of stage **names** only (`app_manifest.py:110`) | `stages` map in `bundle.yaml` with per-stage `entrypoint`/`consumes`/`produces`/`config`/`spec`; `AppManifest` gains `stage_specs`, keeps `surfaces` as a derived compat field |
| `resolve_app` — single winner, narrowest-scope-wins (`app_binding.py:92-133`) | `resolve_apps` — returns the full enabled/activated set for a feature; fan-out, not override |
| `hub_module_installations` (`000_create_base_schema.sql:259-269`) — one flat `(community_id, module_id)` table, conflates install/available/activate | 3-tier `app_catalog` / `app_tenant_availability` / `app_activations`, `activated ⊆ available ⊆ installed` enforced at the application layer |
| No conflict field anywhere in `marketplace_modules` or `AppManifest` | `compatible_with`/`incompatible_with` on `bundle.yaml`, enforced at `app_activations` write |
| `stream_pipeline.py`'s `{stream_prefix}:{stream}` (legacy) / design doc's `waddles:t:{tenant}:{stage}` (target, unimplemented) — neither is per-community or per-app | `waddles:t:{tenant}:c:{community}:app:{app_id}:{stage}` — full per-(community × app) isolation |
| No platform-version compatibility declaration anywhere in `AppManifest` or `marketplace_modules` | `platform_compatibility` (`tested_with`/`min_version`/`max_version`) on `bundle.yaml`, enforced at install/available/activate |
| `permissions` field name on `AppManifest` (`app_manifest.py:111`) vs. `requires_scopes` in the design doc's own example (`2026-08-26-v3-scbm-apps-design.md:409`) | one name, `requires_scopes`, used consistently in `bundle.yaml` (§3.3) |
| No distribution mechanism — `AppRegistry` is load-once at process startup (`app_registry.py:16-21`), nothing pushes/pulls bundle code to a running container | hub-api is the sole `app_catalog` writer (primary); stage-runners poll hub-api and reconcile their own stage's code; routing reads go to the read replica, never hub-api or the primary (§6) |
| No presentation/UI component anywhere in `AppManifest`/`bundle.yaml` — a bundle is only ever `{ingest,process,action}` script stages | `presentation` (§3.2a), a 4th, non-script bundle component: a client-side HTML/JS overlay (`html_entrypoint`/`assets`/`browser_source_path`) served per-community by `svc-presentation` for an OBS browser source, outside the ingest→process→action queue chain |
