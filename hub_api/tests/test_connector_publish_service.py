"""Unit tests for `connector_publish_service` + `cli/publish_connector_bundles` (storage mocked).

The real-path round trip (SeaweedFS + the executor's pin loader) is
`tests/test_connector_publish_e2e.py` / `make test-connector-publish`.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cli import publish_connector_bundles as cli
from services import connector_publish_service as svc
from services import storage_service
from services.bundle_signing_service import PlatformSigner, build_signing_payload
from services.errors import ApiError

CATALOG = Path(__file__).resolve().parents[2] / "bundles" / "core-bundles.yaml"
WASM = b"\x00asm\x0d\x00\x01\x00" + b"body"


def _seed_b64(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return base64.b64encode(raw).decode()


@pytest.fixture
def bundles(tmp_path: Path) -> Path:
    (tmp_path / "core-bundles.yaml").write_text(CATALOG.read_text())
    for name in ("twitch.wasm", "discord-connector.wasm", "youtube.wasm"):
        (tmp_path / name).write_bytes(WASM)
    return tmp_path


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> tuple[PlatformSigner, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", _seed_b64(key))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "unit-key")
    monkeypatch.delenv(svc.KEY_FILE_ENV, raising=False)
    return svc.signer_from_env(), key


def test_keys_match_the_executor_layout() -> None:
    digest = "sha256:" + "ab" * 32
    assert (
        storage_service.connector_component_key("twitch", digest)
        == f"connectors/twitch/{digest}.wasm"
    )
    assert (
        storage_service.connector_sidecar_key("twitch", digest)
        == f"connectors/twitch/{digest}.json"
    )


def test_load_resolves_all_three_connectors(bundles: Path) -> None:
    got = svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles)
    assert [a.platform for a in got] == ["twitch", "discord", "youtube"]
    assert {a.app_id for a in got} == {f"waddles.core.service.{p}" for p in svc.CONNECTOR_PLATFORMS}


@pytest.mark.parametrize("platforms", [("nope",), ()])
def test_load_rejects_bad_platforms(bundles: Path, platforms: tuple[str, ...]) -> None:
    with pytest.raises(ApiError) as exc:
        svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles, platforms)
    assert exc.value.code == "connector_publish_bad_platform"


def test_load_fails_on_missing_artifact(bundles: Path) -> None:
    (bundles / "youtube.wasm").unlink()
    with pytest.raises(ApiError) as exc:
        svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles)
    assert exc.value.code == "connector_publish_missing_artifact"


def test_load_fails_when_not_in_catalog(tmp_path: Path) -> None:
    (tmp_path / "c.yaml").write_text("bundles: []\n")
    with pytest.raises(ApiError) as exc:
        svc.load_connector_artifacts(tmp_path / "c.yaml", tmp_path, ("twitch",))
    assert exc.value.code == "connector_publish_not_in_catalog"


def test_signer_fails_closed_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BUNDLE_SIGNING_PRIVATE_KEY", "BUNDLE_SIGNING_KEY_ID", svc.KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ApiError) as exc:
        svc.signer_from_env()
    assert exc.value.code == "artifact_signing_key_unavailable"


def test_signer_key_file_must_exist_and_be_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "unit-key")
    monkeypatch.setenv(svc.KEY_FILE_ENV, str(tmp_path / "missing"))
    with pytest.raises(ApiError) as exc:
        svc.signer_from_env()
    assert exc.value.code == "artifact_signing_key_unavailable"

    key_file = tmp_path / "k"
    key_file.write_text(_seed_b64(Ed25519PrivateKey.generate()) + "\n")
    key_file.chmod(0o644)
    monkeypatch.setenv(svc.KEY_FILE_ENV, str(key_file))
    with pytest.raises(ApiError) as exc:
        svc.signer_from_env()
    assert exc.value.code == "artifact_signing_key_invalid"

    key_file.chmod(0o600)
    assert svc.signer_from_env().key_id == "unit-key"


async def test_publish_signs_the_executor_payload_and_writes_both_objects(
    bundles: Path, signer: tuple[PlatformSigner, Ed25519PrivateKey]
) -> None:
    sgn, key = signer
    artifacts = svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles, ("discord",))
    write = AsyncMock(return_value=("ck", "sk"))
    with patch.object(storage_service, "write_connector_objects", write):
        out = await svc.publish_connectors(artifacts, sgn)

    digest = "sha256:" + hashlib.sha256(WASM).hexdigest()
    assert [o.digest for o in out] == [digest]
    platform, got_digest, data, doc = write.call_args.args
    assert (platform, got_digest, data) == ("discord", digest, WASM)
    assert doc["approval_id"] == 0 and doc["key_id"] == "unit-key" and doc["algorithm"] == "ed25519"
    payload = build_signing_payload(
        app_id="waddles.core.service.discord", version=doc["version"], digest=digest, approval_id=0
    )
    key.public_key().verify(base64.b64decode(doc["signature"]), payload)  # raises if wrong


async def test_publish_refuses_non_wasm_and_empty(
    bundles: Path, signer: tuple[PlatformSigner, Ed25519PrivateKey]
) -> None:
    sgn, _ = signer
    (bundles / "twitch.wasm").write_bytes(b"not wasm")
    artifacts = svc.load_connector_artifacts(bundles / "core-bundles.yaml", bundles, ("twitch",))
    with pytest.raises(ApiError) as exc:
        await svc.publish_connectors(artifacts, sgn)
    assert exc.value.code == "connector_publish_not_wasm"
    with pytest.raises(ApiError) as exc:
        await svc.publish_connectors([], sgn)
    assert exc.value.code == "connector_publish_empty"


async def test_write_connector_objects_puts_wasm_then_sorted_sidecar() -> None:
    from unittest.mock import MagicMock

    client = MagicMock()
    with patch("services.storage_service._client", return_value=client):
        keys = await storage_service.write_connector_objects(
            "twitch", "sha256:aa", b"w", {"b": 1, "a": 2}
        )
    assert keys == ("connectors/twitch/sha256:aa.wasm", "connectors/twitch/sha256:aa.json")
    first, second = client.put_object.call_args_list
    assert first.kwargs["Key"].endswith(".wasm") and second.kwargs["Key"].endswith(".json")
    assert second.kwargs["Body"] == b'{"a":2,"b":1}'
    assert second.kwargs["ServerSideEncryption"] == "AES256"


def test_values_snippet_and_cli_output(
    bundles: Path,
    signer: tuple[PlatformSigner, Ed25519PrivateKey],
    capsys: pytest.CaptureFixture[str],
) -> None:
    write = AsyncMock(return_value=("ck", "sk"))
    with patch.object(storage_service, "write_connector_objects", write):
        rc = cli.main(["--bundles-dir", str(bundles), "--platforms", "twitch,youtube"])
    out = capsys.readouterr().out
    digest = "sha256:" + hashlib.sha256(WASM).hexdigest()
    assert rc == 0 and write.call_count == 2
    assert f'      twitch: "{digest}"' in out and f'      youtube: "{digest}"' in out
    assert "published 2/2" in out


def test_cli_fails_loud_without_key(
    bundles: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("BUNDLE_SIGNING_PRIVATE_KEY", "BUNDLE_SIGNING_KEY_ID", svc.KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["--bundles-dir", str(bundles)]) == 1
    assert "artifact_signing_key_unavailable" in capsys.readouterr().err
