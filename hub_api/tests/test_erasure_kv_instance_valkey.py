"""Erasure must hit the dedicated valkey-kv instance, never the durable one (real Valkey x2).

# regression: erasure scanned the durable instance, not valkey-kv
Docker absent is a FAILURE (no skip), matching the other real-infra gates.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
import redis

from services import erasure_external as external

IMAGE = (
    "valkey/valkey:8-bookworm"
    "@sha256:fea8b3e67b15729d4bb70589eb03367bab9ad1ee89c876f54327fc7c6e618571"
)
PW = "kv-test-pw"
SUBJECT = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
APP_A = "bundlekv:acme:main:app.a"
APP_B = "bundlekv:acme:_tenant:app.b"


def _free_port() -> int:
    """An unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start(name: str, port: int, extra: list[str]) -> None:
    """Run a throwaway valkey container and wait (<=180s) for it to answer."""
    subprocess.run(  # noqa: S603
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", IMAGE, *extra],  # noqa: S607
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 180
    while True:
        try:
            probe = redis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=1)
            try:
                probe.ping()
                return
            except redis.exceptions.AuthenticationError:
                return  # default user is off: auth error proves it is up
            finally:
                probe.close()
        except redis.exceptions.RedisError:
            assert time.monotonic() < deadline, f"{name} never became ready"
            time.sleep(0.5)


@pytest.fixture
def instances() -> Iterator[tuple[int, int]]:
    """(kv_port, durable_port): kv has an ACL-scoped hub-api user and no default user."""
    tag = uuid.uuid4().hex[:8]
    kv_port, durable_port = _free_port(), _free_port()
    names = [f"waddles-erasure-kv-{tag}", f"waddles-erasure-durable-{tag}"]
    try:
        _start(
            names[0],
            kv_port,
            [
                "valkey-server",
                "--user",
                "default",
                "reset",
                "--user",
                "admin",
                "on",
                f">{PW}",
                "~*",
                "&*",
                "+@all",
                "--user",
                "hub-api",
                "on",
                f">{PW}",
                "~bundlekv:*",
                "resetchannels",
                "-@all",
                "+@connection",
                "+scan",
                "+unlink",
                "+exists",
                "+decrby",
                "+del",
            ],
        )
        _start(names[1], durable_port, [])
        yield kv_port, durable_port
    finally:
        for n in names:
            subprocess.run(["docker", "rm", "-f", n], capture_output=True, check=False)  # noqa: S603, S607


def _seed() -> dict[str, str | int]:
    """Subject + other-user keys (dashed and hex spellings, two apps, counters)."""
    hexed = uuid.UUID(SUBJECT).hex
    return {
        f"{APP_A}:data:seen.{SUBJECT}": "1",
        f"{APP_A}:data:cool.{hexed}": "1",
        f"{APP_A}:data:seen.{OTHER}": "1",
        f"{APP_A}:count": 5,
        f"{APP_B}:data:u.{SUBJECT}": "1",
        f"{APP_B}:data:u.{OTHER}": "1",
        f"{APP_B}:count": 3,
    }


def _scan(client: redis.Redis) -> set[str]:
    """All bundlekv keys visible to `client`."""
    return {k.decode() for k in client.scan_iter(match="bundlekv:*", count=100)}


def test_erasure_targets_valkey_kv_not_the_durable_instance(
    instances: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subject keys vanish from kv (ACL user), counts drop, others stay; the decoy is untouched."""
    kv_port, durable_port = instances
    seed = _seed()
    kv_admin = redis.Redis(host="127.0.0.1", port=kv_port, username="admin", password=PW)
    decoy = redis.Redis(host="127.0.0.1", port=durable_port)
    try:
        for key, value in seed.items():
            kv_admin.set(key, value)
            decoy.set(key, value)
        monkeypatch.setenv("VALKEY_KV_URL", f"redis://127.0.0.1:{kv_port}/0")
        monkeypatch.setenv("VALKEY_KV_USERNAME", "hub-api")
        monkeypatch.setenv("VALKEY_KV_PASSWORD", PW)
        monkeypatch.setenv("VALKEY_URL", f"redis://127.0.0.1:{durable_port}/0")
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{durable_port}/0")

        counts = external.EnvExternalErasure().erase(object_urls=[], user_uuids=[SUBJECT])

        subject_keys = {k for k in seed if SUBJECT in k or uuid.UUID(SUBJECT).hex in k}
        assert len(subject_keys) == 3
        assert counts == {"kv_keys_deleted": 3}
        remaining = _scan(kv_admin)
        assert not remaining & subject_keys
        assert remaining == set(seed) - subject_keys
        assert kv_admin.get(f"{APP_A}:count") == b"3"
        assert kv_admin.get(f"{APP_B}:count") == b"2"
        assert kv_admin.exists(f"{APP_A}:data:seen.{OTHER}") == 1
        assert kv_admin.exists(f"{APP_B}:data:u.{OTHER}") == 1
        assert _scan(decoy) == set(seed)  # durable decoy never connected to
        assert decoy.get(f"{APP_A}:count") == b"5"
    finally:
        kv_admin.close()
        decoy.close()
