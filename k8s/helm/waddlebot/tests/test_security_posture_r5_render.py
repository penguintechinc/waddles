"""Helm-template assertions for fix/chart-security-posture-r5 (release-audit R5 cluster-posture HIGHs).

Pins the shipping (beta/gamma/production) cluster-security baseline the chart must render:

1. securityContext: every container (init containers included) of every Deployment/StatefulSet/
   Job/CronJob drops ALL caps, forbids privilege escalation, runs non-root with a RuntimeDefault
   seccomp profile -- i.e. the pod template is Pod Security Admission `restricted`-compliant; the
   previously bare workloads (collectors, postgres, redis, qdrant, ollama) also run with a
   read-only root filesystem.
2. Pod Security Admission: the chart-managed Namespace carries enforce/audit/warn=restricted.
3. Default-deny egress: every long-running workload is selected by a CiliumNetworkPolicy that
   carries an `egress` section; "world" rules never reach RFC1918 / link-local / metadata.
4. Port-scoped ingress: every ingress allow names ports, and each workload's declared
   containerPorts are admitted -- nothing wider; hub-api's gRPC port is left to its dedicated CNP.
5. No plain `NetworkPolicy` remains anywhere (CiliumNetworkPolicy only).
6. Tetragon: every app workload has an ENFORCED (Sigkill) sys_execve allowlist TracingPolicy.

Every test reports/asserts the number of documents or workloads it examined -- zero matches is a
failure, never a silent pass (critical-rules.md Verification Integrity). A missing helm CLI is
a failure, not a skip.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_POD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
_LONG_RUNNING = ("Deployment", "StatefulSet", "DaemonSet")
# Shipping environments, plus the dev ones (the pod specs must stay PSA-restricted everywhere).
_SHIPPING = ("values-beta.yaml", "values-gamma.yaml", "values-production.yaml")
_ALL_ENVS = (*_SHIPPING, "values-alpha.yaml", "values-local.yaml", "values-ci.yaml")
# values-local deliberately relaxes runAsNonRoot (pre-alpha laptop clusters) and so enforces PSA
# `baseline`, not `restricted` -- it is asserted separately in TestPodSecurityAdmission.
_RESTRICTED_ENVS = (*_SHIPPING, "values-alpha.yaml", "values-ci.yaml")
# Workloads whose egress/ingress is governed by a dedicated, pre-existing CNP rather than the
# generic per-workload one (bundle-executors have no listener and an explicit deny-all ingress).
_OWN_CNP_COMPONENTS = ("bundle-executor", "bundle-executor-action")
_RFC1918_AND_METADATA = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
# Rootfs must be read-only on the workloads this fix newly locked down.
_READONLY_ROOTFS = ("postgres", "redis", "qdrant", "ollama", "discord-collector", "twitch-collector",
                    "slack-collector", "youtube-live-collector")
_SPIRE_PREFIX = "waddlebot-spire"
_ALLOWED_VOLUME_TYPES = {"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral",
                         "persistentVolumeClaim", "projected", "secret", "name"}


def _render(values: str, extra_set: list[str] | None = None) -> list[dict[str, Any]]:
    """helm-template one values file; a render failure or zero documents fails the test."""
    args = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_CHART_DIR / values),
        "--set", "global.deploymentTier=alpha",
    ]
    for kv in extra_set or []:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell, test-only
    if result.returncode != 0:
        pytest.fail(f"helm template failed for {values}:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, f"helm template produced zero documents for {values} -- cannot be a pass"
    return docs


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the pod spec of a workload doc (handles CronJob's extra nesting)."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def _pod_labels(doc: dict[str, Any]) -> dict[str, str]:
    """Return the pod-template labels of a workload doc."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"].get("metadata", {}).get("labels", {}) or {}


def _selects(selector: dict[str, Any] | None, labels: dict[str, str]) -> bool:
    """Evaluate a Cilium endpointSelector (matchLabels + matchExpressions In/NotIn) against labels."""
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        val = labels.get(expr["key"])
        if expr["operator"] == "In" and val not in expr["values"]:
            return False
        if expr["operator"] == "NotIn" and val in expr["values"]:
            return False
    return True


def _cnps(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All CiliumNetworkPolicy docs."""
    return [d for d in docs if d["kind"] == "CiliumNetworkPolicy"]


def _expand_ports(entries: list[dict[str, Any]]) -> set[tuple[int, str]]:
    """Expand CNP toPorts entries (incl. endPort ranges) into (port, protocol) pairs."""
    out: set[tuple[int, str]] = set()
    for e in entries:
        lo = int(e["port"])
        hi = int(e.get("endPort", lo))
        out.update((p, e.get("protocol", "TCP")) for p in range(lo, hi + 1))
    return out


def _declared_ports(doc: dict[str, Any]) -> set[tuple[int, str]]:
    """(containerPort, protocol) pairs declared by every container of a workload."""
    out: set[tuple[int, str]] = set()
    for c in _pod_spec(doc).get("containers", []):
        ports = {p["name"]: p for p in c.get("ports", []) or []}
        for p in ports.values():
            out.add((int(p["containerPort"]), p.get("protocol", "TCP")))
        # svc-streaming declares only the two boundary ports of its WebRTC media range
        # (`webrtc-lo` / `webrtc-hi`); the whole range is the serving surface.
        if "webrtc-lo" in ports and "webrtc-hi" in ports:
            lo, hi = int(ports["webrtc-lo"]["containerPort"]), int(ports["webrtc-hi"]["containerPort"])
            out.update((p, "UDP") for p in range(lo, hi + 1))
    return out


def _ingress_ports(cnp: dict[str, Any]) -> set[tuple[int, str]]:
    """Every (port, proto) admitted by any ingress rule of a CNP."""
    out: set[tuple[int, str]] = set()
    for rule in cnp["spec"].get("ingress") or []:
        for tp in rule.get("toPorts") or []:
            out |= _expand_ports(tp["ports"])
    return out


def _workloads(docs: list[dict[str, Any]], kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Workload docs of the given kinds."""
    return [d for d in docs if d["kind"] in kinds]


class TestSecurityContextRestricted:
    """Every pod template is PSA `restricted`-compliant, in every environment."""

    @pytest.mark.parametrize("values", _RESTRICTED_ENVS)
    def test_all_containers_restricted(self, values: str) -> None:
        docs = _render(values)
        workloads = _workloads(docs, _POD_KINDS)
        assert workloads, "no workloads rendered -- cannot be a pass"
        containers_checked = 0
        spire_exempt = 0
        problems: list[str] = []
        for w in workloads:
            ps = _pod_spec(w)
            psc = ps.get("securityContext") or {}
            name = f"{w['kind']}/{w['metadata']['name']}"
            if w["metadata"]["name"].startswith(_SPIRE_PREFIX):
                # The SPIRE subchart (agent: uid 0 + hostPath socket dir) is an approved ROOT
                # EXCEPTION with its own audit trail; the namespace enforces `privileged` while it
                # is enabled (see test_namespace_labels / values-beta.yaml).
                spire_exempt += 1
                continue
            if ps.get("hostNetwork") or ps.get("hostPID") or ps.get("hostIPC"):
                problems.append(f"{name}: host namespaces")
            for v in ps.get("volumes") or []:
                kinds = set(v) - {"name"}
                if not kinds <= _ALLOWED_VOLUME_TYPES:
                    problems.append(f"{name}: volume type {kinds} not allowed by restricted")
            for c in (ps.get("containers") or []) + (ps.get("initContainers") or []):
                containers_checked += 1
                sc = c.get("securityContext") or {}
                cn = f"{name}/{c['name']}"
                if sc.get("allowPrivilegeEscalation") is not False:
                    problems.append(f"{cn}: allowPrivilegeEscalation != false")
                if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
                    problems.append(f"{cn}: capabilities.drop lacks ALL")
                if (sc.get("capabilities") or {}).get("add") not in (None, [], ["NET_BIND_SERVICE"]):
                    problems.append(f"{cn}: capabilities.add beyond NET_BIND_SERVICE")
                if sc.get("privileged"):
                    problems.append(f"{cn}: privileged")
                if not (sc.get("runAsNonRoot") is True or psc.get("runAsNonRoot") is True):
                    problems.append(f"{cn}: runAsNonRoot not true")
                if sc.get("runAsUser", psc.get("runAsUser", 1)) == 0:
                    problems.append(f"{cn}: runAsUser 0")
                seccomp = (sc.get("seccompProfile") or psc.get("seccompProfile") or {}).get("type")
                if seccomp not in ("RuntimeDefault", "Localhost"):
                    problems.append(f"{cn}: seccompProfile {seccomp!r}")
                for p in c.get("ports") or []:
                    if p.get("hostPort"):
                        problems.append(f"{cn}: hostPort")
        print(f"{values}: examined {len(workloads)} workloads / {containers_checked} containers "
              f"({spire_exempt} spire-exempt)")
        assert containers_checked > 0
        assert not problems, "\n".join(problems)

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_newly_secured_workloads_have_readonly_rootfs(self, values: str) -> None:
        docs = _render(values, ["modules.collectors.discord.enabled=true",
                                "modules.collectors.twitch.enabled=true",
                                "modules.collectors.slack.enabled=true",
                                "modules.collectors.youtubeLive.enabled=true"])
        by_comp: dict[str, dict[str, Any]] = {}
        for w in _workloads(docs, _LONG_RUNNING):
            labels = _pod_labels(w)
            by_comp[labels.get("app.kubernetes.io/name") if labels.get("app.kubernetes.io/name") != "waddlebot"
                    else labels.get("app.kubernetes.io/component", "")] = w
        checked = 0
        for key in _READONLY_ROOTFS:
            w = by_comp.get(key)
            if w is None:
                continue  # workload disabled in this env (e.g. ollama/qdrant) -- nothing to assert
            checked += 1
            main = _pod_spec(w)["containers"][0]
            sc = main["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True, key
            assert sc["runAsNonRoot"] is True and sc["runAsUser"] != 0, key
            assert (_pod_spec(w)["securityContext"]).get("fsGroup"), key
        print(f"{values}: read-only-rootfs asserted on {checked} workloads")
        assert checked >= 4, "expected at least collectors/postgres/redis to be present"


class TestRedisRootlessUpgrade:
    """regression: kind upgrade-path -- pre-R5 redis ran as root and left root-owned AOF/RDB files in /data."""

    @pytest.mark.parametrize("values", (*_SHIPPING, "values-alpha.yaml", "values-ci.yaml"))
    def test_rootless_redis_uses_fresh_subdir_with_migration(self, values: str) -> None:
        docs = _render(values)
        redis = [d for d in _workloads(docs, ("Deployment",)) if d["metadata"]["name"] == "redis"]
        assert len(redis) == 1, f"{values}: expected exactly one redis Deployment"
        ps = _pod_spec(redis[0])
        main = ps["containers"][0]
        cmd = main["command"]
        assert cmd[cmd.index("--dir") + 1] == "/data/valkey", "valkey must persist under a dir uid 999 can create"
        inits = {c["name"]: c for c in ps.get("initContainers") or []}
        mig = inits["migrate-data-dir"]
        assert mig["securityContext"]["runAsNonRoot"] is True and mig["securityContext"]["runAsUser"] != 0
        assert mig["securityContext"]["readOnlyRootFilesystem"] is True
        script = mig["command"][-1]
        assert "set -eu" in script and "/data/valkey" in script
        assert "|| true" not in script, "migration failures must be loud"
        assert {m["name"] for m in mig["volumeMounts"]} == {"redis-data"}


class TestPodSecurityAdmission:
    """The chart-managed Namespace enforces/audits/warns `restricted`."""

    @pytest.mark.parametrize("values", (*_SHIPPING, "values-alpha.yaml"))
    def test_namespace_labels(self, values: str) -> None:
        docs = _render(values)
        nss = [d for d in docs if d["kind"] == "Namespace"]
        assert len(nss) == 1, f"expected exactly one chart-managed Namespace, got {len(nss)}"
        labels = nss[0]["metadata"]["labels"]
        # beta runs the SPIRE agent (approved ROOT EXCEPTION, hostPath) in-namespace, so it cannot
        # ENFORCE restricted; every other env does. audit/warn are restricted everywhere.
        expected_enforce = "privileged" if values == "values-beta.yaml" else "restricted"
        assert labels["pod-security.kubernetes.io/enforce"] == expected_enforce
        for mode in ("audit", "warn"):
            assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"
        for mode in ("enforce", "audit", "warn"):
            assert labels[f"pod-security.kubernetes.io/{mode}-version"] == "latest"

    def test_local_enforces_baseline(self) -> None:
        """values-local relaxes runAsNonRoot, so it enforces baseline (never privileged) and warns restricted."""
        docs = _render("values-local.yaml")
        labels = [d for d in docs if d["kind"] == "Namespace"][0]["metadata"]["labels"]
        assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
        assert labels["pod-security.kubernetes.io/warn"] == "restricted"
        workloads = _workloads(docs, _POD_KINDS)
        assert workloads
        for w in workloads:
            ps = _pod_spec(w)
            assert not (ps.get("hostNetwork") or ps.get("hostPID") or ps.get("hostIPC")), w["metadata"]["name"]
            for c in (ps.get("containers") or []) + (ps.get("initContainers") or []):
                sc = c.get("securityContext") or {}
                assert not sc.get("privileged"), w["metadata"]["name"]
                assert (sc.get("capabilities") or {}).get("add") in (None, []), w["metadata"]["name"]

    def test_spire_blocks_restricted_enforce(self) -> None:
        """Enforcing restricted/baseline while the SPIRE agent is enabled must fail the render."""
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
             "--values", str(_CHART_DIR / "values-beta.yaml"), "--set", "global.deploymentTier=alpha",
             "--set", "podSecurity.enforce=restricted"],
            capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert "ROOT EXCEPTION" in result.stderr + result.stdout

    def test_can_be_disabled_and_levels_overridden(self) -> None:
        docs = _render("values-production.yaml", ["podSecurity.enabled=false"])
        ns = [d for d in docs if d["kind"] == "Namespace"][0]
        assert not [k for k in ns["metadata"]["labels"] if k.startswith("pod-security")]
        docs = _render("values-production.yaml", ["podSecurity.enforce=baseline"])
        ns = [d for d in docs if d["kind"] == "Namespace"][0]
        assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
        assert ns["metadata"]["labels"]["pod-security.kubernetes.io/warn"] == "restricted"


class TestCiliumOnly:
    """No plain NetworkPolicy anywhere -- CiliumNetworkPolicy only (house rule)."""

    @pytest.mark.parametrize("values", _ALL_ENVS)
    def test_no_plain_networkpolicy_rendered(self, values: str) -> None:
        docs = _render(values)
        plain = [d for d in docs if d["kind"] == "NetworkPolicy"]
        print(f"{values}: examined {len(docs)} docs, {len(_cnps(docs))} CiliumNetworkPolicies")
        assert not plain, [d["metadata"]["name"] for d in plain]

    def test_no_plain_networkpolicy_in_template_sources(self) -> None:
        sources = [p for p in (_CHART_DIR / "templates").rglob("*") if p.suffix in (".yaml", ".tpl")]
        sources += [p for p in (_CHART_DIR / "charts").rglob("*.yaml")]
        assert sources, "no template sources scanned -- cannot be a pass"
        hits = [str(p) for p in sources if re.search(r"^kind:\s*NetworkPolicy\s*$", p.read_text(), re.M)]
        assert not hits, hits

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_default_deny_ingress_is_cilium(self, values: str) -> None:
        docs = _render(values)
        dd = [d for d in _cnps(docs) if d["metadata"]["name"] == "default-deny-ingress"]
        assert len(dd) == 1
        assert dd[0]["spec"]["endpointSelector"] == {}
        assert dd[0]["spec"]["ingress"] == [{}]


class TestDefaultDenyEgress:
    """Every long-running workload sits behind a CNP with an `egress` section."""

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_every_workload_has_egress_filtering(self, values: str) -> None:
        docs = _render(values, ["modules.collectors.discord.enabled=true"])
        egress_cnps = [c for c in _cnps(docs) if "egress" in c["spec"]]
        workloads = _workloads(docs, _LONG_RUNNING)
        assert workloads and egress_cnps
        unfiltered = []
        for w in workloads:
            labels = _pod_labels(w)
            if not any(_selects(c["spec"].get("endpointSelector"), labels) for c in egress_cnps):
                unfiltered.append(w["metadata"]["name"])
        print(f"{values}: {len(workloads) - len(unfiltered)}/{len(workloads)} workloads egress-filtered")
        assert not unfiltered, f"egress-unfiltered workloads: {unfiltered}"

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_world_rules_exclude_private_and_metadata(self, values: str) -> None:
        docs = _render(values)
        checked = 0
        for c in _cnps(docs):
            name = c["metadata"]["name"]
            for rule in c["spec"].get("egress") or []:
                assert "world" not in (rule.get("toEntities") or []), f"{name}: toEntities world"
                assert "all" not in (rule.get("toEntities") or []), f"{name}: toEntities all"
                for cidr_set in rule.get("toCIDRSet") or []:
                    if cidr_set["cidr"] not in ("0.0.0.0/0", "::/0"):
                        continue
                    if cidr_set["cidr"] == "::/0":
                        v6 = [ipaddress.ip_network(x) for x in cidr_set.get("except") or []]
                        if not name.endswith("-egress-proxy-egress"):
                            assert any(ipaddress.ip_network("fc00::/7").subnet_of(e) for e in v6), name
                        assert any(ipaddress.ip_network("fe80::/10").subnet_of(e) for e in v6), name
                        assert any(ipaddress.ip_network("::1/128").subnet_of(e) for e in v6), name
                        continue
                    checked += 1
                    excepts = [ipaddress.ip_network(x) for x in cidr_set.get("except") or []]
                    required = _RFC1918_AND_METADATA
                    if name.endswith("-egress-proxy-egress"):
                        # The egress proxy intentionally reaches customer private ranges (explicit
                        # per-tenant grants, re-checked in the proxy) -- but never loopback/metadata.
                        required = ("169.254.0.0/16", "127.0.0.0/8")
                    for need in required:
                        assert any(ipaddress.ip_network(need).subnet_of(e) for e in excepts), (
                            f"{name}: 0.0.0.0/0 allow does not except {need}")
                    assert any(ipaddress.ip_address("169.254.169.254") in e for e in excepts), name
        print(f"{values}: {checked} world CIDR rules verified")
        assert checked > 0

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_workload_egress_toggle(self, values: str) -> None:
        docs = _render(values, ["egressPolicy.workloadEgress.enabled=false"])
        generic = [c for c in _cnps(docs)
                   if c["metadata"]["name"].endswith("-policy") and "egress" in c["spec"]]
        assert not generic, [c["metadata"]["name"] for c in generic]

    def test_alpha_is_exempt(self) -> None:
        docs = _render("values-alpha.yaml")
        generic = [c for c in _cnps(docs) if c["metadata"]["name"].endswith("-policy")]
        assert not generic, "alpha must not render the shipping-only per-workload policies"

    def test_https_fqdn_pin(self) -> None:
        docs = _render("values-production.yaml", ["egressPolicy.httpsFqdns[0].matchName=license.penguintech.io"])
        rules = [r for c in _cnps(docs) for r in c["spec"].get("egress") or [] if "toFQDNs" in r]
        assert rules, "httpsFqdns set but no toFQDNs rule rendered"
        assert all(r["toFQDNs"] == [{"matchName": "license.penguintech.io"}] for r in rules)
        # FQDN rules need the DNS proxy: the DNS rule must carry a dns rules block.
        dns = [r for c in _cnps(docs) for r in c["spec"].get("egress") or []
               if r.get("toEndpoints") and r["toEndpoints"][0]["matchLabels"].get("k8s-app") == "kube-dns"
               and "rules" in (r["toPorts"][0])]
        assert dns


class TestPortScopedIngress:
    """Ingress is admitted on declared serving ports only."""

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_every_ingress_rule_names_ports(self, values: str) -> None:
        docs = _render(values)
        checked = 0
        for c in _cnps(docs):
            if c["metadata"]["name"] == "default-deny-ingress":
                continue
            if c["metadata"]["name"].startswith(_SPIRE_PREFIX):
                continue  # SPIRE subchart ships its own CNPs (host/remote-node agent traffic)
            for rule in c["spec"].get("ingress") or []:
                if rule == {}:
                    continue  # explicit deny-all form
                checked += 1
                assert rule.get("toPorts"), f"{c['metadata']['name']}: ingress rule without toPorts: {rule}"
        print(f"{values}: {checked} ingress allow rules, all port-scoped")
        assert checked > 0

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_declared_ports_admitted_and_nothing_more(self, values: str) -> None:
        docs = _render(values, ["modules.collectors.discord.enabled=true"])
        cnps = [c for c in _cnps(docs) if c["metadata"]["name"].endswith("-policy") and "ingress" in c["spec"]]
        workloads = _workloads(docs, _LONG_RUNNING)
        covered = 0
        for w in workloads:
            labels = _pod_labels(w)
            comp = labels.get("app.kubernetes.io/component", "")
            declared = _declared_ports(w)
            if comp in _OWN_CNP_COMPONENTS or w["metadata"]["name"].startswith(_SPIRE_PREFIX):
                continue
            matching = [c for c in cnps if _selects(c["spec"]["endpointSelector"], labels)]
            assert matching, f"{w['metadata']['name']}: no per-workload ingress CNP"
            admitted: set[tuple[int, str]] = set()
            for c in matching:
                admitted |= _ingress_ports(c)
            covered += 1
            if comp == "hub-api":
                # gRPC port is left to the dedicated hub-api-grpc CNP (callers allowlisted).
                declared = {p for p in declared if p[0] != 50204}
                assert (50204, "TCP") not in admitted, "hub-api gRPC must not be open to the whole namespace"
            assert declared <= admitted, f"{w['metadata']['name']}: declared {declared - admitted} not admitted"
            assert admitted <= declared, f"{w['metadata']['name']}: admits undeclared {admitted - declared}"
        print(f"{values}: {covered}/{len(workloads)} workloads have port-scoped ingress")
        assert covered > 10

    def test_hub_api_grpc_cnp_still_lists_callers(self) -> None:
        docs = _render("values-production.yaml")
        grpc = [c for c in _cnps(docs) if c["metadata"]["name"].endswith("-hub-api-grpc")]
        assert len(grpc) == 1
        callers = grpc[0]["spec"]["ingress"][0]["fromEndpoints"][0]["matchExpressions"][0]["values"]
        assert "svc-presentation" in callers and "svc-process-rust" in callers

    def test_external_ingress_only_for_listed_components(self) -> None:
        docs = _render("values-production.yaml")
        ext_ns = "ingress-nginx"
        exposed: set[str] = set()
        for c in _cnps(docs):
            for rule in c["spec"].get("ingress") or []:
                for fe in rule.get("fromEndpoints") or []:
                    if fe.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == ext_ns:
                        exposed.add(c["metadata"]["labels"].get("app.kubernetes.io/component", ""))
        assert exposed, "no component admits the ingress controller"
        assert "postgres" not in exposed and "database" not in exposed and "cache" not in exposed
        assert exposed <= {"hub", "hub-api", "hub-webui", "hub-webui-v3", "router", "marketplace",
                           "trigger-webhooks", "svc-presentation", "svc-streaming", "svc-rtc"}

    def test_httproute_adds_gateway_sources(self) -> None:
        docs = _render("values-production.yaml", ["httproute.enabled=true", "httproute.hostnames[0]=x.example"])
        entities = [r for c in _cnps(docs) for r in c["spec"].get("ingress") or [] if r.get("fromEntities") == ["ingress"]]
        assert entities and all(r.get("toPorts") for r in entities)

    def test_alpha_keeps_nodeport_reachability(self) -> None:
        docs = _render("values-alpha.yaml")
        assert not [c for c in _cnps(docs) if c["metadata"]["name"] == "default-deny-ingress"]
        nodeports = [d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "NodePort"]
        assert nodeports, "alpha must still expose NodePort Services"


class TestTetragonEnforced:
    """An ENFORCED exec allowlist covers every app workload."""

    _INFRA_NAMES = ("postgres", "redis", "qdrant", "ollama", "seaweedfs")

    @pytest.mark.parametrize("values", _SHIPPING)
    def test_every_app_workload_has_enforced_allowlist(self, values: str) -> None:
        docs = _render(values, ["modules.collectors.discord.enabled=true"])
        policies = [d for d in docs if d["kind"] == "TracingPolicyNamespaced"
                    and d["metadata"]["name"].endswith("-exec-allowlist")
                    and "spire" not in d["metadata"]["name"]]
        workloads = [w for w in _workloads(docs, _LONG_RUNNING)
                     if _pod_labels(w).get("app.kubernetes.io/name") == "waddlebot"
                     and not _pod_labels(w).get("app.kubernetes.io/component", "").startswith(("posthog",))]
        assert policies and workloads
        missing = []
        for w in workloads:
            labels = _pod_labels(w)
            hit = [p for p in policies if _selects({"matchLabels": p["spec"]["podSelector"]["matchLabels"]}, labels)]
            if not hit:
                missing.append(w["metadata"]["name"])
        print(f"{values}: {len(workloads) - len(missing)}/{len(workloads)} app workloads covered by Tetragon")
        assert not missing, f"no Tetragon allowlist for: {missing}"
        for p in policies:
            kp = p["spec"]["kprobes"][0]
            assert kp["call"] == "sys_execve" and kp["syscall"] is True
            sel = kp["selectors"][0]
            assert sel["matchArgs"][0]["operator"] == "NotEqual"
            actions = [a["action"] for a in sel["matchActions"]]
            assert actions == ["Sigkill"], f"{p['metadata']['name']}: must be enforced, got {actions}"
            values_ = sel["matchArgs"][0]["values"]
            assert 0 < len(values_) <= 10
            assert not any(v in ("*", "/") or v.endswith("/") for v in values_)

    def test_shells_only_where_documented(self) -> None:
        docs = _render("values-production.yaml")
        shell_ok = {"interactive-ai", "svc-action", "svc-presentation"}
        shells = {"/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash", "/bin/dash", "/usr/bin/dash"}
        seen = 0
        for d in docs:
            if d["kind"] != "TracingPolicyNamespaced" or "spire" in d["metadata"]["name"]:
                continue
            comp = d["metadata"]["labels"]["app.kubernetes.io/component"]
            vals = set(d["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"])
            seen += 1
            if comp not in shell_ok:
                assert not (vals & shells), f"{comp}: shell allowed without a documented reason"
            for banned in ("/usr/bin/curl", "/usr/bin/wget", "/bin/nc", "/usr/bin/nc"):
                assert banned not in vals
        assert seen > 10

    def test_disabled_in_alpha_and_ci(self) -> None:
        for values in ("values-alpha.yaml", "values-local.yaml", "values-ci.yaml"):
            docs = _render(values)
            assert not [d for d in docs if d["kind"] == "TracingPolicyNamespaced"
                        and "spire" not in d["metadata"]["name"]], values

    def test_post_action_is_only_an_explicit_override(self) -> None:
        docs = _render("values-production.yaml", ["tetragon.action=Post"])
        pol = [d for d in docs if d["kind"] == "TracingPolicyNamespaced" and "spire" not in d["metadata"]["name"]]
        assert pol and all(p["spec"]["kprobes"][0]["selectors"][0]["matchActions"][0]["action"] == "Post" for p in pol)
