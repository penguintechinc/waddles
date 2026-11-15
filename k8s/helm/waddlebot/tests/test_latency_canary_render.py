"""Helm-template assertions for the command-latency canary config (alpha testing guild).

The canary is alpha-only and fail-loud at render:

* flag OFF (default): no `CANARY_*` / flag keys in the ConfigMap (nothing is silently half-on);
* enabled with any placeholder id missing: the render FAILS naming the value (never a dead canary);
* enabled outside `global.deploymentTier=alpha`: the render FAILS (hard environment guard);
* a bare (unquoted) 19-digit snowflake -- a lossy YAML float -- is refused;
* fully filled alpha values render the exact `CANARY_*` env both svc-ingest and svc-action read.

A render that yields no ConfigMap is a hard failure; a missing `helm` is a skip locally but a
FAILURE in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = _CHART_DIR / "values-dal2alpha.yaml"
_FILLED = (
    "latencyCanary.enabled=true",
    "latencyCanary.communityId=7",
)
_FILLED_STR = (
    "latencyCanary.guildId=1234567890123456789",
    "latencyCanary.channelId=1234567890123456780",
)


def _helm(values: Path, sets: tuple[str, ...], set_strings: tuple[str, ...] = ()):
    """Render only the shared ConfigMap; returns the completed process."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(values),
        "--show-only",
        "templates/configmap.yaml",
    ]
    for item in sets:
        argv += ["--set", item]
    for item in set_strings:
        argv += ["--set-string", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _data(proc) -> dict[str, str]:
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) == 1, "zero/many ConfigMaps rendered"
    return docs[0]["data"]


def test_flag_off_renders_no_canary_keys() -> None:
    data = _data(_helm(_VALUES, ()))
    assert not [k for k in data if k.startswith("CANARY_")]
    assert "FLAG_WADDLES_COMMAND_LATENCY_CANARY" not in data


def test_enabled_with_placeholders_fails_loud() -> None:
    proc = _helm(_VALUES, ("latencyCanary.enabled=true",))
    assert proc.returncode != 0
    assert "latencyCanary.communityId" in proc.stderr


def test_unquoted_snowflake_is_refused() -> None:
    proc = _helm(
        _VALUES,
        (*_FILLED, "latencyCanary.guildId=1234567890123456789"),
        ("latencyCanary.channelId=45",),
    )
    assert proc.returncode != 0
    assert "latencyCanary.guildId" in proc.stderr


def test_non_alpha_tier_is_refused_even_when_filled() -> None:
    proc = _helm(_CHART_DIR / "values-production.yaml", _FILLED, _FILLED_STR)
    assert proc.returncode != 0
    assert "alpha-only" in proc.stderr


def test_filled_alpha_values_render_the_canary_env() -> None:
    data = _data(_helm(_VALUES, _FILLED, _FILLED_STR))
    assert data["FLAG_WADDLES_COMMAND_LATENCY_CANARY"] == "true"
    assert data["CANARY_ENVIRONMENT"] == "alpha"
    assert data["CANARY_TENANT_ID"] == "1"
    assert data["CANARY_COMMUNITY_ID"] == "7"
    assert data["CANARY_GUILD_ID"] == "1234567890123456789"
    assert data["CANARY_CHANNEL_ID"] == "1234567890123456780"
    assert data["CANARY_COMMAND_TEXT"] == "!ping"
    assert data["CANARY_SINK_MODE"] == "post"
    assert data["CANARY_INTERVAL_SECS"] == "60"


def test_bad_sink_mode_is_refused() -> None:
    proc = _helm(_VALUES, (*_FILLED, "latencyCanary.sinkMode=both"), _FILLED_STR)
    assert proc.returncode != 0
    assert "sinkMode" in proc.stderr
