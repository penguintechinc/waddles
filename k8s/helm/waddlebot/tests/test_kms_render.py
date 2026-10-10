"""Helm-template assertions for the Enterprise external KMS / BYOK subtree (`kms:`).

What must hold, and why each is asserted against REAL `helm template` output rather than the
template source:

1. **Default OFF, baseline untouched.** With `kms.enabled=false` nothing KMS-related renders: no
   ConfigMap, no hub-api `envFrom` hook, no SeaweedFS `kms` section/env/volume, and the bucket
   default stays SSE-S3 (AES256). External KMS is an upsell ON TOP of the baseline, never a
   substitute, so the zero-config path must be byte-for-byte the baseline.
2. **Fail closed.** A KMS feature that is requested but misconfigured -- or requested without the
   master switch -- fails the render with a specific message. It is never silently ignored.
3. **Credentials only by reference.** Provider credentials appear solely as `existingSecret`
   references (`secretRef`/`secretKeyRef`/`secret.secretName`); no credential value is ever
   templated into a manifest.
4. **The SeaweedFS mapping is real.** The `kms` section the chart writes into SeaweedFS's S3 config
   is produced by actually running the container's startup script in a shell, so the JSON asserted
   below is exactly what `weed` would read (provider fields FLAT under the provider object --
   verified against chrislusf/seaweedfs 4.48; a nested "config" object is silently ignored).
5. **The exit ramp keeps data readable.** `mode: drain` returns the bucket default to the baseline
   while the KMS provider stays loaded, so objects already written under KMS remain readable.

Zero rendered documents, or a missing expected document, is a hard failure (critical-rules.md
Verification Integrity) -- never a silent pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"

KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
GCP_KEY = "projects/cust-proj-1/locations/us-east1/keyRings/ring/cryptoKeys/waddles"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)


def _render(
    tmp_path: Path, overrides: dict[str, Any] | None = None
) -> subprocess.CompletedProcess:
    values = tmp_path / "overrides.yaml"
    values.write_text(yaml.safe_dump(overrides or {}))
    return subprocess.run(  # noqa: S603 - fixed argv, test-only
        [
            "helm",
            "template",
            "waddlebot",
            str(_CHART_DIR),
            "--kube-version",
            "1.30.0",
            "--values",
            str(_ALPHA_VALUES),
            "--values",
            str(values),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def render_docs(
    tmp_path: Path, overrides: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """`helm template`, parsed; fails the test on any render error or an empty result."""
    result = _render(tmp_path, overrides)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def render_error(tmp_path: Path, overrides: dict[str, Any]) -> str:
    """The render's stderr; asserts the render FAILED (a misconfiguration must never pass)."""
    result = _render(tmp_path, overrides)
    assert result.returncode != 0, (
        "expected helm template to fail closed, but it rendered"
    )
    return result.stderr


def find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    pytest.fail(
        f"no {kind}/{name} in the render (got: {sorted(d['metadata']['name'] for d in docs if d.get('kind') == kind)[:20]})"
    )


def hub_api_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and "hub-api" in doc["metadata"]["name"]:
            for container in doc["spec"]["template"]["spec"]["containers"]:
                if container["name"] == "hub-api":
                    return container
    pytest.fail("hub-api Deployment/container not found in the render")


def seaweedfs_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return find(docs, "Deployment", "seaweedfs")["spec"]["template"]["spec"]


def seaweedfs_script(docs: list[dict[str, Any]]) -> str:
    return str(seaweedfs_pod(docs)["containers"][0]["command"][-1])


def bucket_init_script(docs: list[dict[str, Any]]) -> str:
    job = find(docs, "Job", "seaweedfs-bucket-init")
    return str(job["spec"]["template"]["spec"]["containers"][0]["command"][-1])


def s3_config(script: str, env: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    """Run the container's startup script (up to `exec weed`) and return the S3 config it writes."""
    etc = tmp_path / "etc-seaweedfs"
    prefix = re.sub(
        r"/etc/seaweedfs(?![-\w])", str(etc), script.split("exec weed", 1)[0]
    )
    base_env = {
        key: "x"
        for key in (
            "HUB_API_S3_ACCESS_KEY_ID",
            "HUB_API_S3_SECRET_ACCESS_KEY",
            "DATAPLANE_S3_ACCESS_KEY_ID",
            "DATAPLANE_S3_SECRET_ACCESS_KEY",
            "BUCKET_INIT_S3_ACCESS_KEY_ID",
            "BUCKET_INIT_S3_SECRET_ACCESS_KEY",
            "STREAMING_S3_ACCESS_KEY_ID",
            "STREAMING_S3_SECRET_ACCESS_KEY",
        )
    }
    subprocess.run(  # noqa: S603 - the rendered script, test-only
        ["/bin/sh", "-c", prefix],
        env={"PATH": os.environ["PATH"], **base_env, **env},
        check=True,
        capture_output=True,
    )
    return json.loads((etc / "s3-identities.json").read_text())


def default_encryption(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The JSON passed to `put-bucket-encryption` by the bucket-init hook."""
    script = bucket_init_script(docs)
    match = re.search(r"--server-side-encryption-configuration '(\{.*?\})'", script)
    assert match, "bucket-init hook has no put-bucket-encryption call"
    return json.loads(match.group(1))["Rules"][0]["ApplyServerSideEncryptionByDefault"]


def env_names(container: dict[str, Any]) -> set[str]:
    return {e["name"] for e in container.get("env", [])}


class TestDefaultOffIsTheBaseline:
    """Zero configuration: nothing KMS renders; SSE-S3 stays the bucket default."""

    def test_nothing_kms_related_renders(self, tmp_path: Path) -> None:
        docs = render_docs(tmp_path)
        assert not [d for d in docs if "kms" in d["metadata"]["name"].lower()]
        refs = json.dumps(hub_api_container(docs)["envFrom"])
        assert "-kms" not in refs
        script = seaweedfs_script(docs)
        assert '"kms"' not in script and "KMS_AWS" not in script
        assert not any(
            n.startswith("KMS_")
            for n in env_names(seaweedfs_pod(docs)["containers"][0])
        )
        assert "seaweedfs-kms" not in json.dumps(seaweedfs_pod(docs))

    def test_the_bucket_default_is_still_sse_s3(self, tmp_path: Path) -> None:
        assert default_encryption(render_docs(tmp_path)) == {"SSEAlgorithm": "AES256"}

    def test_the_identities_config_is_unchanged_json(self, tmp_path: Path) -> None:
        config = s3_config(seaweedfs_script(render_docs(tmp_path)), {}, tmp_path)
        assert set(config) == {"identities"}
        assert len(config["identities"]) == 4


def tenant_keys(**overrides: Any) -> dict[str, Any]:
    base = {"enabled": True, "providers": ["aws_kms", "gcp_kms", "azure_key_vault"]}
    base["azure"] = {"credentialsSecret": "waddles-kms-azure"}
    base["gcp"] = {"credentialsSecret": "waddles-kms-gcp-platform"}
    base["aws"] = {"credentialsSecret": "waddles-kms-aws-platform"}
    base.update(overrides)
    return {"kms": {"enabled": True, "tenantKeys": base}}


class TestTenantKeys:
    """hub-api per-tenant BYOK wiring."""

    def test_configmap_lists_only_identifiers_and_the_enabled_providers(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, tenant_keys(timeoutSeconds=7))
        data = find(docs, "ConfigMap", "waddlebot-kms")["data"]
        assert data == {
            "ENVELOPE_KMS_PROVIDERS": "aws_kms,gcp_kms,azure_key_vault",
            "ENVELOPE_KMS_TIMEOUT_S": "7",
        }

    def test_endpoint_overrides_are_passed_through(self, tmp_path: Path) -> None:
        overrides = tenant_keys(
            aws={
                "kmsEndpointUrl": "https://kms-fips.us-east-1.amazonaws.com",
                "stsEndpointUrl": "https://sts.us-east-1.amazonaws.com",
            },
            gcp={"kmsEndpoint": "https://cloudkms.googleapis.com/v1"},
            azure={
                "authority": "https://login.microsoftonline.com",
                "credentialsSecret": "az",
            },
        )
        overrides["kms"]["tenantKeys"]["aws"]["platformPrincipal"] = (
            "arn:aws:iam::9:role/waddles"
        )
        overrides["kms"]["tenantKeys"]["gcp"]["platformPrincipal"] = (
            "w@p.iam.gserviceaccount.com"
        )
        data = find(render_docs(tmp_path, overrides), "ConfigMap", "waddlebot-kms")[
            "data"
        ]
        assert data["ENVELOPE_AWS_PLATFORM_PRINCIPAL"] == "arn:aws:iam::9:role/waddles"
        assert data["ENVELOPE_GCP_PLATFORM_PRINCIPAL"] == "w@p.iam.gserviceaccount.com"
        assert data["ENVELOPE_AWS_KMS_ENDPOINT_URL"].startswith("https://kms-fips")
        assert data["ENVELOPE_AWS_STS_ENDPOINT_URL"].startswith("https://sts.")
        assert data["ENVELOPE_GCP_KMS_ENDPOINT"].endswith("/v1")
        assert data["ENVELOPE_AZURE_AUTHORITY"].startswith("https://login")

    def test_hub_api_gets_the_config_the_baseline_kek_and_each_credential_secret(
        self, tmp_path: Path
    ) -> None:
        container = hub_api_container(render_docs(tmp_path, tenant_keys()))
        refs = container["envFrom"]
        names = [
            r.get("configMapRef", r.get("secretRef"))["name"]
            for r in refs
            if r.get("configMapRef") or r.get("secretRef")
        ]
        for expected in (
            "waddlebot-kms",
            "waddlebot-tenant-kek",
            "waddles-kms-azure",
            "waddles-kms-gcp-platform",
            "waddles-kms-aws-platform",
        ):
            assert expected in names, expected
        # Credential Secrets are REQUIRED once referenced: a missing one fails the pod loudly.
        for ref in refs:
            secret = ref.get("secretRef")
            if secret and secret["name"].startswith("waddles-kms-"):
                assert not secret.get("optional"), secret

    def test_only_the_selected_providers_credential_secrets_are_referenced(
        self, tmp_path: Path
    ) -> None:
        overrides = tenant_keys(providers=["aws_kms"], aws={}, gcp={}, azure={})
        names = json.dumps(
            hub_api_container(render_docs(tmp_path, overrides))["envFrom"]
        )
        assert "waddles-kms-" not in names
        data = find(render_docs(tmp_path, overrides), "ConfigMap", "waddlebot-kms")[
            "data"
        ]
        assert data["ENVELOPE_KMS_PROVIDERS"] == "aws_kms"

    def test_no_credential_value_can_appear_because_none_is_ever_templated(
        self, tmp_path: Path
    ) -> None:
        result = _render(tmp_path, tenant_keys())
        assert result.returncode == 0
        kms_docs = [
            d
            for d in yaml.safe_load_all(result.stdout)
            if d and "kms" in d["metadata"]["name"].lower()
        ]
        assert {d["kind"] for d in kms_docs} == {
            "ConfigMap"
        }  # never a chart-made Secret


def object_storage(
    mode: str, provider: str = "aws_kms", **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": mode,
        "provider": provider,
        "keyId": KEY_ARN if provider == "aws_kms" else GCP_KEY,
        "aws": {"region": "us-east-1", "credentialsSecret": {"name": "waddles-kms-s3"}},
        "gcp": {
            "projectId": "cust-proj-1",
            "credentialsSecret": {"name": "waddles-kms-gcp"},
        },
    }
    base.update(extra)
    return {"kms": {"enabled": True, "objectStorage": base}}


class TestObjectStorageAws:
    """SeaweedFS SSE-KMS under an AWS KMS key."""

    def test_the_s3_config_carries_a_flat_aws_provider_with_expanded_credentials(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("kms"))
        config = s3_config(
            seaweedfs_script(docs),
            {
                "KMS_AWS_ACCESS_KEY_ID": "AKIAFROMSECRET",
                "KMS_AWS_SECRET_ACCESS_KEY": "s3cr3t/value+x",
            },
            tmp_path,
        )
        provider = config["kms"]["providers"]["customer-managed"]
        assert config["kms"]["default_provider"] == "customer-managed"
        assert provider == {
            "type": "aws",
            "region": "us-east-1",
            "access_key": "AKIAFROMSECRET",
            "secret_key": "s3cr3t/value+x",
        }
        assert (
            "config" not in provider
        )  # nested "config" is silently ignored by SeaweedFS
        assert (
            len(config["identities"]) == 4
        )  # the existing least-privilege identities survive

    def test_credentials_come_only_from_the_existing_secret(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("kms"))
        env = {e["name"]: e for e in seaweedfs_pod(docs)["containers"][0]["env"]}
        ref = env["KMS_AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "waddles-kms-s3", "key": "AWS_ACCESS_KEY_ID"}
        assert (
            "valueFrom" in env["KMS_AWS_SECRET_ACCESS_KEY"]
            and "value" not in env["KMS_AWS_SECRET_ACCESS_KEY"]
        )
        rendered = yaml.safe_dump(docs)
        assert "AKIA" not in rendered

    def test_endpoint_override_is_rendered(self, tmp_path: Path) -> None:
        overrides = object_storage("kms")
        overrides["kms"]["objectStorage"]["aws"]["endpoint"] = (
            "https://vpce.kms.us-east-1.amazonaws.com"
        )
        config = s3_config(
            seaweedfs_script(render_docs(tmp_path, overrides)),
            {"KMS_AWS_ACCESS_KEY_ID": "a", "KMS_AWS_SECRET_ACCESS_KEY": "b"},
            tmp_path,
        )
        assert config["kms"]["providers"]["customer-managed"]["endpoint"].startswith(
            "https://vpce"
        )

    def test_kms_mode_makes_aws_kms_the_bucket_default_on_top_of_the_baseline(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("kms"))
        assert default_encryption(docs) == {
            "SSEAlgorithm": "aws:kms",
            "KMSMasterKeyID": KEY_ARN,
        }
        # The platform KEK is still wired: external KMS never replaces the baseline.
        assert "WEED_S3_SSE_KEK" in env_names(seaweedfs_pod(docs)["containers"][0])

    def test_hub_api_is_told_to_write_under_the_key_and_which_tenant_is_entitled(
        self, tmp_path: Path
    ) -> None:
        overrides = object_storage("kms", entitlementTenant="acme")
        docs = render_docs(tmp_path, overrides)
        assert find(docs, "ConfigMap", "waddlebot-kms")["data"] == {
            "OBJECT_STORAGE_KMS_ENABLED": "true",
            "OBJECT_STORAGE_KMS_KEY_ID": KEY_ARN,
            "OBJECT_STORAGE_KMS_TENANT": "acme",
        }
        assert "waddlebot-kms" in json.dumps(hub_api_container(docs)["envFrom"])


class TestObjectStorageGcp:
    """SeaweedFS SSE-KMS under a Google Cloud KMS key (service-account key file from a Secret)."""

    def test_the_provider_is_flat_with_a_mounted_credentials_file(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("kms", "gcp_kms"))
        config = s3_config(seaweedfs_script(docs), {}, tmp_path)
        assert config["kms"]["providers"]["customer-managed"] == {
            "type": "gcp",
            "project_id": "cust-proj-1",
            "credentials_file": "/etc/seaweedfs-kms/credentials.json",
        }
        assert "access_key" not in json.dumps(config["kms"])

    def test_the_key_file_is_a_read_only_0400_secret_mount_not_an_env_value(
        self, tmp_path: Path
    ) -> None:
        pod = seaweedfs_pod(render_docs(tmp_path, object_storage("kms", "gcp_kms")))
        volume = next(v for v in pod["volumes"] if v["name"] == "seaweedfs-kms-gcp")
        assert volume["secret"]["secretName"] == "waddles-kms-gcp"
        assert volume["secret"]["defaultMode"] == 0o400
        mount = next(
            m
            for m in pod["containers"][0]["volumeMounts"]
            if m["name"] == "seaweedfs-kms-gcp"
        )
        assert mount == {
            "name": "seaweedfs-kms-gcp",
            "mountPath": "/etc/seaweedfs-kms",
            "readOnly": True,
        }
        assert not any(n.startswith("KMS_") for n in env_names(pod["containers"][0]))

    def test_the_bucket_default_uses_the_cryptokey_resource_name(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("kms", "gcp_kms"))
        assert default_encryption(docs) == {
            "SSEAlgorithm": "aws:kms",
            "KMSMasterKeyID": GCP_KEY,
        }


class TestDrainIsTheExitRamp:
    """`drain` returns new writes to the baseline while the provider stays loaded for reads."""

    def test_provider_stays_loaded_but_the_default_returns_to_sse_s3(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("drain"))
        config = s3_config(
            seaweedfs_script(docs),
            {"KMS_AWS_ACCESS_KEY_ID": "a", "KMS_AWS_SECRET_ACCESS_KEY": "b"},
            tmp_path,
        )
        assert (
            config["kms"]["providers"]["customer-managed"]["type"] == "aws"
        )  # reads keep working
        assert default_encryption(docs) == {
            "SSEAlgorithm": "AES256"
        }  # new writes: baseline

    def test_hub_api_goes_back_to_aes256_uploads_without_any_entitlement(
        self, tmp_path: Path
    ) -> None:
        docs = render_docs(tmp_path, object_storage("drain"))
        assert not [d for d in docs if d["metadata"]["name"] == "waddlebot-kms"]
        assert "-kms" not in json.dumps(hub_api_container(docs)["envFrom"])


class TestBothFeaturesTogether:
    def test_tenant_keys_and_object_storage_share_one_configmap(
        self, tmp_path: Path
    ) -> None:
        overrides = tenant_keys(providers=["aws_kms"], gcp={}, azure={})
        overrides["kms"]["objectStorage"] = object_storage("kms")["kms"][
            "objectStorage"
        ]
        data = find(render_docs(tmp_path, overrides), "ConfigMap", "waddlebot-kms")[
            "data"
        ]
        assert {"ENVELOPE_KMS_PROVIDERS", "OBJECT_STORAGE_KMS_ENABLED"} <= set(data)


FAIL_CLOSED = [
    pytest.param(
        {
            "kms": {
                "enabled": False,
                "tenantKeys": {"enabled": True, "providers": ["aws_kms"]},
            }
        },
        "kms.enabled=false",
        id="tenantKeys-without-master",
    ),
    pytest.param(
        {"kms": {"enabled": False, "objectStorage": {"mode": "kms"}}},
        "kms.enabled=false",
        id="objectStorage-without-master",
    ),
    pytest.param(
        {"kms": {"enabled": True}}, "nothing to configure", id="master-with-nothing"
    ),
    pytest.param(
        {"kms": {"enabled": True, "tenantKeys": {"enabled": True, "providers": []}}},
        "requires kms.tenantKeys.providers",
        id="no-providers",
    ),
    pytest.param(
        {
            "kms": {
                "enabled": True,
                "tenantKeys": {"enabled": True, "providers": ["vault"]},
            }
        },
        "valid: aws_kms, gcp_kms, azure_key_vault",
        id="unknown-provider",
    ),
    pytest.param(
        {
            "kms": {
                "enabled": True,
                "tenantKeys": {"enabled": True, "providers": ["azure_key_vault"]},
            }
        },
        "azure.credentialsSecret is empty",
        id="azure-without-credentials",
    ),
    pytest.param(
        {
            "kms": {
                "enabled": True,
                "tenantKeys": {
                    "enabled": True,
                    "providers": ["aws_kms"],
                    "aws": {"kmsEndpointUrl": "http://169.254.169.254"},
                },
            }
        },
        "must be an https:// URL",
        id="insecure-endpoint",
    ),
    pytest.param(
        {
            "kms": {
                "enabled": True,
                "tenantKeys": {
                    "enabled": True,
                    "providers": ["aws_kms"],
                    "aws": {"platformPrincipal": "has a space"},
                },
            }
        },
        "no spaces",
        id="bad-platform-principal",
    ),
    pytest.param(
        {"kms": {"enabled": True, "objectStorage": {"mode": "sideways"}}},
        "invalid",
        id="bad-mode",
    ),
    pytest.param(
        object_storage("kms", "azure_key_vault"),
        "not supported",
        id="object-storage-azure",
    ),
    pytest.param(
        object_storage("kms", "hashicorp"),
        "invalid",
        id="object-storage-unknown-provider",
    ),
    pytest.param(
        object_storage("kms", keyId=""), "keyId is required", id="object-storage-no-key"
    ),
    pytest.param(
        object_storage("kms", keyId="has space"),
        "keyId is required",
        id="object-storage-bad-key",
    ),
    pytest.param(
        object_storage("kms", aws={"region": "", "credentialsSecret": {"name": "s"}}),
        "aws.region is required",
        id="aws-no-region",
    ),
    pytest.param(
        object_storage(
            "kms", aws={"region": "us-east-1", "credentialsSecret": {"name": ""}}
        ),
        "credentialsSecret.name is required",
        id="aws-no-credentials",
    ),
    pytest.param(
        object_storage(
            "kms",
            aws={
                "region": "us-east-1",
                "endpoint": "http://x",
                "credentialsSecret": {"name": "s"},
            },
        ),
        "must be an https:// URL",
        id="aws-insecure-endpoint",
    ),
    pytest.param(
        object_storage(
            "kms", "gcp_kms", gcp={"projectId": "", "credentialsSecret": {"name": "s"}}
        ),
        "projectId is required",
        id="gcp-no-project",
    ),
    pytest.param(
        object_storage(
            "kms",
            "gcp_kms",
            gcp={"projectId": "p-one", "credentialsSecret": {"name": ""}},
        ),
        "credentialsSecret.name is required",
        id="gcp-no-credentials",
    ),
]


@pytest.mark.parametrize(("overrides", "message"), FAIL_CLOSED)
def test_misconfiguration_fails_the_render_loudly(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    assert message in render_error(tmp_path, overrides)


def test_external_kms_is_never_a_substitute_for_the_baseline(tmp_path: Path) -> None:
    overrides = object_storage("kms")
    overrides["infrastructure"] = {"seaweedfs": {"encryption": {"enabled": False}}}
    assert "ON TOP of the platform baseline" in render_error(tmp_path, overrides)


def test_object_storage_kms_requires_seaweedfs(tmp_path: Path) -> None:
    overrides = object_storage("kms")
    overrides["infrastructure"] = {"seaweedfs": {"enabled": False}}
    assert "requires infrastructure.seaweedfs.enabled=true" in render_error(
        tmp_path, overrides
    )
