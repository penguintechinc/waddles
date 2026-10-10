"""Helm-template assertions for the svc-streaming image variant wiring (streaming.gpuVariant).

Three image variants ship from one Dockerfile (core/svc_streaming/Dockerfile.rust):
cpu (software encoders), amd (VA-API via /dev/dri), nvidia (NVENC via the NVIDIA runtime).
The chart selects the variant by tag suffix (`<tag>-<variant>`) and wires each one's pod:

1. cpu    -- image `-cpu`, no GPU resource, runtimeClass, toleration, /dev/dri or render GID;
2. amd    -- image `-amd`, /dev/dri hostPath (type Directory) + render GID as supplementalGroup;
   `deviceResource` swaps the hostPath for a device-plugin limit (PSA-restricted safe);
3. nvidia -- image `-nvidia`, `nvidia` runtimeClass, `nvidia.com/gpu` limit, GPU toleration;
   zeroing runtimeClassName and gpuLimit yields a pod that is schedulable on a CPU node;
4. invalid variant, or amd without supplementalGroups, fails the render loudly.

Every test asserts exactly one svc-streaming Deployment was examined -- zero matches is a
failure, never a pass (critical-rules.md Verification Integrity). A missing helm CLI is a
failure too, not a skip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_AMD_GROUPS = "streaming.amd.supplementalGroups[0]=992"
_GPU_RESOURCE = "nvidia.com/gpu"


def _helm_template(
    extra_set: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Render the chart under alpha values with `extra_set` overrides."""
    if not shutil.which("helm"):
        pytest.fail("helm CLI not available -- the variant render gate cannot run")
    args = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
    ]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    return result


def _svc_streaming(extra_set: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (pod spec, container) of the single svc-streaming Deployment in the render."""
    docs = [d for d in yaml.safe_load_all(_helm_template(extra_set).stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    deployments = [
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"].endswith("-svc-streaming")
    ]
    assert len(deployments) == 1, (
        f"examined {len(deployments)} svc-streaming Deployments, expected 1"
    )
    pod = deployments[0]["spec"]["template"]["spec"]
    containers = [c for c in pod["containers"] if c["name"] == "svc-streaming"]
    assert len(containers) == 1, (
        f"examined {len(containers)} svc-streaming containers, expected 1"
    )
    return pod, containers[0]


def _limits(container: dict[str, Any]) -> dict[str, Any]:
    """Return the container's resource limits (empty dict when none are declared)."""
    return container.get("resources", {}).get("limits", {}) or {}


def _dri_volumes(pod: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every pod volume backed by a hostPath at /dev/dri."""
    return [
        v
        for v in pod.get("volumes", [])
        if v.get("hostPath", {}).get("path") == "/dev/dri"
    ]


class TestCpuVariant:
    """The default cpu image carries no GPU wiring at all."""

    def test_cpu_is_the_default_and_has_the_cpu_tag(self) -> None:
        _, container = _svc_streaming([])
        assert container["image"].endswith(":alpha-cpu"), container["image"]
        assert container["image"].startswith("localhost:32000/waddlebot/svc-streaming:")

    def test_cpu_has_no_gpu_resource_runtime_toleration_or_device(self) -> None:
        pod, container = _svc_streaming(["streaming.gpuVariant=cpu"])
        assert "runtimeClassName" not in pod
        assert "tolerations" not in pod
        assert _GPU_RESOURCE not in _limits(container)
        assert not any(k.startswith("amd.com/") for k in _limits(container))
        assert _dri_volumes(pod) == []
        assert "supplementalGroups" not in pod["securityContext"]
        mounts = [m["mountPath"] for m in container.get("volumeMounts", [])]
        assert "/dev/dri" not in mounts

    def test_cpu_keeps_the_nonroot_uid_contract(self) -> None:
        pod, _ = _svc_streaming(["streaming.gpuVariant=cpu"])
        assert pod["securityContext"].get("runAsNonRoot") is True
        assert _svc_streaming([])[1]["securityContext"]["runAsUser"] == 10001


class TestAmdVariant:
    """The amd image needs /dev/dri and the host render group, nothing NVIDIA-specific."""

    def test_amd_default_uses_the_device_plugin_not_a_hostpath(self) -> None:
        # hostPath alone is denied by the device cgroup for non-privileged pods (EPERM on
        # open), so the default amd wiring must request the device-plugin resource instead.
        pod, container = _svc_streaming(["streaming.gpuVariant=amd", _AMD_GROUPS])
        assert _limits(container).get("amd.com/gpu") == 1
        assert _dri_volumes(pod) == [], "default amd must not hostPath-mount /dev/dri"
        assert "/dev/dri" not in [
            m["mountPath"] for m in container.get("volumeMounts", [])
        ]

    def test_amd_hostpath_fallback_mounts_dri_when_no_device_plugin(self) -> None:
        pod, container = _svc_streaming(
            ["streaming.gpuVariant=amd", _AMD_GROUPS, "streaming.amd.deviceResource="]
        )
        assert "amd.com/gpu" not in _limits(container)
        assert container["image"].endswith(":alpha-amd"), container["image"]
        dri = _dri_volumes(pod)
        assert len(dri) == 1, (
            f"examined {len(dri)} /dev/dri hostPath volumes, expected 1"
        )
        assert dri[0]["hostPath"]["type"] == "Directory"
        mounts = [m for m in container["volumeMounts"] if m["mountPath"] == "/dev/dri"]
        assert len(mounts) == 1, f"examined {len(mounts)} /dev/dri mounts, expected 1"
        assert mounts[0]["name"] == dri[0]["name"]

    def test_amd_passes_the_render_gid_as_a_supplemental_group(self) -> None:
        pod, _ = _svc_streaming(["streaming.gpuVariant=amd", _AMD_GROUPS])
        assert pod["securityContext"]["supplementalGroups"] == [992]
        # The shared pod security context must survive the merge.
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}

    def test_amd_requests_no_nvidia_resource_or_runtime(self) -> None:
        pod, container = _svc_streaming(["streaming.gpuVariant=amd", _AMD_GROUPS])
        assert _GPU_RESOURCE not in _limits(container)
        assert "runtimeClassName" not in pod
        assert "tolerations" not in pod

    def test_amd_device_resource_replaces_the_hostpath(self) -> None:
        pod, container = _svc_streaming(
            [
                "streaming.gpuVariant=amd",
                _AMD_GROUPS,
                "streaming.amd.deviceResource=amd.com/gpu",
            ]
        )
        assert _dri_volumes(pod) == [], (
            "device-plugin mode must not hostPath-mount /dev/dri"
        )
        assert _limits(container).get("amd.com/gpu") == 1
        assert pod["securityContext"]["supplementalGroups"] == [992]

    def test_amd_devdri_false_mounts_nothing(self) -> None:
        pod, container = _svc_streaming(
            [
                "streaming.gpuVariant=amd",
                _AMD_GROUPS,
                "streaming.amd.deviceResource=",
                "streaming.amd.devDri=false",
            ]
        )
        assert _dri_volumes(pod) == []
        assert "/dev/dri" not in [
            m["mountPath"] for m in container.get("volumeMounts", [])
        ]


class TestNvidiaVariant:
    """The nvidia image needs the NVIDIA runtime and a GPU, nothing hostPath-based."""

    def test_nvidia_tag_runtime_class_and_gpu_limit(self) -> None:
        pod, container = _svc_streaming(["streaming.gpuVariant=nvidia"])
        assert container["image"].endswith(":alpha-nvidia"), container["image"]
        assert pod["runtimeClassName"] == "nvidia"
        assert _limits(container).get(_GPU_RESOURCE) == 1

    def test_nvidia_tolerates_the_gpu_taint(self) -> None:
        pod, _ = _svc_streaming(["streaming.gpuVariant=nvidia"])
        matches = [
            t
            for t in pod["tolerations"]
            if t.get("key") == _GPU_RESOURCE and t.get("effect") == "NoSchedule"
        ]
        assert len(matches) == 1, f"examined {len(matches)} GPU tolerations, expected 1"

    def test_nvidia_has_no_dri_mount_or_render_group(self) -> None:
        pod, _ = _svc_streaming(["streaming.gpuVariant=nvidia"])
        assert _dri_volumes(pod) == []
        assert "supplementalGroups" not in pod["securityContext"]

    def test_nvidia_degrades_to_a_cpu_schedulable_pod_when_zeroed(self) -> None:
        pod, container = _svc_streaming(
            [
                "streaming.gpuVariant=nvidia",
                "streaming.nvidia.runtimeClassName=",
                "streaming.nvidia.gpuLimit=0",
            ]
        )
        assert container["image"].endswith(":alpha-nvidia")
        assert "runtimeClassName" not in pod
        assert _GPU_RESOURCE not in _limits(container)


class TestSharedWiring:
    """Behavior common to every variant."""

    @pytest.mark.parametrize("variant", ["cpu", "amd", "nvidia"])
    def test_each_variant_gets_a_distinct_tag_on_the_same_base(
        self, variant: str
    ) -> None:
        extra = [f"streaming.gpuVariant={variant}"]
        if variant == "amd":
            extra.append(_AMD_GROUPS)
        _, container = _svc_streaming(extra)
        assert container["image"].endswith(f":alpha-{variant}"), container["image"]

    def test_encoder_preference_is_passed_through(self) -> None:
        _, container = _svc_streaming(["streaming.encoder=cpu"])
        env = {e["name"]: e.get("value") for e in container["env"] if "value" in e}
        assert env["STREAM_ENCODER"] == "cpu"

    def test_node_selector_is_passed_through(self) -> None:
        pod, _ = _svc_streaming(["streaming.nodeSelector.kubernetes\\.io/arch=amd64"])
        assert pod["nodeSelector"] == {"kubernetes.io/arch": "amd64"}

    def test_unknown_variant_fails_the_render(self) -> None:
        result = _helm_template(["streaming.gpuVariant=intel"], check=False)
        assert result.returncode != 0, "an unknown variant must not render"
        assert "must be one of cpu|amd|nvidia" in result.stderr

    def test_amd_without_render_group_fails_the_render(self) -> None:
        result = _helm_template(["streaming.gpuVariant=amd"], check=False)
        assert result.returncode != 0, "amd without supplementalGroups must not render"
        assert "supplementalGroups must list the host render GID" in result.stderr
