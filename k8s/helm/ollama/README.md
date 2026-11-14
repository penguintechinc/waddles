# ollama (interim, arm64)

Interim in-cluster LLM endpoint for testing/use until WaddleAI lands: one Ollama pod pinned to the
arm64 node `k8s3-worker`, serving `gemma4:e4b` (approved JSON-capable model; `gemma4:e2b` stays the
default elsewhere; never Qwen/PRC models). Chart `k8s/helm/ollama` (apiVersion v2), release `ollama`,
namespace `waddleai` (pre-existing, PSA `restricted`; not created by the chart).

| Item | Value |
|---|---|
| Endpoint (in-cluster) | `http://ollama.waddleai.svc:11434` |
| Image | `ollama/ollama:0.40.13` pinned by multi-arch index digest (`image.digest`; arm64 manifest verified, Ollama >= 0.20 needed for Gemma 4) |
| Placement | `nodeSelector kubernetes.io/arch=arm64` + toleration for `kubernetes.io/arch=arm64:NoSchedule` |
| Resources | requests cpu 4 / mem 12Gi; limits cpu 10 / mem 20Gi (node allocatable cpu 12 / ~60Gi) |
| Storage | PVC `data-ollama-0`, `local-path`, 25Gi, mounted at `/data` (`HOME`, `OLLAMA_MODELS=/data/models`) |
| Tuning | `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_NO_CLOUD=1` |

## Exceptions (approved)

- **STATEFUL EXCEPTION (approved)**: StatefulSet + PVC for the model-weights cache (rebuildable, ~7 GB re-download).
- **Third-party HTTP API**: Ollama speaks HTTP/JSON on 11434; the in-cluster gRPC rule does not apply to this third-party service.
- Rootless: no root exception needed (uid/gid 10001, read-only rootfs, caps dropped, seccomp RuntimeDefault).
- Interim chart: `values.yaml` + `alpha.yml` only; no beta/gamma/production files exist because no such deployment does.

## Install / operate

Use **helm v3.17.3** for installs on alpha. Helm v4 breaks FRESH installs here (managedFields bug); the chart
is written to render identically under v3 and v4 (verified), and v4 becomes the default once that bug is
fixed. v4 `upgrade` of an existing release works.

```bash
H=~/.local/bin/helm3        # v3.17.3
$H upgrade --install ollama k8s/helm/ollama -n waddleai --kube-context dal2alpha -f k8s/helm/ollama/alpha.yml
$H test ollama -n waddleai --kube-context dal2alpha      # GET /api/tags must list the model
kubectl --context dal2alpha -n waddleai logs -f job/$(kubectl --context dal2alpha -n waddleai get job -l app.kubernetes.io/component=model-pull -o name | head -1 | cut -d/ -f2)
```

Do not pass `--wait-for-jobs` (the pull Job is intentionally not waited on, see below).

Validation ladder (all must pass): `helm lint`, `helm template` (v3 and v4 renders are byte-identical),
`helm template ... | kubectl apply --dry-run=server`, `kubeconform -strict -ignore-missing-schemas`.

### Values

| Key | Purpose |
|---|---|
| `image.digest` / `image.tag` | pinned image (digest is what is pulled, required) |
| `model` | model pulled by the Job and checked by `helm test` |
| `resources`, `nodeSelector`, `tolerations`, `persistence.*` | placement and sizing |
| `network.consumerNamespaces`, `network.consumerLabel` | who may call the API (below) |
| `network.registryFQDNs` | hosts the server may resolve/reach (registry + R2 blob CDN) |
| `network.defaultDeny` | namespace-wide default-deny owned by this release (set false if another release owns it) |
| `tetragon.allowedExec`, `tetragon.testAllowedExec` | exec allowlists (server/pull-job; helm-test pod) |

### Pull Job

A plain Job (not a Helm hook) so it never blocks `helm upgrade`. Its name carries a hash of its pod
template (`ollama-pull-<hash>`): changing the model, image or resources creates a new Job and Helm prunes the
old one, so the immutable pod template is never patched. `ollama pull` is idempotent (existing layers are
skipped), so a re-run is cheap. Progress is in the Job logs. If the node is down the Job simply stays Pending.

## Consuming the API (opt-in label)

Ollama's HTTP API is unauthenticated and includes admin endpoints, so access is an explicit opt-in plus an
L7 allowlist. A consumer pod must be in namespace `waddlebot` and carry:

```yaml
metadata:
  labels:
    waddles.penguintech.io/ollama-client: "true"
```

Unlabelled pods are dropped. To admit another namespace add it to `network.consumerNamespaces`. The label is an
opt-in, not authentication: anyone who can create/label pods in that namespace can set it, so protect that with
RBAC/admission. Gemma 4 is a thinking model: for `format=json` send `"think": false` (or a large `num_predict`),
otherwise the token budget is spent on `thinking` and `response` is empty/truncated.

## Network security (CiliumNetworkPolicy, `templates/networkpolicy.yaml`)

| Rule | Allows | Blocks |
|---|---|---|
| `<release>-default-deny` | nothing | all other ingress/egress in the namespace |
| Server ingress, consumers (L7) | labelled pods: `GET /`, `/api/tags`, `/api/version`; `POST /api/generate\|chat\|embed\|embeddings` | everything else: `/api/delete`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs/*`, `/api/pull`, `/api/ps`, other paths/methods (HTTP 403); unlabelled pods (dropped) |
| Server ingress, pull Job (L7) | `HEAD /` (CLI heartbeat), `POST /api/pull` | all other requests from the Job |
| Server ingress, helm-test pod (L7) | `GET /`, `GET /api/tags` | everything else |
| Server egress DNS | `network.registryFQDNs` + cluster-local names (`*.cluster.local` forms, for search-domain expansion) | every other lookup (REFUSED, `lookup hf.co: server misbehaving`): no DNS exfil channel |
| Server egress L3/L4 | TCP 443 to `network.registryFQDNs` only | all other destinations |
| Pull Job / test pod egress | cluster-local DNS + the server :11434 | all external lookups and traffic |

- Drop the registry names (and the FQDN block) for a steady state with no pulls.
- If a pull times out to a new `*.r2.cloudflarestorage.com` name, read the `Location` redirect of `https://registry.ollama.ai/v2/library/gemma4/blobs/<digest>` and update `network.registryFQDNs`.
- OpenAI-compatible `/v1/*` paths are NOT allowed; add `POST ^/v1/(chat/completions|embeddings)$` to the consumer rule if needed.

## Tetragon (`templates/tracingpolicy.yaml`)

Enforced (Sigkill) exec allowlist, one policy per component (server, model-pull, helm-test). Two kprobes with the
same selectors: `sys_execve` (path = arg 0) and `sys_execveat` (path = arg 1; `fexecve` passes an empty path,
which is never allowlisted). The waddlebot chart hooks `sys_execve` only; without `sys_execveat`, `fexecve`/
`execveat` bypass the allowlist (shown live: with an execve-only policy `execve` was killed but `fexecve` and
`execveat` ran; with both hooks all three are killed). Server/Job allow `/bin/ollama`, `/usr/bin/ollama`,
`/usr/lib/ollama/llama-server` (0.40.x GPU discovery + model runner); re-check when the image digest changes.
A second selector per hook lets only runc exec `/proc/self/fd/*` so `kubectl exec` works and memfd exec by app code
is killed. Tetragon sees every PATH-walk exec attempt (not only the one that succeeds), so scripts must use
absolute binary paths (the helm-test script does). `kubectl exec ... -- cat` etc. are killed by design.

## Verification (2026-11-13/14, dal2alpha)

Round 1 (arm64 `ollama-0` on `k8s3-worker`, kustomize-era, limits cpu 10 / mem 20Gi): scheduled on `k8s3-worker`;
valid JSON every call; cold load 9 s; warm 1.7-4.9 tok/s (arm64 CPU, noisy); cgroup `memory.peak` ~10.9 GiB
(includes mmapped page cache); model persisted across 2 restarts with no re-pull.

Round 2 (`k8s3-worker` was cordoned and then NotReady mid-test): the chart was proven on an amd64 stand-in
release `ollama-tp` (same chart, `smollm2:135m`, amd64 nodeSelector), since uninstalled; the real `ollama`
release was migrated from the kustomize objects (below) and is waiting for the node.

| Check | Result |
|---|---|
| lint / template, helm v4.0.0 and v3.17.3 | pass; renders identical; server dry-run + kubeconform pass |
| `helm test` (stand-in) | `GET /api/tags` lists the model: Succeeded |
| Labelled `waddlebot` pod | `GET /`, `/api/tags`, `/api/version`, `POST /api/chat` = 200; 6/6 valid JSON (`think:false`) |
| Blocked calls | `DELETE`/`POST /api/delete`, `/api/pull`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs/*`, `/api/ps` = 403 |
| Unlabelled `waddlebot` pod | timeout (curl 28, dropped) |
| DNS | `ollama pull hf.co/...` -> `server misbehaving`; registry + R2 pull and the Job pull succeed |
| Tetragon | `ollama --version` runs; `ls` killed (137); generation works via `llama-server` |
| Restart | model still present after `rollout restart`, no re-pull |
| Upgrade | repeated `helm upgrade` (v3) and one v4 `upgrade` on an existing release: no Job churn, no errors |

Real-pod re-verification of gemma4:e4b (scheduling on `k8s3-worker`, 6/6 JSON, memory, restart) is pending the
node returning to Ready; run `helm test ollama` plus the generation checks then.

## Migration note (kustomize -> Helm, done on dal2alpha)

The earlier `k8s/interim/ollama-arm` kustomize objects were migrated without touching the PVC (uid
`8d25292a-...` unchanged, still Bound): Service adopted via `meta.helm.sh/*` annotations + `managed-by=Helm`;
the StatefulSet was deleted with `--cascade=orphan` (its selector gained `instance`, which is immutable) and
recreated by Helm, re-attaching `data-ollama-0`; CiliumNetworkPolicy/TracingPolicy CRs were deleted and
recreated by Helm (adoption did not patch CRs); the old namespace `default-deny` was replaced by
`ollama-default-deny`; the old completed Job was removed and replaced by the hash-named pull Job.
