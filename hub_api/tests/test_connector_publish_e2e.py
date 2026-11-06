"""Opt-in real-path E2E: publish a connector, then the executor's pin loader loads it.

Runs only under ``make test-connector-publish`` (sets ``WADDLES_E2E_CONNECTOR_PUBLISH=1``); a
missing prerequisite there is a FAILURE, never a skip. Uses the pinned ``chrislusf/seaweedfs``
image from the chart, a THROWAWAY in-memory Ed25519 test key (never a real platform key), and
``core/bundle_executor/tests/connector_publish_pin_e2e.rs`` (the production
``BucketComponentSource`` + ``pin_configured``) as the verifier. Scenarios: good publish pins
(denominator 1); a wrong verifier key, tampered signature, tampered component and missing
sidecar are all REFUSED.
"""

# ruff: noqa: S603, S607 -- fixed docker/cargo argv, no untrusted input
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.client import Config as BotoConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import connector_publish_service as svc
from services import object_storage_kms

REPO = Path(__file__).resolve().parents[2]
EXECUTOR = REPO / "core" / "bundle_executor"
FIXTURE = EXECUTOR / "tests" / "fixtures" / "discord_connector.wasm"
CATALOG = REPO / "bundles" / "core-bundles.yaml"
BUCKET = "waddles-bundles"
ACCESS, SECRET = "AKCONNPUBLISHTEST", "connpublish-test-secret-1"
KEY_ID = "e2e-test-key"

pytestmark = pytest.mark.skipif(
    os.environ.get("WADDLES_E2E_CONNECTOR_PUBLISH") != "1",
    reason="opt-in E2E: run `make test-connector-publish` (docker + cargo)",
)


def _b64_pub(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _b64_seed(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode()


def _free_port_block() -> int:
    for _ in range(50):
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port < 50000:
            with closing(socket.socket()) as probe:
                if probe.connect_ex(("127.0.0.1", port + 10000)) != 0:
                    return port
    pytest.fail("no free port block")


@pytest.fixture
def weed(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real pinned SeaweedFS with one admin S3 identity and the bundle bucket."""
    for tool in ("docker", "cargo"):
        if shutil.which(tool) is None:
            pytest.fail(f"E2E prerequisite missing: {tool}")
    values = yaml.safe_load((REPO / "k8s/helm/waddlebot/values.yaml").read_text())
    image = str(values["infrastructure"]["seaweedfs"]["image"])
    assert "@sha256:" in image, "SeaweedFS image must be digest-pinned"
    cfg = tmp_path / "cfg"
    data = tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    (cfg / "s3.json").write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "e2e",
                        "credentials": [{"accessKey": ACCESS, "secretKey": SECRET}],
                        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                    }
                ]
            }
        )
    )
    port = _free_port_block()
    name = f"waddles-e2e-connpub-{os.getpid()}-{port}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "WEED_S3_SSE_KEK=" + "11" * 32,  # SSE-S3 (AES256) needs a KEK; test-only constant
            "-v",
            f"{data}:/data",
            "-v",
            f"{cfg}:/cfg:ro",
            image,
            "server",
            "-dir=/data",
            "-s3",
            f"-s3.port={port}",
            "-s3.config=/cfg/s3.json",
            f"-master.port={port + 1}",
            f"-volume.port={port + 2}",
            "-filer=true",
            f"-filer.port={port + 3}",
            "-ip=127.0.0.1",
            "-volume.max=60",
            "-master.volumeSizeLimitMB=32",
            "-volume.minFreeSpace=64MiB",
            "-s3.port.iceberg=0",
            "-s3.port.lance=0",
        ],
        check=True,
        capture_output=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
    )
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                client.create_bucket(Bucket=BUCKET)
                client.put_object(Bucket=BUCKET, Key="probe", Body=b"ok")
                break
            except Exception:  # noqa: BLE001 - still booting
                if time.monotonic() > deadline:
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "40", name],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    pytest.fail(
                        f"weed never became ready:\n{logs.stdout[-2000:]}{logs.stderr[-2000:]}"
                    )
                time.sleep(1)
        yield endpoint, client
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _pin(endpoint: str, digest: str, pubkeys: dict[str, str], expect: str, err: str = "") -> None:
    """Runs the executor-side pin test once; asserts it saw the expected outcome."""
    env = {
        **os.environ,
        "E2E_BUCKET_ENDPOINT": endpoint,
        "E2E_BUCKET_NAME": BUCKET,
        "E2E_BUCKET_ACCESS_KEY_ID": ACCESS,
        "E2E_BUCKET_SECRET_ACCESS_KEY": SECRET,
        "E2E_SIGNING_PUBLIC_KEYS": json.dumps(pubkeys),
        "E2E_DISCORD_DIGEST": digest,
        "E2E_EXPECT": expect,
        "E2E_EXPECT_ERR_CONTAINS": err,
    }
    proc = subprocess.run(
        [
            "cargo",
            "test",
            "--locked",
            "--features",
            "e2e-main-tier",
            "--test",
            "connector_publish_pin_e2e",
            "--",
            "--nocapture",
        ],
        cwd=EXECUTOR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "1 passed" in out, f"test did not run (denominator): {out[-1500:]}"


async def test_publish_then_executor_pins_and_refuses_tampering(  # type: ignore[no-untyped-def]
    weed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint, s3 = weed
    key = Ed25519PrivateKey.generate()  # throwaway test key, never a platform key
    key_file = tmp_path / "signing.key"
    key_file.write_text(_b64_seed(key))
    key_file.chmod(0o600)
    for name, value in {
        "S3_ENDPOINT_URL": endpoint,
        "S3_ACCESS_KEY_ID": ACCESS,
        "S3_SECRET_ACCESS_KEY": SECRET,
        "BUNDLE_BUCKET_NAME": BUCKET,
        "BUNDLE_SIGNING_KEY_ID": KEY_ID,
        svc.KEY_FILE_ENV: str(key_file),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY", raising=False)
    object_storage_kms.reset_object_storage_sse()

    bundles = tmp_path / "bundles"
    bundles.mkdir()
    shutil.copy(CATALOG, bundles / "core-bundles.yaml")
    shutil.copy(FIXTURE, bundles / "discord-connector.wasm")
    artifacts = svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles, ("discord",))
    assert len(artifacts) == 1

    published = await svc.publish_connectors(artifacts, svc.signer_from_env())
    assert len(published) == 1
    pub = published[0]
    assert pub.digest == svc.digest_of(FIXTURE.read_bytes())
    comp_key, side_key = pub.component_key, pub.sidecar_key
    assert comp_key == f"connectors/discord/{pub.digest}.wasm"
    good = {KEY_ID: _b64_pub(key)}

    # 1. real path: signed objects pin.
    _pin(endpoint, pub.digest, good, "ok")
    # Idempotent re-publish is byte-identical and still pins.
    await svc.publish_connectors(artifacts, svc.signer_from_env())
    _pin(endpoint, pub.digest, good, "ok")

    # 2. verifier trusts a DIFFERENT key -> refused.
    other = {KEY_ID: _b64_pub(Ed25519PrivateKey.generate())}
    _pin(endpoint, pub.digest, other, "fail", "signature")

    # 3. tampered signature -> refused.
    sidecar = json.loads(s3.get_object(Bucket=BUCKET, Key=side_key)["Body"].read())
    orig = sidecar["signature"]
    raw = bytearray(base64.b64decode(orig))
    raw[0] ^= 0xFF
    sidecar["signature"] = base64.b64encode(bytes(raw)).decode()
    s3.put_object(Bucket=BUCKET, Key=side_key, Body=json.dumps(sidecar).encode())
    _pin(endpoint, pub.digest, good, "fail", "signature")
    sidecar["signature"] = orig
    s3.put_object(Bucket=BUCKET, Key=side_key, Body=json.dumps(sidecar).encode())
    _pin(endpoint, pub.digest, good, "ok")

    # 4. tampered component under the pinned digest -> digest mismatch refused.
    s3.put_object(Bucket=BUCKET, Key=comp_key, Body=FIXTURE.read_bytes() + b"tamper")
    _pin(endpoint, pub.digest, good, "fail", "digest")
    s3.put_object(Bucket=BUCKET, Key=comp_key, Body=FIXTURE.read_bytes())

    # 5. sidecar missing -> refused.
    s3.delete_object(Bucket=BUCKET, Key=side_key)
    _pin(endpoint, pub.digest, good, "fail", "HTTP 404")
