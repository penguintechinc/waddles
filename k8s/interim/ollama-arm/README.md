# Interim Ollama (arm64) on dal2alpha

Interim, in-cluster LLM endpoint for testing/use until WaddleAI lands. One Ollama pod pinned to the
arm64 node `k8s3-worker`, serving `gemma4:e4b` (approved JSON-capable model; `gemma4:e2b` stays the
default elsewhere; never Qwen/PRC models).

| Item | Value |
|---|---|
| Cluster / namespace | `dal2alpha` / `waddleai` (pre-existing, PSA `restricted` enforce; not managed here) |
| Endpoint (in-cluster) | `http://ollama.waddleai.svc:11434` |
| Image | `ollama/ollama:0.40.13` pinned by index digest (linux/arm64 manifest present; Ollama >= 0.20 needed for Gemma 4) |
| Placement | `nodeSelector kubernetes.io/arch=arm64` + toleration for `kubernetes.io/arch=arm64:NoSchedule` |
| Resources | requests cpu 4 / mem 12Gi; limits cpu 10 / mem 20Gi (node allocatable cpu 12 / ~60Gi) |
| Storage | PVC `data-ollama-0`, `local-path`, 25Gi, mounted at `/data` (`HOME`, `OLLAMA_MODELS=/data/models`) |
| Tuning | `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_NO_CLOUD=1` |

## Exceptions (approved)

- **STATEFUL EXCEPTION (approved)**: StatefulSet + PVC for the model-weights cache (rebuildable, but ~7 GB re-download).
- **Third-party HTTP API**: Ollama speaks HTTP/JSON on 11434; the in-cluster gRPC rule does not apply to this third-party service (approved exception).
- **Plain manifests, not Helm**: owner-requested interim (kustomize). Replace with a chart if it outlives the interim.
- Rootless: no root exception needed. The image runs fine as uid/gid 10001 with a read-only rootfs.

## Files

| File | Purpose |
|---|---|
| `kustomization.yaml` | Entry point (`kubectl apply -k .`) |
| `statefulset.yaml` | Ollama server, replicas=1, hardened securityContext |
| `service.yaml` | ClusterIP `ollama:11434` |
| `job-pull-model.yaml` | One-shot idempotent `ollama pull gemma4:e4b` (progress in job logs) |
| `networkpolicy.yaml` | Namespace default-deny + server and pull-job allows (CiliumNetworkPolicy only) |
| `tracingpolicy.yaml` | Tetragon exec allowlist, enforced (Sigkill) |

## Deploy / operate

```bash
kubectl --context dal2alpha apply -k k8s/interim/ollama-arm
kubectl --context dal2alpha -n waddleai rollout status sts/ollama
kubectl --context dal2alpha -n waddleai logs -f job/ollama-pull-gemma4-e4b     # pull progress
# re-run / refresh the model (Job pod specs are immutable):
kubectl --context dal2alpha -n waddleai delete job ollama-pull-gemma4-e4b && kubectl --context dal2alpha apply -k k8s/interim/ollama-arm
```

Consumers call `POST /api/generate` or `/api/chat` with `"model":"gemma4:e4b"`. Gemma 4 is a
thinking model: for `format=json` set `"think": false` (or allow enough `num_predict`), otherwise the
token budget is spent on `thinking` and `response` comes back empty/truncated.

## Consuming the API (opt-in label)

Ollama's HTTP API is unauthenticated and includes admin endpoints, so network access is an explicit
opt-in plus an L7 allowlist. A consumer pod must be in namespace `waddlebot` and carry:

```yaml
metadata:
  labels:
    waddles.penguintech.io/ollama-client: "true"
```

Unlabelled pods are dropped. Another namespace needs a change to `networkpolicy.yaml` (add it to the
first `fromEndpoints`). The label is an opt-in, not authentication: anyone who can create/label pods in
`waddlebot` can set it, so protect that with RBAC/admission.

## Network security (CiliumNetworkPolicy, `networkpolicy.yaml`)

| Rule | What it allows | What it blocks |
|---|---|---|
| `default-deny` | nothing | all other ingress/egress in `waddleai` |
| Server ingress, consumers (L7) | from labelled `waddlebot` pods on 11434: `GET /`, `GET /api/tags`, `GET /api/version`, `POST /api/generate\|chat\|embed\|embeddings` | everything else: `/api/delete`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs/*`, `/api/pull`, `/api/ps`, any other path/method, trailing-slash and `//` tricks (HTTP 403); unlabelled pods (dropped) |
| Server ingress, pull Job (L7) | from the pull Job pod only: `HEAD /` (CLI heartbeat), `POST /api/pull` | all other requests from the Job |
| Server egress DNS | `registry.ollama.ai`, the blob host below, and cluster-local names (`*.cluster.local` forms, needed for search-domain expansion) | every other lookup (resolver gets REFUSED: `lookup hf.co: server misbehaving`), so no DNS exfil channel |
| Server egress L3/L4 | TCP 443 to `registry.ollama.ai` and `dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com` (discovered from a real pull) | all other destinations |
| Pull Job egress | cluster-local DNS names + `ollama` server :11434 | all external lookups and traffic (the server downloads) |

- The server needs the registry names only while pulling. For a steady state with no pulls, drop the two `matchName` DNS entries and the `toFQDNs` block.
- If a pull times out to a new `*.r2.cloudflarestorage.com` name, read the redirect `Location` of `https://registry.ollama.ai/v2/library/gemma4/blobs/<digest>` and update the policy.
- OpenAI-compatible `/v1/*` paths are NOT allowed; add `POST ^/v1/(chat/completions|embeddings)$` to the consumer rule if a consumer needs them.

## Tetragon (`tracingpolicy.yaml`)

Enforced (Sigkill) exec allowlist on every `app.kubernetes.io/name=ollama` pod. Two kprobes, same
selectors: `sys_execve` (path = arg 0) and `sys_execveat` (path = arg 1; `fexecve` passes an empty
path, which is never allowlisted). The repo's waddlebot chart hooks `sys_execve` only; without the
`sys_execveat` hook `fexecve`/`execveat` bypass the allowlist (shown live, see below).

Allowed: `/bin/ollama`, `/usr/bin/ollama`, `/usr/lib/ollama/llama-server` (0.40.x GPU discovery + model
runner). A second selector per hook lets only runc exec `/proc/self/fd/*` so `kubectl exec` keeps working
and fileless memfd exec by app code is killed. Re-check the binary paths whenever the image digest is
bumped. `kubectl exec ... -- cat` etc. are killed by design; to inspect, temporarily add the binary to the
policy and revert.

## Verification (2026-11-13, dal2alpha)

Round 1 (arm64 `ollama-0` on `k8s3-worker`, limits cpu 10 / mem 20Gi):

| Check | Result |
|---|---|
| Scheduling | `ollama-0` on `k8s3-worker`; PVC `data-ollama-0` 25Gi local-path |
| JSON generate (`format=json`, `think:false`) | valid JSON every call; cold load 9 s; warm 1.7-4.9 tok/s (arm64 CPU, noisy) |
| Memory | cgroup `memory.peak` ~10.9 GiB (includes mmapped model page cache); limit 20 GiB |
| Restart persistence | 2 pod restarts: model stays in `ollama list`, no re-pull |

Round 2 (L7 / DNS / execveat hardening). `k8s3-worker` was cordoned and then went unreachable by an
operator mid-test, so these were proven on an amd64 stand-in pod (`ollama-tp`: same image digest, labels,
securityContext, and the SAME applied policies) with `smollm2:135m`, then deleted:

| Check | Result |
|---|---|
| Labelled waddlebot pod, allowed calls | `GET /`, `/api/tags`, `/api/version`, `POST /api/generate`, `/api/chat` = 200; `/api/embed`, `/api/embeddings` reach the server (model lacks embeddings: 501/500, not 403) |
| Labelled pod, blocked calls | `DELETE`/`POST /api/delete`, `/api/pull`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs/*` (POST/HEAD), `/api/ps`, `/api/tags/`, `//api/delete`, `/api/generate/../delete` = 403 |
| Unlabelled waddlebot pod | `GET /api/tags` timeout (curl 28, dropped) |
| Pull Job path | Job (cluster-only DNS) pulls through `POST /api/pull`: success; first attempt was 403 on `HEAD /`, fixed by allowing it for the Job only |
| DNS | `ollama pull hf.co/...` -> `lookup hf.co ... server misbehaving` (REFUSED); `ollama pull smollm2:135m` (registry + R2 blob host, search-domain expansion) succeeds |
| Tetragon | old (execve-only) policy: `execve` killed but `fexecve` and `execveat` RAN; new policy: all three killed (exit 137); `kubectl exec ls` killed; `ollama --version` runs; generation still works through `llama-server` |
| JSON generate on stand-in | 6/6 valid |

Not yet re-verified on the real arm64 pod after Round 2: gemma4:e4b 6/6 JSON generation (node was
unavailable). Re-run once `k8s3-worker` is schedulable.
