"""Opt-in E2E: the chart's SeaweedFS KMS mapping against the REAL pinned `weed` image.

Runs only under ``make test-seaweedfs-sse-kms`` (which sets ``WADDLES_E2E_SEAWEEDFS=1``); a plain
``pytest`` run skips it with a clear reason, and under the make target a missing prerequisite is a
FAILURE, never a skip. It needs docker, helm and the pinned ``chrislusf/seaweedfs`` image.

What it proves that no unit test can, using the chart's own rendered output end to end:

1. ``helm template`` (``kms.objectStorage.mode=kms``) -> the container's startup script writes the
   S3 config -> the real ``weed server`` loads the ``kms`` section (provider fields FLAT -- verified
   here, not assumed) and talks to a mock AWS KMS at a socket.
2. Per-object SSE-KMS (``aws:kms`` + key id, exactly what hub-api's ``put_object`` sends)
   round-trips,
   and every object costs a real ``GenerateDataKey``/``Decrypt`` against the KMS.
3. The bucket default JSON the chart's bucket-init hook passes to ``put-bucket-encryption`` is
   accepted, and writers that send NO SSE header (the Rust services) land under the KMS key.
4. The SSE-S3 baseline coexists with SSE-KMS in the same bucket.
5. ``mode: drain`` -- bucket default back to AES256, provider still loaded -- keeps objects that
   were written under KMS readable (the object-storage exit ramp).
6. A KMS outage/revocation makes new SSE-KMS writes FAIL loudly (no silent AES256 fallback).
   (Reads of already-cached data keys may keep working until SeaweedFS' key cache turns over; that
   is documented in docs/guides/external-kms-byok.md rather than asserted here.)

The mock KMS endpoint is injected into the generated config by this test only because the chart
(correctly) refuses a non-https ``endpoint`` for operators.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from tests.envelope.kms_mocks import MockAwsKms

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "k8s" / "helm" / "waddlebot"
KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
REQUIRED = os.environ.get("WADDLES_E2E_SEAWEEDFS") == "1"

pytestmark = pytest.mark.skipif(
    not REQUIRED,
    reason="opt-in E2E: run `make test-seaweedfs-sse-kms` (needs docker, helm, the weed image)",
)


def _need(condition: bool, message: str) -> None:
    """Under the make target a missing prerequisite FAILS; it is never a silent skip."""
    if not condition:
        pytest.fail(f"E2E prerequisite missing: {message}")


def _free_port_block() -> int:
    """A base port P such that P and P+10000 (weed's gRPC offset) are both free."""
    for _ in range(50):
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port < 50000:
            with closing(socket.socket()) as probe:
                if probe.connect_ex(("127.0.0.1", port + 10000)) != 0:
                    return port
    pytest.fail("could not find a free port block")


def _render(tmp_path: Path, mode: str) -> list[dict]:
    overrides = tmp_path / f"values-{mode}.yaml"
    overrides.write_text(
        yaml.safe_dump(
            {
                "kms": {
                    "enabled": True,
                    "objectStorage": {
                        "mode": mode,
                        "provider": "aws_kms",
                        "keyId": KEY_ARN,
                        "aws": {"region": "us-east-1", "credentialsSecret": {"name": "s"}},
                    },
                }
            }
        )
    )
    result = subprocess.run(
        [
            "helm",
            "template",
            "waddlebot",
            str(CHART),
            "--kube-version",
            "1.30.0",
            "--values",
            str(CHART / "values-alpha.yaml"),
            "--values",
            str(overrides),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _script(docs: list[dict], kind: str, name: str) -> str:
    doc = next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)
    spec = doc["spec"]["template"]["spec"]
    return str(spec["containers"][0]["command"][-1])


def _s3_config_from_chart(docs: list[dict], work: Path, kms_endpoint: str) -> Path:
    """Run the chart's real startup script to produce the S3 config, then point KMS at the mock."""
    etc = work / "etc"
    script = re.sub(r"/etc/seaweedfs(?![-\w])", str(etc), _script(docs, "Deployment", "seaweedfs"))
    prefix = script.split("exec weed", 1)[0]
    env = {
        "PATH": os.environ["PATH"],
        "KMS_AWS_ACCESS_KEY_ID": "AKIAPLATFORM",
        "KMS_AWS_SECRET_ACCESS_KEY": "platform-secret",
    }
    for name in ("HUB_API", "DATAPLANE", "BUCKET_INIT", "STREAMING"):
        env[f"{name}_S3_ACCESS_KEY_ID"] = f"AK{name}"
        env[f"{name}_S3_SECRET_ACCESS_KEY"] = f"SK{name}-secret-1"
    subprocess.run(["/bin/sh", "-c", prefix], env=env, check=True, capture_output=True)
    path = etc / "s3-identities.json"
    config = json.loads(path.read_text())
    config["kms"]["providers"]["customer-managed"]["endpoint"] = kms_endpoint
    path.write_text(json.dumps(config))
    return path


def _bucket_default(docs: list[dict]) -> dict:
    script = _script(docs, "Job", "seaweedfs-bucket-init")
    match = re.search(r"--server-side-encryption-configuration '(\{.*?\})'", script)
    assert match, "bucket-init hook has no put-bucket-encryption call"
    return json.loads(match.group(1))


def _values_image() -> str:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    return str(values["infrastructure"]["seaweedfs"]["image"])


class Weed:
    """A real SeaweedFS server (pinned image) with its S3 gateway on a loopback port."""

    def __init__(self, config: Path, work: Path, port: int) -> None:
        self.port = port
        self.name = f"waddles-e2e-weed-{os.getpid()}-{port}"
        data = work / "data"
        data.mkdir(exist_ok=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                "--network",
                "host",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                "WEED_S3_SSE_KEK=" + "11" * 32,
                "-v",
                f"{data}:/data",
                "-v",
                f"{config.parent}:/cfg:ro",
                _values_image(),
                "server",
                "-dir=/data",
                "-s3",
                f"-s3.port={port}",
                "-s3.config=/cfg/s3-identities.json",
                f"-master.port={port + 1}",
                f"-volume.port={port + 2}",
                "-filer=true",
                f"-filer.port={port + 3}",
                "-ip=127.0.0.1",
                "-volume.max=60",
                "-master.volumeSizeLimitMB=32",  # many small volumes: one set per bucket
                # CI/dev disks are often >99% full; the default 1% free-space floor would mark
                # every volume read-only and make this E2E fail for reasons unrelated to KMS.
                "-volume.minFreeSpace=64MiB",
                "-s3.port.iceberg=0",
                "-s3.port.lance=0",
            ],
            check=True,
            capture_output=True,
        )

    def client(self, access: str, secret: str):  # type: ignore[no-untyped-def]
        return boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{self.port}",
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
        )

    def wait_ready(self, client) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                # The S3 gateway answers before the volume server has registered with the master;
                # a real write is the only honest readiness signal ("no volume server capacity").
                client.list_buckets()
                client.create_bucket(Bucket="readiness-probe")
                client.put_object(Bucket="readiness-probe", Key="probe", Body=b"ok")
                return
            except Exception:  # noqa: BLE001 - still booting
                time.sleep(1)
        logs = subprocess.run(
            ["docker", "logs", "--tail", "60", self.name],
            capture_output=True,
            text=True,
            check=False,
        )
        pytest.fail(f"weed never became ready:\n{logs.stdout[-2500:]}{logs.stderr[-2500:]}")

    def logs(self) -> str:
        """The server's recent log (attached to a failure so a red run explains itself)."""
        out = subprocess.run(
            ["docker", "logs", "--tail", "80", self.name],
            capture_output=True,
            text=True,
            check=False,
        )
        return (out.stdout + out.stderr)[-6000:]

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False)


@pytest.fixture
def mock_kms() -> Iterator[MockAwsKms]:
    mock = MockAwsKms()
    mock.add_key(KEY_ARN)
    yield mock
    mock.stop()


def _boot(tmp_path: Path, mode: str, mock: MockAwsKms) -> tuple[Weed, list[dict]]:
    _need(shutil.which("docker") is not None, "docker")
    _need(shutil.which("helm") is not None, "helm")
    image = _values_image()
    _need(
        subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        ).returncode
        == 0,
        f"{image} (docker pull it once; the make target never pulls implicitly)",
    )
    docs = _render(tmp_path, mode)
    config = _s3_config_from_chart(docs, tmp_path, mock.url)
    weed = Weed(config, tmp_path, _free_port_block())
    admin = weed.client("AKBUCKET_INIT", "SKBUCKET_INIT-secret-1")
    weed.wait_ready(admin)
    return weed, docs


def test_chart_rendered_kms_mapping_works_against_the_real_seaweedfs(
    tmp_path: Path, mock_kms: MockAwsKms
) -> None:
    weed, docs = _boot(tmp_path, "kms", mock_kms)
    try:
        admin = weed.client("AKBUCKET_INIT", "SKBUCKET_INIT-secret-1")
        hub = weed.client("AKHUB_API", "SKHUB_API-secret-1")  # hub-api's least-privilege identity
        bucket = yaml.safe_load((CHART / "values.yaml").read_text())["infrastructure"]["seaweedfs"][
            "bucketName"
        ]
        admin.create_bucket(Bucket=bucket)

        # (3) the chart's own bucket-init JSON is accepted, then no-header writers land under KMS.
        default = _bucket_default(docs)
        admin.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration=default)
        assert (
            admin.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"][
                "Rules"
            ][0]["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"]
            == KEY_ARN
        )
        hub.put_object(Bucket=bucket, Key="rust-writer.bin", Body=b"no sse header")
        head = hub.head_object(Bucket=bucket, Key="rust-writer.bin")
        assert (head["ServerSideEncryption"], head["SSEKMSKeyId"]) == ("aws:kms", KEY_ARN)
        assert (
            hub.get_object(Bucket=bucket, Key="rust-writer.bin")["Body"].read() == b"no sse header"
        )

        # (2) exactly the headers hub-api's put_object sends in KMS mode.
        hub.put_object(
            Bucket=bucket,
            Key="hub-api.bin",
            Body=b"hub bytes",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=KEY_ARN,
        )
        assert hub.get_object(Bucket=bucket, Key="hub-api.bin")["Body"].read() == b"hub bytes"
        assert mock_kms.kms_calls("GenerateDataKey"), "writes never reached the customer KMS"
        assert mock_kms.kms_calls("Decrypt"), "reads never reached the customer KMS"
        generated = mock_kms.kms_calls("GenerateDataKey")[0].json()
        assert generated["KeyId"] == KEY_ARN

        # (4) baseline SSE-S3 coexists in the same bucket.
        hub.put_object(
            Bucket=bucket, Key="baseline.bin", Body=b"sse-s3", ServerSideEncryption="AES256"
        )
        assert (
            hub.head_object(Bucket=bucket, Key="baseline.bin")["ServerSideEncryption"] == "AES256"
        )

        # (6) a KMS outage fails NEW SSE-KMS writes loudly -- no silent AES256 fallback.
        mock_kms.behavior.deny = True
        with pytest.raises(ClientError):
            hub.put_object(
                Bucket=bucket,
                Key="denied.bin",
                Body=b"x",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=KEY_ARN,
            )
        with pytest.raises(ClientError):
            hub.head_object(Bucket=bucket, Key="denied.bin")  # nothing was written at all
        mock_kms.behavior.deny = False
    except Exception:
        print("---- weed log (tail) ----\n" + weed.logs())  # noqa: T201 - shown on failure only
        raise
    finally:
        weed.stop()


def test_drain_mode_returns_the_default_to_the_baseline_and_keeps_old_objects_readable(
    tmp_path: Path, mock_kms: MockAwsKms
) -> None:
    weed, docs = _boot(tmp_path, "drain", mock_kms)
    try:
        admin = weed.client("AKBUCKET_INIT", "SKBUCKET_INIT-secret-1")
        hub = weed.client("AKHUB_API", "SKHUB_API-secret-1")
        bucket = yaml.safe_load((CHART / "values.yaml").read_text())["infrastructure"]["seaweedfs"][
            "bucketName"
        ]
        admin.create_bucket(Bucket=bucket)
        # An object written earlier under KMS (while the mode was `kms`).
        hub.put_object(
            Bucket=bucket,
            Key="old.bin",
            Body=b"written under kms",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=KEY_ARN,
        )
        # `drain`: the chart now sets the AES256 default...
        default = _bucket_default(docs)
        assert default["Rules"][0]["ApplyServerSideEncryptionByDefault"] == {
            "SSEAlgorithm": "AES256"
        }
        admin.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration=default)
        hub.put_object(Bucket=bucket, Key="new.bin", Body=b"after drain")
        assert hub.head_object(Bucket=bucket, Key="new.bin")["ServerSideEncryption"] == "AES256"
        # ...while the provider is still loaded, so the old object remains readable.
        assert hub.get_object(Bucket=bucket, Key="old.bin")["Body"].read() == b"written under kms"

        # The documented drain procedure: re-encrypt each KMS object in place under the baseline
        # (a self-copy with REPLACE + an explicit AES256), after which it no longer needs the KMS.
        hub.copy_object(
            Bucket=bucket,
            Key="old.bin",
            CopySource={"Bucket": bucket, "Key": "old.bin"},
            MetadataDirective="REPLACE",
            ServerSideEncryption="AES256",
        )
        head = hub.head_object(Bucket=bucket, Key="old.bin")
        assert head["ServerSideEncryption"] == "AES256" and "SSEKMSKeyId" not in head
        mock_kms.behavior.deny = True  # the customer key is now irrelevant to this object
        assert hub.get_object(Bucket=bucket, Key="old.bin")["Body"].read() == b"written under kms"
    except Exception:
        print("---- weed log (tail) ----\n" + weed.logs())  # noqa: T201 - shown on failure only
        raise
    finally:
        weed.stop()
