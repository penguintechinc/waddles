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

## Network security

- `default-deny`: every endpoint in `waddleai` denies all ingress and egress unless allowed (Cilium convention on this cluster).
- Ingress to `ollama:11434` ONLY from namespaces `waddlebot` and `waddleai`.
- Server egress: DNS (kube-dns) + `toFQDNs` for the pull hosts only, TCP 443:
  - `registry.ollama.ai` (manifests; blobs answer 307)
  - `dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com` (blob CDN, discovered from a real pull)
- DNS rule is `matchPattern: "*"` because search-domain lookups (`*.svc.cluster.local`) must resolve; the egress itself is FQDN-scoped.
- If the CDN host changes (a pull fails with timeouts to a new `*.r2.cloudflarestorage.com` name), find the host from the redirect `Location` of `https://registry.ollama.ai/v2/library/gemma4/blobs/<digest>` and update `networkpolicy.yaml`.
- Pull job: egress only to DNS and the `ollama` server; it does not reach the internet itself.

## Tetragon

Allowlist (`sys_execve`, Sigkill): `/bin/ollama`, `/usr/bin/ollama`, `/usr/lib/ollama/llama-server`
(0.40.x GPU discovery + model runner), plus a second selector so only runc may `/proc/self/fd/*`
re-exec (keeps `kubectl exec` working). Proven: `ollama --version` runs; `ls` is killed (exit 137).
Re-check the binary paths whenever the image digest is bumped. `kubectl exec ... -- cat` etc. are
killed by design; to inspect, temporarily add the binary to the policy and revert.

## Verification (2026-11-13, dal2alpha, limits cpu 10 / mem 20Gi)

| Check | Result |
|---|---|
| Scheduling | `ollama-0` on `k8s3-worker` |
| Cross-namespace allow | probe in `waddlebot`: `GET /api/tags` lists `gemma4:e4b` |
| Cross-namespace deny | probe in `default`: curl exit 28 (timeout) |
| Egress deny | server `ollama pull hf.co/...`: `dial tcp ...:443: i/o timeout` |
| Tetragon | `ollama --version` runs; `ls` killed (exit 137) |
| JSON generate (`format=json`, `think:false`) | valid JSON every call; cold load 9 s (page cache cold: first call 0.96 tok/s); warm 1.7-4.9 tok/s generation, 3-24 tok/s prompt eval (noisy) |
| Memory | cgroup `memory.peak` 11,650,789,376 B (~10.9 GiB, includes mmapped model page cache); limit 20 GiB |
| Restart persistence | 2 pod restarts: model still in `ollama list`, no new pull, generation works |

Throughput is CPU-bound and modest (arm64, no GPU): fine for small JSON jobs, slow for long outputs.
If it is too slow for a consumer, `gemma4:e2b` is the smaller alternative (not deployed here).
