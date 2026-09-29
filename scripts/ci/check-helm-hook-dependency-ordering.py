#!/usr/bin/env python3
"""Static check for the helm-hook-rbac-ordering bug class.

Helm finishes the ENTIRE pre-install/pre-upgrade hook phase, in ascending
`helm.sh/hook-weight` order, before creating a single regular (non-hook)
resource. A pre-install/pre-upgrade Job or Pod that references a regular
ServiceAccount, ConfigMap, or Secret (serviceAccountName, envFrom, or a
volume/env secretKeyRef/configMapKeyRef) is therefore not guaranteed that
dependency exists yet -- it either doesn't exist (fresh install) or is a race
against whichever hook creates it. This is exactly the class of bug behind
the "serviceaccounts ... not found" / DeadlineExceeded failure fixed by
fix/helm-hook-rbac-ordering.

Reads a `helm template` multi-document YAML stream on stdin and asserts, for
every pre-install/pre-upgrade hook Job/Pod:
  - its serviceAccountName resolves to either (a) a hook resource of the
    matching kind at a strictly lower hook-weight, or (b) an entry in the
    documented ALLOWLIST below.
  - every ConfigMap/Secret name it mounts (envFrom, volumes, or
    secretKeyRef/configMapKeyRef) resolves the same way.

post-install/post-upgrade hooks are recorded but never checked: by the time
they run, Helm has already created every regular resource, so no ordering
gap can exist for them.

Exit code is the gate: zero findings examined is treated as a hard failure,
never a silent pass (see critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

# Dependency names a pre-install/pre-upgrade hook may reference even though
# they are plain/regular (non-hook) chart resources -- each entry must be
# justified here, never added silently.
ALLOWLIST: dict[str, str] = {
    "default": (
        "Kubernetes auto-creates the 'default' ServiceAccount in every "
        "namespace as soon as the namespace exists; it is never a chart "
        "resource."
    ),
}

HOOK_ANNOTATION = "helm.sh/hook"
WEIGHT_ANNOTATION = "helm.sh/hook-weight"
PRE_HOOKS = {"pre-install", "pre-upgrade"}
POST_HOOKS = {"post-install", "post-upgrade"}


def hook_types(doc: dict[str, Any]) -> set[str]:
    ann = (doc.get("metadata") or {}).get("annotations") or {}
    raw = ann.get(HOOK_ANNOTATION, "")
    return {h.strip() for h in raw.split(",") if h.strip()}


def hook_weight(doc: dict[str, Any]) -> int:
    ann = (doc.get("metadata") or {}).get("annotations") or {}
    try:
        return int(ann.get(WEIGHT_ANNOTATION, "0"))
    except ValueError:
        return 0


def pod_spec_of(doc: dict[str, Any]) -> dict[str, Any] | None:
    kind = doc.get("kind")
    if kind == "Pod":
        return doc.get("spec")
    if kind == "Job":
        return ((doc.get("spec") or {}).get("template") or {}).get("spec")
    return None


def collect_configmap_secret_refs(pod_spec: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    containers = (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    for c in containers:
        for ef in c.get("envFrom") or []:
            if "secretRef" in ef:
                refs.add(ef["secretRef"]["name"])
            if "configMapRef" in ef:
                refs.add(ef["configMapRef"]["name"])
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            if "secretKeyRef" in vf:
                refs.add(vf["secretKeyRef"]["name"])
            if "configMapKeyRef" in vf:
                refs.add(vf["configMapKeyRef"]["name"])
    for v in pod_spec.get("volumes") or []:
        if "secret" in v and v["secret"].get("secretName"):
            refs.add(v["secret"]["secretName"])
        if "configMap" in v and v["configMap"].get("name"):
            refs.add(v["configMap"]["name"])
    return refs


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if d]

    # index every resource's own hook status by (kind, name)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for d in docs:
        kind = d.get("kind")
        name = (d.get("metadata") or {}).get("name")
        if kind and name:
            index[(kind, name)] = d

    def resolves(name: str, kinds: tuple[str, ...], max_weight: int) -> tuple[bool, str]:
        if name in ALLOWLIST:
            return True, f"allowlisted ({ALLOWLIST[name]})"
        for kind in kinds:
            dep = index.get((kind, name))
            if dep is None:
                continue
            dep_hooks = hook_types(dep)
            if not (dep_hooks & PRE_HOOKS):
                return False, f"{kind}/{name} exists but is a regular (non-hook) resource"
            dep_weight = hook_weight(dep)
            if dep_weight < max_weight:
                return True, f"{kind}/{name} is a pre-* hook at weight {dep_weight} < {max_weight}"
            return False, f"{kind}/{name} is a pre-* hook at weight {dep_weight}, not < {max_weight}"
        return False, f"no {'/'.join(kinds)} named {name} found in rendered output"

    examined = 0
    failures: list[str] = []

    for d in docs:
        if d.get("kind") not in ("Job", "Pod"):
            continue
        hooks = hook_types(d)
        if not hooks:
            continue
        examined += 1
        name = (d.get("metadata") or {}).get("name")
        weight = hook_weight(d)

        if not (hooks & PRE_HOOKS):
            print(f"SKIP  {d['kind']}/{name}: post-install/post-upgrade only, "
                  f"regular resources already exist by the time it fires")
            continue

        pod_spec = pod_spec_of(d) or {}
        sa = pod_spec.get("serviceAccountName")
        if sa:
            ok, reason = resolves(sa, ("ServiceAccount",), weight)
            status = "OK" if ok else "FAIL"
            print(f"{status}  {d['kind']}/{name} (weight {weight}) serviceAccountName={sa}: {reason}")
            if not ok:
                failures.append(f"{d['kind']}/{name} serviceAccountName={sa}: {reason}")

        for ref in sorted(collect_configmap_secret_refs(pod_spec)):
            ok, reason = resolves(ref, ("ConfigMap", "Secret"), weight)
            status = "OK" if ok else "FAIL"
            print(f"{status}  {d['kind']}/{name} (weight {weight}) mounts {ref}: {reason}")
            if not ok:
                failures.append(f"{d['kind']}/{name} mounts {ref}: {reason}")

    print(f"\nhooks examined: {examined}")
    if examined == 0:
        print("FAIL: zero hooks examined -- check is pointed at the wrong input", file=sys.stderr)
        return 1

    if failures:
        print(f"\nFAIL: {len(failures)} hook dependency ordering violation(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("PASS: every pre-install/pre-upgrade hook's dependencies resolve safely")
    return 0


if __name__ == "__main__":
    sys.exit(main())
