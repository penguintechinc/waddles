#!/usr/bin/env python3
"""Static check for dev-only placeholder credentials leaking into a render.

fix/helm-platform-credentials -- a routine `helm upgrade` on alpha once
overwrote the REAL Discord bot token in `waddlebot-secrets` with the
values-alpha.yaml dev placeholder ("REPLACE_ME_discord_bot_token_dev_only"),
killing the live Discord gateway session (4004 Authentication failed). The
structural fix moves externally-issued platform credentials out of any
chart-rendered Secret entirely (see k8s/helm/waddlebot/docs/
PLATFORM_CREDENTIALS.md); this script is the regression gate that catches a
future re-introduction of a placeholder default anywhere in a rendered
manifest, in either a Secret's data/stringData or a workload's plain (non-
secretKeyRef) env var value.

Reads a `helm template` multi-document YAML stream on stdin. Exit code is the
gate: zero objects examined is a hard failure, never a silent pass (see
critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import base64
import re
import sys
from typing import Any

import yaml

PLACEHOLDER_RE = re.compile(r"(REPLACE_ME|CHANGE_ME|changeme|example)", re.IGNORECASE)

# Workload env vars are checked ONLY when their NAME looks credential-shaped --
# unlike a Secret's data/stringData (inherently sensitive by definition), a
# plain Deployment env var can legitimately contain the substring "example"
# in non-secret demo/sample data (e.g. this chart's own
# "waddles.core.example.ping" demo bundle app_id). Scoping to credential-
# shaped names avoids false positives there while still catching a
# credential accidentally hardcoded as a literal `value:` instead of a
# `valueFrom.secretKeyRef`.
CREDENTIAL_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH_KEY|CLIENT_ID|ACCESS_KEY)",
    re.IGNORECASE,
)


def _decode_secret_value(kind: str, key: str, value: Any) -> str | None:
    """Returns the plaintext string for a Secret data/stringData entry."""
    if not isinstance(value, str):
        return None
    if kind == "data":
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except Exception:
            return value
    return value


def check_secret(doc: dict[str, Any], findings: list[str]) -> int:
    """Checks a Secret's `data`/`stringData` for placeholder values. Returns
    the number of key/value pairs examined."""
    name = doc.get("metadata", {}).get("name", "<unnamed>")
    examined = 0
    for field in ("data", "stringData"):
        block = doc.get(field) or {}
        for key, raw in block.items():
            examined += 1
            plaintext = _decode_secret_value(field, key, raw)
            if plaintext:
                match = PLACEHOLDER_RE.search(plaintext)
                if match:
                    findings.append(
                        f"Secret/{name} {field}.{key}: matched placeholder "
                        f"pattern {match.group(0)[:20]!r}"
                    )
    return examined


def check_workload_env(doc: dict[str, Any], findings: list[str]) -> int:
    """Checks every container's plain `env[].value` (never `valueFrom`,
    which is a live secretKeyRef/configMapKeyRef reference, not a literal)
    across all containers/initContainers in Pod-template-bearing kinds.
    Returns the number of env entries examined."""
    kind = doc.get("kind", "")
    if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
        return 0
    name = doc.get("metadata", {}).get("name", "<unnamed>")
    pod_spec = (
        doc.get("spec", {})
        .get("jobTemplate", {})
        .get("spec", {})
        .get("template", {})
        .get("spec")
        if kind == "CronJob"
        else doc.get("spec", {}).get("template", {}).get("spec")
    ) or {}
    examined = 0
    for containers_key in ("containers", "initContainers"):
        for container in pod_spec.get(containers_key, []) or []:
            for env in container.get("env", []) or []:
                if "value" not in env:
                    continue
                env_name = env.get("name", "")
                if not CREDENTIAL_NAME_RE.search(env_name):
                    continue
                examined += 1
                value = env.get("value")
                if isinstance(value, str):
                    match = PLACEHOLDER_RE.search(value)
                    if match:
                        findings.append(
                            f"{kind}/{name} container {container.get('name')} "
                            f"env {env.get('name')}: matched placeholder "
                            f"pattern {match.group(0)[:20]!r}"
                        )
    return examined


def main() -> int:
    stream = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(stream) if d]

    findings: list[str] = []
    objects_examined = 0
    values_examined = 0

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        objects_examined += 1
        kind = doc.get("kind", "")
        if kind == "Secret":
            values_examined += check_secret(doc, findings)
        values_examined += check_workload_env(doc, findings)

    print(f"objects examined: {objects_examined}")
    print(f"values examined: {values_examined}")

    if objects_examined == 0:
        print("FAIL: zero objects examined -- check is not gating anything "
              "(wrong input / empty render)")
        return 1

    if findings:
        print(f"FAIL: {len(findings)} placeholder-looking credential value(s) found:")
        for f in findings:
            print(f"  - {f}")
        return 1

    print("PASS: no placeholder-looking credential values found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
