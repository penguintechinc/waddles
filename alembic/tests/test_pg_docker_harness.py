"""Self-tests for the shared Postgres harness's fail-loud readiness wait and teardown bookkeeping.

The harness gates ~70 real-Postgres test files; a readiness wait that can hang, or a
timeout that hides why, wastes whole CI runs -- so its failure path is tested directly.
"""

from __future__ import annotations

import os

import pg_docker
import pytest


def test_env_seconds_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset -> default; a positive number -> that value."""
    monkeypatch.delenv("WADDLES_PG_READY_TIMEOUT_S", raising=False)
    assert pg_docker._env_seconds("WADDLES_PG_READY_TIMEOUT_S", 120.0) == 120.0
    monkeypatch.setenv("WADDLES_PG_READY_TIMEOUT_S", "7.5")
    assert pg_docker._env_seconds("WADDLES_PG_READY_TIMEOUT_S", 120.0) == 7.5


@pytest.mark.parametrize("raw", ["abc", "0", "-3"])
def test_env_seconds_rejects_malformed(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """A malformed or non-positive override fails loudly instead of silently defaulting."""
    monkeypatch.setenv("WADDLES_PG_PROBE_TIMEOUT_S", raw)
    with pytest.raises(ValueError, match="WADDLES_PG_PROBE_TIMEOUT_S"):
        pg_docker._env_seconds("WADDLES_PG_PROBE_TIMEOUT_S", 10.0)


def test_container_name_is_per_process() -> None:
    """Two concurrent sessions using the same suffix never share (and rm -f) a container."""
    assert pg_docker.container_name("x") == f"waddles-migtest-x-{os.getpid()}"


@pytest.mark.skipif(not pg_docker.DOCKER_AVAILABLE, reason="docker CLI not installed")
def test_wait_ready_times_out_loudly_with_log_tail() -> None:
    """A never-ready container raises TimeoutError naming probes, last error and the log tail."""
    missing = f"waddles-migtest-does-not-exist-{os.getpid()}"
    with pytest.raises(TimeoutError) as exc:
        pg_docker._wait_ready(missing, "u", "d", timeout_s=2.0, probe_timeout_s=1.0)
    msg = str(exc.value)
    assert missing in msg and "probes" in msg and "last error:" in msg
    assert f"docker logs --tail {pg_docker._LOG_TAIL_LINES}" in msg


def test_remove_container_drops_from_live_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown always clears the atexit worklist entry, even when `docker rm` itself fails."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> object:
        calls.append(argv)
        raise pg_docker.subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(pg_docker.subprocess, "run", fake_run)
    pg_docker._LIVE_CONTAINERS.add("c-test")
    pg_docker._remove_container("c-test")
    assert "c-test" not in pg_docker._LIVE_CONTAINERS
    assert calls == [["docker", "rm", "-f", "c-test"]]
