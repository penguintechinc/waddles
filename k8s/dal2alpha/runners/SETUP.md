# dal2-alpha self-hosted runner (ARC) - activation

Flavor: official `actions/actions-runner-controller` **gha-runner-scale-set** (non-PRC), chart **0.13.0** (verify with `helm show chart` and bump the pin consciously). Runner is an in-cluster pod; outbound-only to GitHub (NAT-safe); no kubeconfig - it uses ServiceAccount `github-runners/dal2-alpha-runner`.

## HARD PREREQUISITE
Fix the cluster node-IP issue first. Cluster networking is currently broken; nothing below will work until pods can schedule and egress.

## IMPORTANT - label semantics
Scale sets are targeted by **scale-set name only**: the workflow must use `runs-on: dal2-alpha`. `runs-on: [self-hosted, dal2-alpha]` will NOT match. Update the deploy job's `runs-on` (or switch to legacy RunnerDeployment, which is deprecated).

## Steps
1. **GitHub Environment**: repo Settings > Environments > New `dal2-alpha`; URL `https://waddlebot.dal2a.penguintech.cloud`; optionally add Required reviewers to mirror the user-gated flow.
2. **GitHub App** (preferred over PAT): create an App owned by the org with Repository permissions Actions: read, Administration: read & write, Metadata: read; install on `waddles` only. (PAT fallback: classic `repo` scope or fine-grained with Administration RW + Actions R on this repo.) Then create the secret from files (nothing in shell history/args):
   `kubectl create ns github-runners` (or apply 00-namespace.yaml first), then
   `kubectl -n github-runners create secret generic arc-github-app --from-literal=github_app_id="$(cat app_id)" --from-literal=github_app_installation_id="$(cat inst_id)" --from-file=github_app_private_key=./app.pem`
3. **Apply + install** (helm v3.17.3 per repo pin):
   ```
   kubectl apply -f k8s/dal2alpha/runners/00-namespace.yaml
   kubectl apply -f k8s/dal2alpha/runners/20-buildkit.yaml  # rootless buildkitd (alpha builds)
   kubectl apply -f k8s/dal2alpha/runners/10-rbac.yaml     # needs ns `waddlebot` to exist (kubectl create ns waddlebot if fresh)
   helm install arc oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
     --version 0.13.0 -n arc-systems -f k8s/dal2alpha/runners/arc-controller-values.yaml
   helm install dal2-alpha oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
     --version 0.13.0 -n github-runners -f k8s/dal2alpha/runners/runner-scale-set-values.yaml
   ```
   Controller, runner and buildkit images are already digest-pinned (2026-10-22); re-resolve digests when bumping versions.
4. **Confirm**: `kubectl -n github-runners get pods` (listener pod Running); repo Settings > Actions > Runners shows scale set `dal2-alpha` (runners spawn on demand, minRunners 0).

## Build capability (alpha only - rootless BuildKit)
- `20-buildkit.yaml`: `buildkitd` (moby/buildkit rootless, UID 1000) in ns `buildkit`; runner reaches it via `BUILDKIT_HOST=tcp://buildkitd.buildkit.svc:1234`. Workflows install `buildctl`/`docker buildx` with the `remote` driver (`docker buildx create --driver remote $BUILDKIT_HOST`); no docker daemon/socket.
- Security: not privileged, non-root uid 1000, caps drop ALL + add only SETUID/SETGID, `allowPrivilegeEscalation: true`, no SA token. Residual: rootless BuildKit requires seccomp+AppArmor `Unconfined`, which PSA baseline/restricted reject, so ns `buildkit` is PSA `privileged` (isolated; runners ns stays `restricted`). CiliumNetworkPolicy limits ingress to ns `github-runners` only and egress to DNS + registries/package indexes. gRPC is unauthenticated: anything in github-runners can build - keep that ns single-purpose. Add mTLS if it ever shares a cluster tier with untrusted workloads.
- Trusted-code-only: buildkitd runs with `--oci-worker-no-process-sandbox` (inner build-step sandbox off), so build RUN steps are not PID-isolated from buildkitd. Only run workflows that build trusted first-party code (non-fork, protected-branch) against this runner; never PRs from forks.
- **Acknowledged finding (MEDIUM, container privilege escalation) - ACCEPTED**: buildkitd keeps `capabilities.add: [SETUID, SETGID]` + `allowPrivilegeEscalation: true`.
  - Why required: rootlesskit's parent maps a subuid range (`[0 1000 1][1 100000 65536]`) via setuid `newuidmap`/`newgidmap`, which need CAP_SETUID/CAP_SETGID. Proven: the no-caps config (a9496b9c) crashes with `newuidmap ... operation not permitted`. `--oci-worker-no-process-sandbox` does not avoid it (it only affects the inner build sandbox).
  - Why acceptable: least-privilege-viable rootless (strictly safer than privileged/root dockerd); uid 1000; only those two caps (no SYS_ADMIN/NET_ADMIN/privileged); dedicated `buildkit` ns; gRPC NetworkPolicy-gated to `github-runners`; runner limited to trusted non-fork/protected-branch workflows (9af23709).
- Hardening follow-ups for beta/prod (documented, NOT implemented): (1) replace seccomp `Unconfined` with a custom `Localhost` seccomp profile allowing only the needed userns/mount syscalls (profile distributed to nodes via a DaemonSet); (2) buildkitd mTLS (`--tlscacert`/`--tlscert`/`--tlskey` on buildkitd, client certs held only by deploy jobs) on top of the CiliumNetworkPolicy gate.
- Push to ghcr: workflow does `docker/login`-style auth with `GITHUB_TOKEN` (`packages: write`) or a registry PAT stored as a GitHub secret; creds are sent by the client to buildkit per build, never stored in the cluster.
- Sizing: buildkitd 2-6 CPU / 4-12Gi RAM / 60Gi ephemeral limit (emptyDir 50Gi, cache GC cap ~40GB); runner 1-2 CPU / 2-4Gi / 30Gi. Cache is emptyDir (lost on pod move; use registry cache `--cache-to type=registry` for durability). Two amd64 nodes: confirm free disk before enabling (kubelet DiskPressure risk).
- Alpha-specific: beta/prod use GitHub-hosted build runners (hybrid).

## Deploy job notes
Runner image has no helm/kubectl: the deploy job must install helm v3.17.3 (helm v4 fresh-install bug) and run in-cluster (uses SA token automatically). Egress NetworkPolicy for `github-runners` must allow github.com / ghcr.io / api.github.com + kube-apiserver.

## RBAC granted (least privilege)
- Role `waddlebot-helm-deployer` in ns `waddlebot` only: CRUD on the kinds the waddlebot chart renders (core, apps, batch, autoscaling, policy, ingress/HTTPRoute, CiliumNetworkPolicy/TracingPolicyNamespaced, Certificate, Role/RoleBinding). Includes Secrets (Helm release storage + chart secrets). No `escalate`/`bind`.
- ClusterRole `waddlebot-helm-deployer-cluster`: get/update/patch/delete on ClusterRole+ClusterRoleBinding `waddlebot-hub-api-tokenreview` ONLY (resourceNames); `create` on clusterroles/clusterrolebindings (RBAC cannot name-scope create; residual - escalation prevention limits it to perms the runner holds, i.e. tokenreviews:create); `tokenreviews:create`; get namespace `waddlebot`. No Kyverno (re-add only if supplyChain.verifyImages.enabled), no CRD, no `bind`/`escalate`. Cilium cluster policies / LB pool / L2 / GatewayClass / shared Gateway are not chart-managed: apply by hand from `k8s/dal2alpha/*.yaml`.
- Namespace creation is NOT granted: create `waddlebot` once by hand (set `namespaceCreate: false` in alpha overrides, or pre-create).
