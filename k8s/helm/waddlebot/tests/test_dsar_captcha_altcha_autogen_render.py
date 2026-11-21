"""The public DSAR form's ALTCHA HMAC key is an INTERNAL secret: created if absent, never overwritten.

Proves (render + the real provision script against a fake kubectl): the key is in the provisioning
spec for every tier, hub-api no longer depends on an operator-supplied `waddlebot-dsar-captcha`
Secret for it, the provider defaults to `altcha` when unset, a third-party provider stays an
operator opt-in, an absent key is generated, and an existing key survives an upgrade untouched.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_provision_secrets_script import Env  # type: ignore[import-not-found]

_CHART = Path(__file__).resolve().parents[1]
_ENVS = ["alpha", "beta", "gamma", "production", "dal2alpha", "local"]
_KEY = "DSAR_ALTCHA_HMAC_KEY"

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm CLI not available")


def _render(env: str, sets: list[str] | None = None) -> list[dict[str, Any]]:
    args = ["helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0", "-f", str(_CHART / f"values-{env}.yaml")]
    for kv in sets or []:
        args += ["--set", kv]
    res = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "zero documents rendered"
    return docs


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def _spec(docs: list[dict[str, Any]]) -> str:
    return _one(docs, "ConfigMap", "waddlebot-provision-secrets")["data"]["spec.psv"]


@pytest.mark.parametrize("env", _ENVS)
def test_key_is_generated_into_waddlebot_secrets_in_every_tier(env: str) -> None:
    spec = _spec(_render(env)).splitlines()
    lines = [ln.strip() for ln in spec if ln.strip().startswith(f"key|{_KEY}|")]
    assert len(lines) == 1, lines
    assert lines[0] == f"key|{_KEY}|gen|b64url:48|nodefaults;min=32"
    # the key sits under the waddlebot-secrets block (the Secret hub-api envFroms)
    idx = spec.index(next(ln for ln in spec if ln.strip() == lines[0]))
    owner = [ln.strip() for ln in spec[:idx] if ln.strip().startswith("secret|")][-1]
    assert owner.split("|")[1] == "waddlebot-secrets", owner


def test_hub_api_takes_the_key_from_envfrom_not_an_operator_secret() -> None:
    docs = _render("alpha")
    dep = _one(docs, "Deployment", "waddlebot-hub-api-v3")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    explicit = {e["name"]: e for e in container["env"]}
    assert _KEY not in explicit, "an explicit env would shadow the generated key from envFrom"
    refs = [e["secretRef"]["name"] for e in container["envFrom"] if "secretRef" in e]
    assert "waddlebot-secrets" in refs
    # The third-party secret stays an optional, operator-supplied opt-in.
    third = explicit["DSAR_CAPTCHA_SECRET"]["valueFrom"]["secretKeyRef"]
    assert third["name"] == "waddlebot-dsar-captcha" and third["optional"] is True


def test_provider_defaults_to_altcha_and_third_party_is_opt_in() -> None:
    cfg = _one(_render("alpha"), "ConfigMap", "waddlebot-config")["data"]
    assert cfg["DSAR_CAPTCHA_PROVIDER"] == "altcha"
    opted = _one(_render("alpha", ["config.dsarCaptchaProvider=turnstile"]), "ConfigMap", "waddlebot-config")["data"]
    assert opted["DSAR_CAPTCHA_PROVIDER"] == "turnstile"


def _run_spec(e: Env) -> None:
    res = e.run(_spec(_render("alpha")), {"LABEL_RELEASE": "waddlebot"})
    assert res.returncode == 0, res.stderr


def test_absent_key_is_created(tmp_path: Path) -> None:
    e = Env(tmp_path)
    _run_spec(e)
    generated = e.val("waddlebot-secrets", _KEY)
    assert len(base64.urlsafe_b64decode(generated + "=" * (-len(generated) % 4))) >= 32
    # distinct from the login-ALTCHA key: a leak of one never forges the other
    assert generated != e.val("waddlebot-secrets", "ALTCHA_HMAC_KEY")


def test_existing_key_is_preserved_on_upgrade(tmp_path: Path) -> None:
    e = Env(tmp_path)
    kept = "operator-or-previous-run-key-0123456789abcdefghijklmnop"
    e.put("waddlebot-secrets", {_KEY: kept})
    _run_spec(e)
    assert e.val("waddlebot-secrets", _KEY) == kept
    _run_spec(e)  # second run: still untouched
    assert e.val("waddlebot-secrets", _KEY) == kept
