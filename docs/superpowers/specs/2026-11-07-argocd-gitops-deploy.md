# Spec: ArgoCD GitOps Deploy (replace GHA `helm upgrade`)

Status: DRAFT 2026-11-07 — design only, no implementation. Chart versions marked "to verify" must be re-checked at implementation time.

## Locked decisions

| # | Decision |
|---|---|
| 1 | One ArgoCD per environment cluster, installed IN that cluster. dal2alpha first; dal2beta/gamma/prod later. No central mgmt cluster. |
| 2 | Product repos own deployment specifics (Application, values, hooks, verification). `penguintechinc/admin` holds only GENERIC config: ArgoCD install, Image Updater install, registry-cred wiring, and ONE tiny bootstrap file per product per cluster. |
| 5 | **Isolation per product per environment** (~10 products: waddles, waddleai, elder, nest, killkrill, ...). A break in one product's deploy must never affect another's. Shared control plane, isolated everything else (see Isolation model). |
| 3 | Image tags flow via ArgoCD Image Updater, NOT CI committing tags. Alpha write-back = `argocd` (no git creds). `git` write-back = option for beta/prod. |
| 4 | CI = build + push only. Helm deploy job removed only AFTER ArgoCD has synced alpha successfully once. |

## Goals / non-goals

| Goals | Non-goals |
|---|---|
| Pull-based deploy: cluster reconciles from git, runner needs no kube creds | Central multi-cluster ArgoCD; cross-product sync dependencies |
| Drift detection + self-heal on alpha | Replacing image builds (stay on dal2 ARC runner + buildkitd) |
| Preserve ordering: migrations -> svc_action -> svc_ingest -> rest | Beta/gamma/prod rollout (follow-on specs; alpha only here) |
| Preserve count-verified post-deploy checks | SSO for ArgoCD (later, noted) |
| Cut-over with zero downtime gap and one-step rollback | ApplicationSet / multi-tenant Projects beyond waddles |

## Architecture

```
 GHA (dal2 ARC runner)                       dal2alpha cluster
 push release/v3.0.X                         ┌──────────────────────────────────────────────┐
   │ build 12 images (buildkitd)             │ ns argocd                                    │
   ▼                                         │  ┌──────────┐   ┌─────────────────────┐      │
 ghcr.io/penguintechinc/waddles/<svc>:       │  │ argocd   │   │ argocd-image-updater│      │
   alpha-<epoch64>  ──────────────────────────▶│ repo-srv │◀──│ polls ghcr (creds)  │      │
                                             │  │ app-ctrl │   │ write-back: argocd  │      │
 git (waddles @ release/v3.0.X) ◀─── pull ───│  └────┬─────┘   └─────────────────────┘      │
 git (admin: per-product bootstrap) ◀──── pull ───│       │ sync (waves)                         │
                                             │       ▼                                      │
                                             │ ns waddlebot: PreSync migrations Job ->      │
                                             │   wave0 infra -> wave1 svc_action ->         │
                                             │   wave2 svc_ingest -> wave3 rest ->          │
                                             │   PostSync seed jobs + verify Job            │
                                             └──────────────────────────────────────────────┘
 admin: k8s/argocd/dal2alpha/products/<product>.yaml (AppProject + 1 root App) - one file per product
   ├─ waddles  root ──▶ waddles repo deploy/argocd/dal2alpha/*  ──▶ ns waddlebot
   ├─ waddleai root ──▶ waddleai repo deploy/argocd/dal2alpha/* ──▶ ns waddleai
   └─ ...      (each root syncs independently; no shared app-of-apps)
```

## Repo layout

### admin (`penguintechinc/admin`) — generic only

Follows existing `k8s/arc-runners/` pattern (wrapper chart, vendored `charts/*.tgz`, `Chart.lock`, `alpha.yml`, README).

| Path | Purpose |
|---|---|
| `k8s/argocd/Chart.yaml` + `Chart.lock` | Wrapper chart, dependency `argo-cd` pinned |
| `k8s/argocd/values.yaml` | Env-neutral defaults (rootless, no ingress, redis-ha off) |
| `k8s/argocd/alpha.yml` | dal2alpha overrides (resources, repo URLs, NodePort-free, RBAC cm) |
| `k8s/argocd/templates/networkpolicy.yaml` | CNP egress: github.com, ghcr.io, kube-apiserver, DNS |
| `k8s/argocd/templates/tetragon.yaml` | TracingPolicy allowlist additions for argocd binaries (only if enforcement blocks) |
| `k8s/argocd/dal2alpha/products/waddles.yaml` | Per-product bootstrap: namespace + ResourceQuota + LimitRange + AppProject `waddles` + ONE root Application -> waddles repo `deploy/argocd/dal2alpha/`. Template for every other product (`<product>.yaml`) |
| `k8s/argocd/dal2alpha/products/README.md` | Onboarding: copy file, change name, repoURL, namespace, quota |
| `k8s/argocd/SETUP.md` | Install runbook: pinned versions, helm v3.17.3, secrets, verify |
| `k8s/argocd-image-updater/Chart.yaml` + `Chart.lock` | Wrapper, dep `argocd-image-updater` pinned |
| `k8s/argocd-image-updater/values.yaml`, `alpha.yml` | Registry `ghcr`, creds ref, log level |
| `k8s/argocd-image-updater/SETUP.md` | Creds wiring (Secret created by operator, never in git) |
| (removed) `k8s/argocd-root/` | No shared app-of-apps; replaced by per-product files above |

Discovery contract: each product's root Application sources its own repo path `deploy/argocd/<cluster>/` (`directory.recurse: false`). Onboarding a product = one new file in admin; nothing shared is edited, so one bad product manifest cannot break discovery for others.

### waddles (`penguintechinc/waddles`) — product specifics

| Path | Purpose |
|---|---|
| `deploy/argocd/dal2alpha/waddlebot-app.yaml` | The `Application` (source, valueFiles, waves, Image Updater annotations) |
| `k8s/helm/waddlebot/values-argocd-alpha.yaml` | Thin overlay (only if needed; else reuse `values-alpha.yaml` + `values-dal2alpha.yaml`) |
| `k8s/helm/waddlebot/templates/verify-postsync-job.yaml` | PostSync count-verified smoke Job (flag-gated `argocd.verifyHook.enabled`) |
| `k8s/helm/waddlebot/templates/verify-postsync-rbac.yaml` | Namespaced SA/Role/RoleBinding for verify Job |
| `.github/workflows/deploy-dal2alpha.yml` | Phase 3: drop `deploy` job; keep `prepare`+`build` |
| `.github/workflows/alpha-smoke.yml` | Optional: `workflow_dispatch` smoke (see Verification) |
| `docs/deploy/argocd-alpha.md` | Operator doc: sync, rollback, tag pinning |

## Pinned versions (all "to verify" at implementation)

| Component | Chart | Notes |
|---|---|---|
| argo-cd | `argo/argo-cd` **10.15.0** (latest seen 2026-11-07; to verify) | Pin exact chart + app image by digest in prod; vendor `.tgz` like arc-runners |
| argocd-image-updater | `argo/argocd-image-updater` **1.4.2** (to verify) | v1.x uses `ImageUpdater` CRD; v0.x used annotations. See open question 1 |
| helm (ArgoCD repo-server) | **3.17.3** | helm v4 fresh-install managedFields bug; bake into repo-server via custom image/initContainer, verify `helm version` in pod |

## Application spec (waddles, alpha)

| Field | Value |
|---|---|
| name / project | `waddlebot-alpha` / `waddles` (AppProject per product) |
| source | `repoURL: https://github.com/penguintechinc/waddles` `targetRevision: release/v3.0.X` `path: k8s/helm/waddlebot` |
| helm.valueFiles | `values-alpha.yaml`, `values-dal2alpha.yaml` (same order as current workflow) |
| helm.releaseName | `waddlebot` (must equal current release name for adoption; see cut-over) |
| destination | `https://kubernetes.default.svc`, ns `waddlebot` |
| syncOptions | `CreateNamespace=false` (ns pre-exists), `ServerSideApply=true`, `PruneLast=true`, `ApplyOutOfSyncOnly=true` |
| ignoreDifferences | Secret `waddlebot-platform-credentials` data (operator-owned); Deployment `/spec/replicas` where HPA owns |
| repo access | Public repo -> no creds. If private: read-only deploy key / fine-grained PAT in Secret in `argocd` ns (operator-created) |

### syncPolicy decision

| Option | Pros | Risks |
|---|---|---|
| **A (recommended alpha)**: `automated: {selfHeal: true, prune: true}` | Matches "alpha is disposable"; image bumps deploy hands-free; drift reverted | prune can delete a resource removed from chart by mistake (incl. PVC-backed stateful sets); selfHeal fights manual `kubectl` debug edits |
| B: automated, `prune: false` | Safer for PVCs | Orphans accumulate; renamed resources linger |
| C: manual sync | Safest | Defeats Image Updater auto-deploy |

Guardrails for A:
- `argocd.argoproj.io/sync-options: Prune=false` on PVCs, Secret `waddlebot-platform-credentials`, Postgres/Redis/SeaweedFS StatefulSet/Deployment+PVC (chart annotation via values).
- Secret `waddlebot-platform-credentials`: NOT in chart output; additionally `Prune=false` and in `ignoreDifferences`. Never `REPLACE_ME` clobber (see helm-credential-clobber incident) — chart must keep `existingSecret` path.
- Debug: `argocd app set waddlebot-alpha --sync-policy none` (or annotate) before manual edits; document in `argocd-alpha.md`.
- beta/prod: automated sync OFF for prod (manual gate), prune=false initially.

### Hook + sync wave ordering

Current Helm hooks and how ArgoCD maps them (ArgoCD maps `pre-install/pre-upgrade` -> PreSync, `post-install/post-upgrade` -> PostSync; `helm.sh/hook-weight` -> sync-wave within phase):

| Resource | Current helm hook | ArgoCD behavior | Action |
|---|---|---|---|
| auto-provisioned-keys RBAC + Job | pre-install,pre-upgrade, wt -10/-6 | PreSync, waves -10/-6 | OK as-is |
| **migrations Job + RBAC** | **post-install,pre-upgrade**, wt -5/-10 | Maps to BOTH PreSync and PostSync (runs twice per sync) | **Change**: annotate natively `argocd.argoproj.io/hook: PreSync` + `hook-delete-policy: BeforeHookCreation`, drop post-install mapping (ArgoCD has no "install vs upgrade"; PreSync Job must be idempotent on fresh DB) |
| core-bundle-seeder, hub-admin-seed, bundle-signing-backfill, seaweedfs bucket job | post-install,post-upgrade, wt 0/1/5/10 | PostSync, waves preserved | OK; confirm idempotent |
| hook-delete-policy `hook-succeeded` | helm | Maps to `HookSucceeded` | OK |

**Fresh-install gotcha**: on first sync ArgoCD runs PreSync BEFORE any app resource exists; migrations Job needs DB. Postgres is part of the chart -> PreSync runs before Postgres exists. Mitigation (pick in impl, open question 3): (a) Postgres + secrets as wave `-20` normal resources and migrations as `Sync` wave `-5` Job (not PreSync); or (b) keep PreSync and bootstrap Postgres first via separate Application. Spec recommends (a): migrations = `argocd.argoproj.io/sync-wave: "-5"` (plain Sync-phase Job) after infra waves, before app waves.

Wave plan (Sync phase; lower first; ArgoCD waits for Healthy per wave):

| Wave | Resources |
|---|---|
| -20 | Namespace-scoped prereqs: ServiceAccounts, ConfigMaps, ExternalSecret/auto-provisioned secrets, cert-manager Certificates, NetworkPolicies |
| -15 | Infra: Postgres (strategy **Recreate**), Redis/Valkey, SeaweedFS, Qdrant |
| -5 | migrations Job (must complete before any app Deployment rolls) |
| 0 | hub-api |
| 1 | **svc_action(-rust)** + bundle-executor (must precede ingest: pin_sig v2) |
| 2 | **svc_ingest(-rust)** |
| 3 | svc-process, svc-presentation, svc-streaming, reputation, hub-webui, egress-proxy, CronJobs |
| PostSync | seeders (existing hooks) -> verify Job (wave 100) |

Wave annotations are added in chart templates (`argocd.argoproj.io/sync-wave`), inert under plain `helm upgrade`, so the current deploy keeps working during cut-over.

## Isolation model (per product, per environment)

AppProject per product (admin-owned, in `products/<product>.yaml`):

| Field | Value |
|---|---|
| `sourceRepos` | ONLY that product's repo |
| `destinations` | ONLY that product's namespace(s) on this cluster (`https://kubernetes.default.svc`) |
| `clusterResourceWhitelist` | `[]` (or minimal explicit list, each entry justified) |
| `namespaceResourceBlacklist` | `ResourceQuota`, `LimitRange`, `CiliumClusterwideNetworkPolicy`; product cannot loosen its own quota/baseline |
| `roles` | Project-scoped RBAC (`<product>-deployer`, `<product>-readonly`), SSO groups later; e.g. `p, proj:<product>:deployer, applications, sync, <product>/*, allow` |
| `orphanedResources.warn` | true |
| `syncWindows` | optional per product |

Rules:
- Separate Applications per product; **no cross-product sync waves or dependencies** (waves are intra-Application only). Cross-product needs are runtime contracts, not deploy ordering.
- Per-product namespace gets `ResourceQuota` + `LimitRange` (admin-owned, in the bootstrap file) so one product cannot starve another.
- Root Application per product: a bad manifest in product A's `deploy/argocd/<cluster>/` degrades only A's root.
- Repo credentials per product (one scoped cred Secret per repo); registry pull creds per product namespace.

### ApplicationSet vs per-product bootstrap files

| Option | Blast radius | Verdict |
|---|---|---|
| **Per-product bootstrap file** | One file = one product | Chosen: failure domain = 1 product |
| Single ApplicationSet over all products | One malformed element/template error stalls generation for ALL products | Rejected |
| ApplicationSet per product | Isolated, but extra CRD/generator indirection for 1 root per product per cluster | Rejected now; revisit if a product wants templating (generator lives in the PRODUCT repo, bound by its AppProject) |

Admin applies bootstrap files per product (operator `kubectl apply -f`), NOT via one admin Application syncing the whole `products/` dir (reintroduces a shared failure point). If automated later: one admin Application per product file.

### Shared per cluster vs isolated per product

| Component | Scope | Blast radius / mitigation |
|---|---|---|
| ArgoCD control plane (server, repo-server, controller, redis) | Shared per cluster | Outage pauses ALL products' deploys (running workloads unaffected). HA where env warrants, resource limits, pinned versions, upgrade alpha first |
| Repo-server (manifest generation) | Shared | A huge/slow chart can starve others: repo-server replicas/limits, parallelism limit, exec timeout |
| Image Updater | Shared per cluster | Registry rate limits / ghcr outage / bad cred stall tag updates for all, never break running pods. Mitigate: per-product registry creds (`pull-secret` per Application), staggered per-app intervals, strict `allow-tags` regex, per-Application failure (bad image skips only that app), alert on updater errors |
| ArgoCD chart/CRD versions | Shared | Pin; alpha first; CRD upgrades affect all |
| Cluster prerequisites (cert-manager, Cilium, CRDs) | Shared, admin-owned | Product projects cannot modify (empty `clusterResourceWhitelist`) |
| AppProject | Isolated | Cannot deploy outside own ns/repo |
| Root App + bootstrap file | Isolated | Own failure domain |
| Applications + sync state | Isolated | No cross-product waves |
| Namespace, ResourceQuota, LimitRange, CNP | Isolated | Starvation/egress contained |
| Repo creds, registry creds | Isolated | Leak/rotation affects one product |
| Image Updater annotations/settings | Isolated (on that product's Applications only) | Misconfig affects one app |
| RBAC roles / SSO groups | Isolated (project-scoped) | Deployer of A has no rights on B |

## Image Updater

Services (12) and chart keys. Registry `ghcr.io/penguintechinc/waddles/<svc>`. Common: `allow-tags: regexp:^alpha-\d+$`, `update-strategy: newest-build`, `pull-secret: pullsecret:argocd/ghcr-readonly`, write-back `argocd`.

| # | Alias | Image | Helm param (`image-tag`) |
|---|---|---|---|
| 1 | hubapi | hub-api | `hubApi.image.imageTag` (verify key) |
| 2 | webui | hub-webui | verify key |
| 3 | migrations | migrations | verify key |
| 4 | seeder | core-bundle-seeder | verify key |
| 5 | reputation | reputation-module | `reputation.image.tag` |
| 6 | executor | bundle-executor | verify key |
| 7 | ingest | svc-ingest-rust | verify key |
| 8 | process | svc-process-rust | verify key |
| 9 | action | svc-action-rust | verify key |
| 10 | presentation | svc-presentation-rust | verify key |
| 11 | egress | egress-proxy | verify key |
| 12 | streaming | svc-streaming (tag has `-cpu` suffix: regex `^alpha-\d+-cpu$`, chart appends variant -> map param to prefix) | `streaming.image.tag` |

Annotation shape (per alias `<a>`; classic form, CRD equivalent in v1.x):
```
argocd-image-updater.argoproj.io/image-list: hubapi=ghcr.io/penguintechinc/waddles/hub-api,...
argocd-image-updater.argoproj.io/<a>.update-strategy: newest-build
argocd-image-updater.argoproj.io/<a>.allow-tags: regexp:^alpha-\d+$
argocd-image-updater.argoproj.io/<a>.helm.image-tag: <chart key>
argocd-image-updater.argoproj.io/<a>.pull-secret: pullsecret:argocd/ghcr-readonly
argocd-image-updater.argoproj.io/write-back-method: argocd
```

Image Updater is shared per cluster but configured per product: annotations, `pull-secret`, and interval live only on that product's Applications; see blast-radius row in Isolation model.

**Skew risk (key design issue)**: chart today uses ONE `global.imageTag` for all images. 12 independent trackers can roll partial sets (e.g. svc_ingest new before svc_action new -> breaks pin_sig v2 ordering; builds finish at different times). Options:

| Option | Mechanism | Trade-off |
|---|---|---|
| **1 (recommended)**: sentinel image | CI pushes tiny `waddles/alpha-release:alpha-<epoch>` as LAST step after all 12 builds succeed; Image Updater tracks only it -> sets `global.imageTag`; all 12 images already exist at that tag | Atomic set; needs 1 extra CI step; deviates from "annotations per image" (annotations kept for the 12 only if option 2) |
| 2: per-image trackers | 12 aliases as table above | Skew; waves only order within one sync, not across syncs |
| 3: `git` write-back | One commit bumps `global.imageTag` | Needs git creds; better audit trail; for beta/prod |

Write-back `argocd` caveats: override lives in Application CR (survives only while Application exists; root app must `ignoreDifferences` on `/spec/source/helm/parameters` or selfHeal on the root reverts it — **must be configured**). Tag not in git -> git alone does not tell what alpha runs; use `argocd app get`.

`git` write-back (beta/prod option): pro = auditable, reproducible, rebuildable cluster; con = needs write cred scoped to one path, bot commits to release branch (branch protection/CODEOWNERS/required checks conflict -> use dedicated `argocd-image-updater/*` branch + PR, or separate deploy-values repo), commit noise.

## Post-deploy verification

Current checks (deploy-dal2alpha.yml smoke step) -> new home:

| Check | New home |
|---|---|
| 1 all Deployments ready, denominator > 0 | PostSync verify Job (kubectl, namespaced Role: get/list deploy, pods; exec on hub-api) |
| 2 no non-Running/Succeeded pods, count > 0 | PostSync verify Job |
| 3 images carry expected tag | Job reads `global.imageTag` via downward env from chart (`IMAGE_TAG`); asserts count > 0 |
| 4 hub-api `/health` + `/ready` HTTP 200 | PostSync verify Job (in-cluster HTTP to Service, no `exec` needed) |

- PostSync Job: `argocd.argoproj.io/hook: PostSync`, `hook-delete-policy: BeforeHookCreation`, wave 100, `set -euo pipefail`, prints denominators, fails loud. Failed PostSync -> app Degraded (visible; no auto-rollback).
- Complement: `alpha-smoke.yml` (`workflow_dispatch` + optional `workflow_run` after build) runs `argocd app wait waddlebot-alpha --health --timeout 900` + same asserts from outside; used for GH Environment `dal2-alpha` gate visibility. Needs ArgoCD API reach from runner (in-cluster ARC runner -> `argocd-server.argocd.svc`) and a read-only token (Secret, masked).
- Tetragon: Job image must contain only allowlisted binaries; reuse chart's existing smoke image.

## Security

| Area | Design |
|---|---|
| RBAC | AppProject `waddles` (one per product): destinations = ns `waddlebot` only; sourceRepos = waddles repo only; `clusterResourceWhitelist` = empty (chart is namespaced; CRDs/ClusterIssuer are admin-owned) -> if waddles chart renders cluster-scoped objects, list them explicitly. argocd application-controller default is cluster-admin; scope down: controller ClusterRole limited + namespaced Role in `waddlebot` (`argocd-manager`-style) — verify controller still starts (needs list/watch for cache); fall back to namespaced-mode (`application.namespaces` + `controller.clusterRoleRules`) if chart supports |
| CNP | `argocd` ns default-deny; egress allow: DNS, kube-apiserver, `github.com`, `ghcr.io` (+ `pkg-containers.githubusercontent.com` for blobs, verify); intra-ns free. Image Updater egress: ghcr.io + argocd-server. Cross-ns: argocd -> waddlebot only via API server (no pod traffic) |
| Tetragon | Allowlist argocd-repo-server binaries (`helm`, `git`, `kustomize`, `ssh`, `gpg`); validate with enforced policy before cut-over; fallback = scoped additive TracingPolicy in admin `templates/tetragon.yaml` |
| PSA | `argocd` ns `restricted`; rootless images, `runAsNonRoot`, read-only rootfs |
| Admin password | Initial `argocd-initial-admin-secret` read once by operator -> password manager -> **delete secret**; `admin.enabled: false` once SSO/local user exists; never in git or values. Bcrypt hash via `--set` from env at install only |
| Secrets | `ghcr-readonly` (read:packages PAT/token) + optional repo cred: operator-created Secrets, documented in SETUP.md, never committed (Token hygiene: stdin/env, masked) |
| Exposure | No Ingress initially; access via `kubectl port-forward`. Later: Gateway + TLS + SSO (OIDC) behind CF Access |
| mTLS/SPIFFE | n/a (infra tool); reserve ID `spiffe://penguintech.io/alpha/argocd` per convention |
| Supply chain | Pin chart + image digests; Image Updater restricted by regex to `alpha-<digits>`; ghcr package write limited to CI |
| Trusted-code | Unchanged ARC runner constraint (no PR/fork builds) |

## Cut-over plan (dal2alpha)

| Phase | Steps | Gate |
|---|---|---|
| 0 Prep | Merge chart changes (waves, hook remap, verify Job; all inert under helm) — **waddles PR** | Existing GHA deploy still green |
| 1 Install | Install argocd + Image Updater in dal2alpha (admin PRs; `helm` v3.17.3; `SETUP.md`); CNP + Tetragon verified; AppProject created | `argocd` pods Ready; repo-server can clone + `helm template` |
| 2 Adopt (no change) | Create Application with **manual sync, prune off, Image Updater off**. `argocd app diff` vs live Helm release; fix diffs. Existing release secrets `sh.helm.release.v1.*` are not used by ArgoCD (it renders templates); remove stale release secrets only after Phase 4 | Diff = empty/expected-only |
| 3 First sync | Manual `argocd app sync` at the currently-deployed tag; verify PreSync/waves/PostSync verify Job pass; Secret `waddlebot-platform-credentials` untouched (assert hash before/after) | Healthy + Synced + verify Job green — **"synced successfully once"** |
| 4 Enable automation | Turn on automated sync (selfHeal+prune w/ guardrails) and Image Updater (sentinel or per-image). Trigger one real push -> observe end-to-end | One auto-rollout green |
| 5 Remove GHA deploy | PR removes `deploy` job (and runner kube RBAC for deploy) from `deploy-dal2alpha.yml`; keep `prepare`+`build`+sentinel push | Next merge deploys only via ArgoCD |

Rollback:

| Scenario | Action |
|---|---|
| Bad image | `argocd app set ... -p global.imageTag=<prev>` (disable Image Updater for app first) or `argocd app rollback`; or re-push prior tag |
| Bad chart | revert git commit on release/v3.0.X; ArgoCD resyncs |
| ArgoCD itself broken (Phases 2-4) | `argocd app set --sync-policy none`; old GHA `helm upgrade` still present until Phase 5 |
| After Phase 5 | Revert Phase-5 PR (restore `deploy` job) — kept as a single revertible commit; `helm upgrade --install` is idempotent against ArgoCD-applied resources only if ArgoCD auto-sync is paused first (two controllers must never run) |
| DB migration failed | Postgres is Recreate + Job idempotent; fix forward; migrations never auto-downgrade |

## PR breakdown (ordered, each independently mergeable)

| # | Repo | PR | Depends on | Safe because |
|---|---|---|---|---|
| 1 | waddles | chart: sync-wave annotations, migrations hook -> Sync wave -5, `Prune=false` on stateful/Secret, verify PostSync Job (flag OFF default) | — | Annotations inert under helm; flag off |
| 2 | admin | `k8s/argocd/` wrapper chart + SETUP.md + CNP + AppProject | — | Nothing applied until operator runs install |
| 3 | admin | `k8s/argocd-image-updater/` wrapper + SETUP.md | 2 | Same |
| 4 | admin | `k8s/argocd/dal2alpha/products/waddles.yaml` (AppProject + root App + ns/quota) + products README | 2 | Applied manually by operator; per-product, one file |
| 5 | waddles | `deploy/argocd/dal2alpha/waddlebot-app.yaml` (manual sync, IU annotations present but IU disabled) + `docs/deploy/argocd-alpha.md` | 1,4 | Not applied by CI |
| 6 | waddles | CI: sentinel `alpha-release` push step (if option 1) | — | Additive |
| 7 | waddles | Enable automated sync + Image Updater (flip Application) | operator Phase 3 done | One-file change, revertible |
| 8 | waddles | Remove `deploy` job from `deploy-dal2alpha.yml` | Phase 4 green | Revertible commit |

Operator (non-PR) steps between: install ArgoCD (after #2/#3), create secrets, Phase 2/3.

## Open questions

| # | Question | Leaning |
|---|---|---|
| 1 | Image Updater v1.x (CRD `ImageUpdater`) vs v0.x annotations — user spec says annotations; chart 1.4.x is CRD-based. Which? | Verify v1.x annotation compat; else CRD |
| 2 | Sentinel image (atomic tag) vs 12 per-image trackers? Skew can break svc_action->svc_ingest ordering | Sentinel |
| 3 | Fresh install: Postgres must exist before migrations; PreSync can't. OK to move migrations to Sync wave -5 (loses "PreSync before rollout" semantics for upgrades; waves still precede app waves)? | Yes |
| 4 | Is the `waddles` repo public? (determines repo cred need for ArgoCD) | Verify |
| 5 | Scope-down ArgoCD controller from cluster-admin: acceptable to run namespaced-mode, or does chart render cluster-scoped objects (ClusterIssuer, CRDs, CNP clusterwide)? | Namespaced if possible |
| 6 | Where does the ArgoCD-managed `argocd-server` get exposure/SSO (CF Access vs port-forward only)? | Port-forward v1 |
| 7 | Is Tetragon enforcing on `argocd` ns today? Needs allowlist before install | Verify |
| 8 | argocd-write-back tag not in git: acceptable for alpha audit, or want `git` write-back to a separate branch now? | `argocd` per decision |
| 10 | Per-product namespace/quota values; apply bootstrap files via operator kubectl vs one admin Application per product | Operator kubectl first |
| 9 | Beta/prod: separate deploy-values repo vs in-repo `k8s/argocd/<env>/` for git write-back branch protection | Defer to beta spec |
