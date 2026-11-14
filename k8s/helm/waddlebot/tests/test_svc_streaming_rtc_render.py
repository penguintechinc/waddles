"""Render tests for the RTC (svc-streaming SFU) Helm wiring, on every environment overlay and on
both Helm majors the chart must support (v3.17.x and v4).

Pins, per environment (local, alpha, dal2alpha, beta, gamma, production, ci):

1. svc-streaming declares EXACTLY its serving ports -- HTTP 8208, metrics, gRPC 50208, RTMP, SRT and
   ONE multiplexed UDP media port (plus an optional ICE-TCP port) -- never a range, never a
   hostPort/hostNetwork, never externalIPs;
2. the dedicated `-svc-streaming-rtc-media` Service publishes only that media port: NodePort (fixed
   nodePort == bound port) on alpha/dal2alpha/local, LoadBalancer on beta/gamma/production, and the
   chart REFUSES NodePort outside alpha/local and any published port != bound port;
3. the CiliumNetworkPolicies are exact: gRPC <- hub-api pods only, media <- the configured
   entities only, and the generic per-workload policy admits neither;
4. hub-api's RoomsClient env (gRPC target, TLS server name that is a cert SAN, SPIFFE service id,
   timeout) and the retired `STREAMING_RTC_URL` (never rendered); no secret value in any env;
5. HTTP probes (documented EXCEPTION), startup budget, owner resource policy, Recreate/replicas 1,
   no PDB; Tetragon allowlist + PSA-restricted container context;
6. `helm lint` and kubeconform over every rendered environment.

Every helper reports what it examined; zero matching documents is a hard failure, never a silent
pass (critical-rules.md Verification Integrity). Helm binaries: every distinct major found among
`helm3`, `helm4`, `helm` on PATH is exercised (override with RTC_HELM_BINARIES, comma separated).
"""

from __future__ import annotations

import base64
import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_TEMPLATE = _CHART_DIR / "templates" / "svc-streaming.yaml"

# env -> (values file, media Service type, bound UDP port, ingress CNPs rendered, CNP entities)
_ENVS: dict[str, dict[str, Any]] = {
    "local": dict(type="NodePort", udp=31400, cnp=False, entities=None),
    "alpha": dict(type="NodePort", udp=31400, cnp=False, entities=None),
    "dal2alpha": dict(
        type="NodePort", udp=31400, cnp=True, entities=["world", "host", "remote-node"]
    ),
    "beta": dict(type="LoadBalancer", udp=40000, cnp=True, entities=["world"]),
    "gamma": dict(type="LoadBalancer", udp=40000, cnp=True, entities=["world"]),
    "production": dict(type="LoadBalancer", udp=40000, cnp=True, entities=["world"]),
    "ci": dict(type=None, udp=None, cnp=False, entities=None),  # svc-streaming disabled
}
_ENABLED_ENVS = [e for e, c in _ENVS.items() if c["type"]]
_SHIPPING = ("beta", "gamma", "production")
_FIXED_PORTS = {
    ("http", 8208, "TCP"),
    ("metrics", 9090, "TCP"),
    ("grpc", 50208, "TCP"),
    ("rtmp", 1935, "TCP"),
    ("srt", 9000, "UDP"),
}


def _find_helm() -> dict[str, str]:
    """Distinct Helm majors found on PATH (`major -> binary`); empty means a hard failure."""
    names = os.environ.get("RTC_HELM_BINARIES", "helm3,helm4,helm").split(",")
    found: dict[str, str] = {}
    for name in names:
        path = shutil.which(name.strip())
        if not path:
            continue
        out = subprocess.run(  # noqa: S603 -- fixed argv, test-only
            [path, "version", "--short"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if out.startswith("v") and out.split(".")[0][1:].isdigit():
            found.setdefault(out.split(".")[0][1:], path)
    return found


_HELMS = _find_helm()


@functools.lru_cache(maxsize=None)
def _render(helm: str, env: str, extra: tuple[str, ...] = ()) -> tuple[dict[str, Any], ...]:
    """helm-template one environment (the same `deploymentTier=alpha` override the other render
    suites use so shipping overlays do not trip the unrelated secret-provisioning guards)."""
    args = [
        helm, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
        "--values", str(_CHART_DIR / f"values-{env}.yaml"),
        "--set", "global.deploymentTier=alpha",
    ]  # fmt: skip
    for kv in extra:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        pytest.fail(f"{helm} template failed for {env} {extra}:\n{result.stderr[-1500:]}")
    docs = tuple(d for d in yaml.safe_load_all(result.stdout) if d)
    assert docs, f"{helm} template produced zero documents for {env} -- cannot be a pass"
    return docs


def _render_fails(helm: str, env: str, extra: list[str], tier: str = "alpha") -> str:
    """Render expecting failure; returns stderr (a success is a test failure)."""
    args = [
        helm, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
        "--values", str(_CHART_DIR / f"values-{env}.yaml"),
        "--set", f"global.deploymentTier={tier}",
    ]  # fmt: skip
    for kv in extra:
        # "json:k=v" entries go through --set-json (needed to pass an empty list).
        args += ["--set-json", kv[5:]] if kv.startswith("json:") else ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0, f"expected a render failure for {env} {extra}"
    return result.stderr


def _comp(doc: dict[str, Any]) -> str:
    return doc["metadata"].get("labels", {}).get("app.kubernetes.io/component", "")


def _streaming_deployment(docs: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    found = [
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-svc-streaming")
    ]  # fmt: skip
    assert len(found) == 1, f"expected exactly one svc-streaming Deployment, got {len(found)}"
    return found[0]


def _container(dep: dict[str, Any]) -> dict[str, Any]:
    cs = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "svc-streaming"]
    assert len(cs) == 1
    return cs[0]


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container.get("env", [])}


def _services(docs: tuple[dict[str, Any], ...], suffix: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == "Service" and d["metadata"]["name"].endswith(suffix)]


def _cnp(docs: tuple[dict[str, Any], ...], suffix: str) -> list[dict[str, Any]]:
    return [
        d for d in docs
        if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith(suffix)
    ]  # fmt: skip


def _ports(rule: dict[str, Any]) -> list[str]:
    return [f"{p['port']}/{p['protocol']}" for t in rule["toPorts"] for p in t["ports"]]


@pytest.fixture(params=sorted(_HELMS), ids=lambda m: f"helm{m}")
def helm(request: pytest.FixtureRequest) -> str:
    """One Helm binary per distinct major."""
    return _HELMS[request.param]


def test_a_helm_binary_was_found() -> None:
    """A suite that examined zero Helm binaries is a failure, not a skip."""
    assert _HELMS, "no helm binary found on PATH (helm3/helm4/helm)"
    print(f"helm majors exercised: {sorted(_HELMS)}")


@pytest.mark.parametrize("env", _ENABLED_ENVS)
def test_container_ports_are_declared_exactly(helm: str, env: str) -> None:
    docs = _render(helm, env)
    dep = _streaming_deployment(docs)
    pod = dep["spec"]["template"]["spec"]
    c = _container(dep)
    got = {(p["name"], p["containerPort"], p["protocol"]) for p in c["ports"]}
    expected = _FIXED_PORTS | {("webrtc-media", _ENVS[env]["udp"], "UDP")}
    assert got == expected, f"{env}: {got ^ expected}"
    assert len(c["ports"]) == len(expected), "duplicate containerPort entries"
    assert not any("hostPort" in p for p in c["ports"]), "hostPort is forbidden"
    assert not pod.get("hostNetwork") and not pod.get("hostPID") and not pod.get("hostIPC")
    envs = _env(c)
    port = _ENVS[env]["udp"]
    assert envs["WEBRTC_UDP_RANGE"]["value"] == f"{port}-{port}", "single mux port, not a range"
    assert "WEBRTC_ICE_TCP_PORT" not in envs, "ICE-TCP is opt-in"
    print(f"{env}: {len(got)} container ports, UDP mux {port}")


@pytest.mark.parametrize("env", _ENABLED_ENVS)
def test_media_service_is_scoped_to_the_single_port(helm: str, env: str) -> None:
    docs = _render(helm, env)
    media = _services(docs, "-svc-streaming-rtc-media")
    assert len(media) == 1, f"{env}: expected one media Service, got {len(media)}"
    spec = media[0]["spec"]
    want = _ENVS[env]
    assert spec["type"] == want["type"]
    assert "externalIPs" not in spec
    assert [(p["name"], p["port"], p["protocol"]) for p in spec["ports"]] == [
        ("rtc-media", want["udp"], "UDP")
    ], "the media Service publishes ONLY the single UDP mux port"
    assert spec["ports"][0]["targetPort"] == "webrtc-media"
    sel = spec["selector"]
    assert sel["app.kubernetes.io/component"] == "svc-streaming"
    if want["type"] == "NodePort":
        assert spec["ports"][0]["nodePort"] == want["udp"], "nodePort must equal the bound port"
        assert spec["externalTrafficPolicy"] == "Cluster"
    else:
        assert "nodePort" not in spec["ports"][0]
        assert spec["externalTrafficPolicy"] == "Local"
        assert "loadBalancerSourceRanges" not in spec, "open by default (browsers anywhere)"


@pytest.mark.parametrize("env", _SHIPPING)
def test_shipping_envs_expose_no_nodeport_hostport_or_external_ips(helm: str, env: str) -> None:
    docs = _render(helm, env)
    targeting = [
        d for d in docs
        if d["kind"] == "Service" and d["spec"].get("selector", {}).get(
            "app.kubernetes.io/component"
        ) == "svc-streaming"
    ]  # fmt: skip
    assert len(targeting) == 2, f"{env}: expected ClusterIP + media LB, got {len(targeting)}"
    for svc in targeting:
        assert svc["spec"].get("type", "ClusterIP") in ("ClusterIP", "LoadBalancer")
        assert "externalIPs" not in svc["spec"]
        for p in svc["spec"]["ports"]:
            assert "nodePort" not in p, f"{env}: {svc['metadata']['name']} sets a nodePort"
    assert [s["spec"]["type"] for s in targeting].count("LoadBalancer") == 1
    pods = [d for d in docs if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet")]
    for w in pods:
        spec = w["spec"]["template"]["spec"]
        if w["metadata"]["name"].startswith(("waddlebot-spire", "spire")):
            continue  # the SPIRE subchart's approved agent exception, covered by its own suite
        assert not spec.get("hostNetwork"), w["metadata"]["name"]
        for c in spec.get("containers", []):
            assert not any(
                "hostPort" in p for p in c.get("ports", [])
            ), f"{env}: {w['metadata']['name']}/{c['name']} declares a hostPort"
    print(f"{env}: {len(targeting)} svc-streaming Services, {len(pods)} workloads, no external ports")


def test_ci_renders_no_svc_streaming(helm: str) -> None:
    docs = _render(helm, "ci")
    leftovers = [d["metadata"]["name"] for d in docs if "svc-streaming" in d["metadata"]["name"]]
    assert not leftovers, leftovers
    hub = [d for d in docs if d["kind"] == "Deployment" and _comp(d) == "hub-api"]
    assert len(hub) == 1
    env = _env(hub[0]["spec"]["template"]["spec"]["containers"][0])
    assert "STREAMING_RTC_GRPC_TARGET" not in env, "no target when svc-streaming is off"


@pytest.mark.parametrize("env", [e for e, c in _ENVS.items() if c["type"] and c["cnp"]])
def test_cnp_rules_are_exact(helm: str, env: str) -> None:
    docs = _render(helm, env)
    want = _ENVS[env]
    grpc = _cnp(docs, "-svc-streaming-grpc")
    media = _cnp(docs, "-svc-streaming-media")
    policy = _cnp(docs, "-svc-streaming-policy")
    assert (len(grpc), len(media), len(policy)) == (1, 1, 1), (len(grpc), len(media), len(policy))
    # gRPC <- hub-api pods only, on 50208/TCP only.
    ing = grpc[0]["spec"]["ingress"]
    assert len(ing) == 1 and _ports(ing[0]) == ["50208/TCP"]
    (peer,) = ing[0]["fromEndpoints"]
    assert peer["matchLabels"]["app.kubernetes.io/component"] == "hub-api"
    assert "io.kubernetes.pod.namespace" in peer["matchLabels"]
    # media <- the configured entities only, on the single UDP port only.
    ing = media[0]["spec"]["ingress"]
    assert len(ing) == 1 and _ports(ing[0]) == [f"{want['udp']}/UDP"]
    assert ing[0]["fromEntities"] == want["entities"]
    assert "fromEndpoints" not in ing[0] and "fromCIDR" not in ing[0]
    # The generic policy admits neither the gRPC port nor the media port, to anyone.
    admitted = {p for r in policy[0]["spec"]["ingress"] for p in _ports(r)}
    assert admitted == {"8208/TCP", "9090/TCP", "1935/TCP", "9000/UDP"}, admitted
    # default-deny egress profile unchanged (`world` carve-outs): DNS + namespace + CIDR set.
    assert "egress" in policy[0]["spec"]
    for c in (grpc[0], media[0]):
        assert c["spec"]["endpointSelector"]["matchLabels"]["app.kubernetes.io/component"] == (
            "svc-streaming"
        )
        assert "egress" not in c["spec"], "dedicated CNPs must not alter egress"


@pytest.mark.parametrize("env", ["local", "alpha"])
def test_alpha_without_network_policy_renders_no_dedicated_cnps(helm: str, env: str) -> None:
    docs = _render(helm, env)
    assert not _cnp(docs, "-svc-streaming-grpc") and not _cnp(docs, "-svc-streaming-media")


def test_ice_tcp_is_opt_in_and_scoped(helm: str) -> None:
    extra = ("pipeline.svcStreaming.webrtc.iceTcp.enabled=true",)
    docs = _render(helm, "beta", extra)
    c = _container(_streaming_deployment(docs))
    assert ("webrtc-ice-tcp", 40000, "TCP") in {
        (p["name"], p["containerPort"], p["protocol"]) for p in c["ports"]
    }
    assert _env(c)["WEBRTC_ICE_TCP_PORT"]["value"] == "40000"
    (media,) = _services(docs, "-svc-streaming-rtc-media")
    assert [(p["name"], p["port"], p["protocol"]) for p in media["spec"]["ports"]] == [
        ("rtc-media", 40000, "UDP"),
        ("rtc-ice-tcp", 40000, "TCP"),
    ]
    (mcnp,) = _cnp(docs, "-svc-streaming-media")
    assert _ports(mcnp["spec"]["ingress"][0]) == ["40000/UDP", "40000/TCP"]
    (pol,) = _cnp(docs, "-svc-streaming-policy")
    assert "40000/TCP" not in {p for r in pol["spec"]["ingress"] for p in _ports(r)}
    # alpha NodePort: both nodePorts must equal their bound ports.
    adocs = _render(
        helm, "alpha",
        (*extra, "pipeline.svcStreaming.webrtc.iceTcp.port=31401",
         "pipeline.svcStreaming.webrtc.media.service.nodePort.tcp=31401"),
    )  # fmt: skip
    (amedia,) = _services(adocs, "-svc-streaming-rtc-media")
    assert {(p["protocol"], p["port"], p["nodePort"]) for p in amedia["spec"]["ports"]} == {
        ("UDP", 31400, 31400),
        ("TCP", 31401, 31401),
    }


def test_lb_hooks_render(helm: str) -> None:
    docs = _render(
        helm, "beta",
        ("pipeline.svcStreaming.webrtc.media.service.loadBalancerSourceRanges[0]=203.0.113.0/24",
         "pipeline.svcStreaming.webrtc.media.service.annotations.example\\.io/pool=media",
         "pipeline.svcStreaming.webrtc.media.service.loadBalancerClass=example.io/udp-lb"),
    )  # fmt: skip
    (media,) = _services(docs, "-svc-streaming-rtc-media")
    assert media["spec"]["loadBalancerSourceRanges"] == ["203.0.113.0/24"]
    assert media["metadata"]["annotations"] == {"example.io/pool": "media"}
    assert media["spec"]["loadBalancerClass"] == "example.io/udp-lb"


@pytest.mark.parametrize(
    ("env", "extra", "tier", "needle"),
    [
        ("beta", ["pipeline.svcStreaming.webrtc.media.service.type=NodePort"], "beta",
         "alpha/local exemption only"),
        ("production", ["pipeline.svcStreaming.webrtc.media.service.type=NodePort"], "production",
         "alpha/local exemption only"),
        ("alpha", ["pipeline.svcStreaming.webrtc.media.service.nodePort.udp=31999"], "alpha",
         "must equal webrtc.udpPort"),
        ("alpha", ["pipeline.svcStreaming.webrtc.udpPort=100"], "alpha", "unprivileged port"),
        ("beta", ["pipeline.svcStreaming.webrtc.udpPort=9000"], "alpha", "collides with srtPort"),
        ("beta", ["pipeline.svcStreaming.webrtc.iceTcp.enabled=true",
                  "pipeline.svcStreaming.webrtc.iceTcp.port=8208"], "alpha", "collides with port"),
        ("beta", ["pipeline.svcStreaming.webrtc.media.service.type=ClusterIP"], "alpha",
         "must be NodePort or LoadBalancer"),
        ("beta", ["pipeline.svcStreaming.webrtc.media.service.nodePort.udp=31400"], "alpha",
         "must be 0 for a LoadBalancer"),
        ("beta", ["json:pipeline.svcStreaming.webrtc.media.networkPolicy.fromEntities=[]"], "alpha",
         "must not be empty"),
        ("beta", ["pipeline.svcStreaming.webrtc.media.networkPolicy.fromEntities={all}"], "alpha",
         "is not allowed"),
    ],
)  # fmt: skip
def test_misconfiguration_fails_the_render_loudly(
    helm: str, env: str, extra: list[str], tier: str, needle: str
) -> None:
    stderr = _render_fails(helm, env, extra, tier)
    assert needle in stderr, stderr[-800:]


def _hub_api_env(docs: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    hub = [d for d in docs if d["kind"] == "Deployment" and _comp(d) == "hub-api"]
    assert len(hub) == 1, f"expected one hub-api Deployment, got {len(hub)}"
    containers = hub[0]["spec"]["template"]["spec"]["containers"]
    assert containers
    return {e["name"]: e for c in containers for e in c.get("env", [])}


def _cert_sans(docs: tuple[dict[str, Any], ...]) -> set[str]:
    """DNS SANs of the shared host-api-tls certificate (cert-manager Certificate, or decoded
    from the chart-generated Secret on alpha/local)."""
    for d in docs:
        if d["kind"] == "Certificate" and d["metadata"]["name"].endswith("-host-api-tls"):
            return set(d["spec"]["dnsNames"])
    for d in docs:
        if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-host-api-tls"):
            raw = d["stringData"]["tls.crt"] if "stringData" in d else None
            pem = raw.encode() if raw else base64.b64decode(d["data"]["tls.crt"])
            with tempfile.NamedTemporaryFile(suffix=".pem") as f:
                f.write(pem)
                f.flush()
                out = subprocess.run(  # noqa: S603, S607 -- fixed argv, test-only
                    ["openssl", "x509", "-in", f.name, "-noout", "-ext", "subjectAltName"],
                    capture_output=True, text=True, check=True,
                ).stdout  # fmt: skip
            return {
                tok.strip().split("DNS:", 1)[1]
                for tok in out.replace("\n", ",").split(",")
                if tok.strip().startswith("DNS:")
            }
    pytest.fail("no host-api-tls Certificate/Secret rendered")


@pytest.mark.parametrize("env", _ENABLED_ENVS)
def test_hub_api_rooms_client_env(helm: str, env: str) -> None:
    docs = _render(helm, env)
    env_ = _hub_api_env(docs)
    (clusterip,) = [
        s for s in _services(docs, "-svc-streaming") if s["spec"].get("type", "ClusterIP") == "ClusterIP"
    ]  # fmt: skip
    name = clusterip["metadata"]["name"]
    if env == "local":
        # pipeline.rustDataPlane is off locally -> no host-api-tls material -> the rooms gRPC
        # listener and hub-api's client are both OFF (by design: the call then fails loud).
        assert "STREAMING_RTC_GRPC_TARGET" not in env_
        assert "ROOMS_GRPC_PORT" not in _env(_container(_streaming_deployment(docs)))
        assert "STREAMING_RTC_URL" not in env_
        return
    assert env_["STREAMING_RTC_GRPC_TARGET"]["value"] == f"{name}:50208"
    assert "STREAMING_RTC_URL" not in env_, "the REST proxy env is retired"
    assert env_["STREAMING_RTC_GRPC_SERVER_NAME"]["value"] == name
    assert env_["STREAMING_RTC_TIMEOUT_SECONDS"]["value"] == "10"
    assert env_["STREAMING_RTC_GRPC_CA_FILE"]["value"].endswith("/ca.crt")
    sans = _cert_sans(docs)
    assert name in sans, (name, sans)
    # service identity: the hub-api SPIFFE id (the minted JWT `sub`), allow-listed with exactly the
    # three RoomsService scopes, and pinned on the server side.
    sid = env_["STREAMING_RTC_SERVICE_ID"]["value"]
    assert sid.startswith("spiffe://penguintech.io/") and sid.endswith("/hub-api")
    srv = _env(_container(_streaming_deployment(docs)))
    assert srv["ROOMS_GRPC_ALLOWED_SUBJECTS"]["value"] == sid
    idents = json.loads(env_["SERVICE_JWT_IDENTITIES"]["value"])
    hub_ident = next(i for i in (idents if isinstance(idents, list) else idents.values())
                     if sid in json.dumps(i))  # fmt: skip
    assert set(hub_ident.get("allowedScopes", hub_ident.get("allowed_scopes", []))) == {
        "streaming.rooms:read", "streaming.rooms:write", "streaming.rooms:moderate",
    }  # fmt: skip
    # no secret value anywhere in these two workloads' env, and the mTLS material is a mount.
    for e in [*env_.values(), *srv.values()]:
        assert "PRIVATE KEY" not in str(e.get("value", ""))
    assert any(
        m["name"] == "host-api-tls"
        for c in _hub_api_containers(docs)
        for m in c.get("volumeMounts", [])
    )


def _hub_api_containers(docs: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    (hub,) = [d for d in docs if d["kind"] == "Deployment" and _comp(d) == "hub-api"]
    return hub["spec"]["template"]["spec"]["containers"]


def test_hub_api_target_tracks_a_grpc_port_override(helm: str) -> None:
    docs = _render(helm, "alpha", ("pipeline.svcStreaming.grpcPort=51234",))
    assert _hub_api_env(docs)["STREAMING_RTC_GRPC_TARGET"]["value"].endswith(":51234")
    srv = _env(_container(_streaming_deployment(docs)))
    assert srv["ROOMS_GRPC_PORT"]["value"] == "51234"


@pytest.mark.parametrize("env", _ENABLED_ENVS)
def test_probes_resources_and_rollout(helm: str, env: str) -> None:
    docs = _render(helm, env)
    dep = _streaming_deployment(docs)
    c = _container(dep)
    spec = dep["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}
    for kind, path in (("startupProbe", "/health"), ("livenessProbe", "/health"),
                       ("readinessProbe", "/readyz")):  # fmt: skip
        probe = c[kind]
        assert probe["httpGet"] == {"path": path, "port": "http"}, (kind, probe)
        assert "grpc" not in probe and "exec" not in probe
    sp = c["startupProbe"]
    assert sp["periodSeconds"] * sp["failureThreshold"] >= 300, "first-boot budget"
    assert c["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "4000m", "memory": "4Gi"},
    }
    # singleton: no PDB / HPA may select it (a PDB on one replica blocks every node drain).
    for d in docs:
        if d["kind"] in ("PodDisruptionBudget", "HorizontalPodAutoscaler"):
            blob = json.dumps(d)
            assert "svc-streaming" not in blob, f"{d['kind']} targets svc-streaming"


def test_probe_exception_is_documented_in_the_template() -> None:
    text = _TEMPLATE.read_text()
    assert text.count("# EXCEPTION (approved)") >= 3, "startup/liveness/readiness each carry it"
    assert "grpc.health.v1" in text


@pytest.mark.parametrize("env", _ENABLED_ENVS)
def test_pod_security_baseline(helm: str, env: str) -> None:
    docs = _render(helm, env)
    dep = _streaming_deployment(docs)
    c = _container(dep)
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
    assert "runtimeClassName" not in dep["spec"]["template"]["spec"], "no code executes here"
    pol = [
        d for d in docs
        if d["kind"] == "TracingPolicyNamespaced"
        and d["metadata"]["name"].endswith("-svc-streaming-exec-allowlist")
    ]  # fmt: skip
    tetragon_on = bool(
        [d for d in docs if d["kind"] == "TracingPolicyNamespaced"]
    )  # off on alpha/local overlays by design
    if tetragon_on:
        assert len(pol) == 1
        allowed = pol[0]["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
        assert "/app/svc-streaming" in allowed and not any(
            a.endswith(("/sh", "/bash", "/curl", "/nc")) for a in allowed
        )


def test_room_token_key_is_a_secret_reference_only(helm: str) -> None:
    default = _env(_container(_streaming_deployment(_render(helm, "beta"))))
    assert "WEBRTC_ROOM_TOKEN_SECRET" not in default, "no env (and no value) unless a Secret is named"
    docs = _render(helm, "beta", ("pipeline.svcStreaming.rooms.roomToken.existingSecret=room-token",))
    e = _env(_container(_streaming_deployment(docs)))["WEBRTC_ROOM_TOKEN_SECRET"]
    assert "value" not in e
    assert e["valueFrom"]["secretKeyRef"] == {"name": "room-token", "key": "WEBRTC_ROOM_TOKEN_SECRET"}


@pytest.mark.parametrize("env", list(_ENVS))
def test_helm_lint(helm: str, env: str) -> None:
    result = subprocess.run(  # noqa: S603
        [helm, "lint", str(_CHART_DIR), "--values", str(_CHART_DIR / f"values-{env}.yaml"),
         "--set", "global.deploymentTier=alpha"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-500:]
    assert "0 chart(s) failed" in result.stdout, result.stdout[-500:]


@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
@pytest.mark.parametrize("env", list(_ENVS))
def test_kubeconform(helm: str, env: str) -> None:
    docs = _render(helm, env)
    payload = yaml.safe_dump_all(docs)
    result = subprocess.run(  # noqa: S603, S607
        ["kubeconform", "-ignore-missing-schemas", "-kubernetes-version", "1.30.0", "-summary",
         "-output", "json", "-"],
        input=payload, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-500:]
    summary = json.loads(result.stdout)["summary"]
    assert summary["valid"] + summary["skipped"] == len(docs) and summary["valid"] > 0, summary
    assert summary["invalid"] == 0 and summary["errors"] == 0, summary
    print(f"{env}: kubeconform valid={summary['valid']} skipped={summary['skipped']}")
