# gVisor Availability Findings — MicroK8s Community Addon + DOKS

**Date**: 2026-09-21
**Status**: Investigation complete — informs §18 R3, §12.2.1, §12.2.2 of `2026-09-14-rust-data-plane-design.md`
**Scope**: Read-only research. Spec not edited — findings only.

## Verdict

Alpha today: `sandbox.gvisor.enabled: false`. Build the §12.2.2 installer DaemonSet as a scoped follow-up (needed for R8's benchmark and to exercise the fail-closed path), not as the default running posture yet.

## Q1 — MicroK8s `community` addon: does `gvisor` still exist for v1.35.x?

**Confirmed: no. Not stale — never present.**

| Check | Result |
|---|---|
| `canonical/microk8s-community-addons` repo, `addons/` dir listing | No `gvisor` entry. 32 addons present (`kata`, `cilium`, `istio`, etc.) — `kata` is the community sandboxed-runtime addon, not `gvisor` |
| `addons.yaml` (authoritative addon manifest) | No `gvisor` or `runsc` string anywhere in the file |
| Repo commit history, `git log --all -- addons/gvisor` (via `gh api .../commits?path=addons/gvisor`) | **0 commits, ever** — this path has never existed in the repo |
| `canonical.com/microk8s/docs/addons` (official docs) | Core addons and community addons both enumerated; no gvisor/runsc/sandboxed-runtime entry besides `kata` |
| GitHub code search, `"enable gvisor" microk8s` | 4 hits, all personal blogs/homelabs, none canonical |
| `canonical/microk8s` core repo code search for `gvisor` | 0 hits |

- §12.2.1's MicroK8s row (`microk8s enable gvisor`) appears to conflate MicroK8s with **minikube**, which does ship a real, current `minikube addons enable gvisor` (confirmed via `minikube.sigs.k8s.io/docs/handbook/addons/gvisor/`) — that row is correct for minikube, not MicroK8s.
- MicroK8s does support hand-rolled runtime registration: `gh issue view 4171 --repo canonical/microk8s` confirms containerd config is templated at `microk8s-resources/default-args/containerd-template.toml` and regenerated on restart — editing the **template**, not the generated file, persists (same lesson the spec already captured for k3s). This is the mechanism the §12.2.2 installer would need to use on MicroK8s, and it is technically workable because the team fully controls this node (unlike DOKS — see Q2).
- Not verified: whether a `gvisor` addon existed in some *other*, now-defunct MicroK8s addon source outside `canonical/microk8s-community-addons` (e.g., a pre-2022 addon layout). Low likelihood given zero hits anywhere in canonical's current org, but not exhaustively ruled out for pre-2022 history.

## Q2 — Getting `runsc` onto a DOKS worker node

**Confirmed: DOKS does not permit node-level containerd changes. Neither the native path nor the §12.2.2 installer works there.**

| Check | Result |
|---|---|
| `docs.digitalocean.com/products/kubernetes/details/managed/` (official) | "Once you've added them, we manage their configuration, including the: Operating system, Installed packages, File system, Local storage, **Container daemon configuration**, Machine size." And: "While it *is* technically possible to access and alter the worker nodes at this time, your changes are overwritten by the reconciler and do not persist." Also flags this may become fully blocked in future. |
| Custom/BYO node images or self-managed node pools for DOKS | Not found. `doctl`/Terraform/Pulumi node-pool docs expose size, count, autoscale, labels/taints — no custom image or containerd-config field. |
| `ideas.digitalocean.com` feature request: "Support for Custom Sandbox Runtimes (e.g., gVisor) on DigitalOcean Kubernetes" | Open request, filed 2025-10-24, **zero official DigitalOcean staff response or roadmap status** as of this check. Requester's own proof-of-concept (privileged DaemonSet patching containerd) is called out by the requester as unsustainable — breaks on node replacement/upgrade. |

- This directly falsifies §12.2.1's DOKS row ("No native offering; use `sandbox.installer.enabled: true`, or a pre-baked node image") for the installer half: DOKS's reconciler actively reverts node-level file changes, so the installer DaemonSet's containerd patch would not survive a node replacement/upgrade cycle — the same failure mode the DO feature-request author already hit.
- "Pre-baked node image" is also not viable: DOKS worker nodes are DO-managed images only; no custom-image path was found in current docs or IaC providers.
- **Decision-relevant**: this forces `sandbox.gvisor.enabled: false` for gamma/prod on DOKS, or a different sandbox mechanism entirely (out of scope here) — not a delay, a hard platform constraint, unless DigitalOcean ships native sandbox-runtime support (no evidence of a timeline).
- Not verified: DOKS behavior on a **self-managed** cluster running on plain DO Droplets (i.e., not DOKS proper, kubeadm on Droplets) — that scenario falls under the spec's existing "kubeadm / upstream CNCF" row, which already covers manual `runsc` installation, and was out of scope for "DOKS" specifically.

## Q3 — Which option should alpha take today

| Option | Status | Verdict |
|---|---|---|
| `microk8s enable community && microk8s enable gvisor` | **Does not exist** (Q1) | Ruled out, not just "stale" |
| §12.2.2 installer DaemonSet | Not yet built (M2 deliverable); technically viable on MicroK8s (template-file containerd editing persists — Q1) | Right long-term alpha path, wrong thing to block on today |
| `sandbox.gvisor.enabled: false` | Available now, zero engineering cost | **Recommended for alpha today** |

**Reasoning:**
- The installer DaemonSet doesn't exist yet — building it (pinned + checksum-verified fetch, idempotent containerd template patch, containerd restart, node labeling) is real, non-trivial work that shouldn't gate M1/M1.5/M2/M3–M5, none of which depend on gVisor actually being present.
- `sandbox.gvisor.enabled: false` is the spec's own designed-for-this fallback: fails visibly (WARN log, gauge, `/health`, `hello` frame), every other sandbox layer (rootless uid 10001, dropped caps, read-only rootfs, seccomp, default-deny CiliumNetworkPolicy, WASM sandbox itself) stays on.

**Cost of this recommendation:**
- Alpha loses the gVisor defense-in-depth layer for day-to-day dev — a wasmtime native-code escape would hit the host kernel directly. Acceptable for alpha (no customer data), unacceptable to leave as the permanent posture without revisiting.
- The gVisor-specific code paths (`RuntimeClass` wiring, `/proc/version`/`/proc/self/status` verification, fail-closed exit 78 on mismatch) go unexercised by default until the installer is built and flipped on at least once.
- **R8's benchmark deliverable is blocked by this choice**: measuring `waddles_executor_call_seconds`/`waddles_e2e_latency_seconds` with `sandbox.gvisor.enabled: true` needs a real gVisor-enabled run somewhere. Given DOKS is confirmed unable to host it (Q2), **alpha via the installer DaemonSet is the only currently-identified environment that can produce this number** — so the installer isn't optional forever, it's a required one-time (or CI-gated) exercise before M2 closes, just not the thing alpha blocks its default posture on today.
- Because DOKS can't do gVisor either, gamma/prod will very likely also land on `sandbox.gvisor.enabled: false` as their *running* default — worth surfacing as a spec-level question (does the spec's "gVisor by default" framing still hold once neither alpha's cheapest path nor gamma/prod's only path can deliver it?), not just an alpha-day tactical call.

## Sources

- [canonical/microk8s-community-addons](https://github.com/canonical/microk8s-community-addons) — `addons.yaml`, `addons/` tree, commit history (queried via `gh api`)
- [canonical.com/microk8s/docs/addons](https://canonical.com/microk8s/docs/addons) — official core/community addon list
- [canonical/microk8s#4171](https://github.com/canonical/microk8s/issues/4171) — containerd template persistence mechanism
- [minikube.sigs.k8s.io/docs/handbook/addons/gvisor](https://minikube.sigs.k8s.io/docs/handbook/addons/gvisor/) — confirms the real (non-MicroK8s) `addons enable gvisor` command
- [docs.digitalocean.com/products/kubernetes/details/managed](https://docs.digitalocean.com/products/kubernetes/details/managed/) — official DOKS node-management scope, reconciler-reverts-changes statement
- [ideas.digitalocean.com — Support for Custom Sandbox Runtimes (gVisor) on DOKS](https://ideas.digitalocean.com/kubernetes/p/support-for-custom-sandbox-runtimes-eg-gvisor-on-digitalocean-kubernetes) — open feature request, no official response
