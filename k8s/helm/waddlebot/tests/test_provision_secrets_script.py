"""Shell-level tests of files/provision-secrets.sh against a fake kubectl (no cluster touched).

Covers: missing => created; existing => untouched (second run changes nothing); AlreadyExists race
and resourceVersion Conflict race => adopt the winner; externalSecret => skipped; placeholder =>
treated as unset (owned keys only); explicit wins at creation but never over a real value; legacy
import is validated; root marker; no secret value ever in argv/stdout/stderr.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_CHART = Path(__file__).resolve().parents[1]
_SCRIPT = _CHART / "files" / "provision-secrets.sh"
_FAKE = Path(__file__).with_name("fake_kubectl.py")

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not available")


class Env:
    """One isolated fake cluster + inputs dir."""

    def __init__(self, tmp: Path) -> None:
        self.state = tmp / "state"
        self.inputs = tmp / "inputs"
        self.work = tmp / "work"
        for d in (self.state, self.inputs, self.work):
            d.mkdir()
        (self.state / "_calls.log").write_text("")
        bindir = tmp / "bin"
        bindir.mkdir()
        k = bindir / "kubectl"
        k.write_text(f'#!/bin/sh\nexec python3 "{_FAKE}" "$@"\n')
        k.chmod(0o755)
        self.kubectl = str(k)
        self.spec = tmp / "spec.psv"

    def put(self, name: str, data: dict[str, str], annotations: dict[str, str] | None = None) -> None:
        obj = {
            "rv": 1,
            "annotations": annotations or {},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        }
        (self.state / f"{name}.json").write_text(json.dumps(obj))

    def get(self, name: str) -> dict | None:
        p = self.state / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def val(self, name: str, key: str) -> str:
        return base64.b64decode(self.get(name)["data"][key]).decode()

    def calls(self) -> list[list[str]]:
        return [json.loads(x) for x in (self.state / "_calls.log").read_text().splitlines() if x]

    def mutating(self) -> list[list[str]]:
        return [c for c in self.calls() if any(v in c for v in ("create", "patch"))]

    def run(self, spec: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.spec.write_text(spec)
        env = {
            **os.environ,
            "NAMESPACE": "ns",
            "SPEC_FILE": str(self.spec),
            "INPUTS_DIR": str(self.inputs),
            "KUBECTL": self.kubectl,
            "FAKE_KUBE_STATE": str(self.state),
            "WORK_PARENT": str(self.work),
            "OPENSSL_CONF": "/dev/null" if os.environ.get("USE_NULL_OPENSSL_CONF") else os.environ.get("OPENSSL_CONF", ""),
            **(extra_env or {}),
        }
        if not env["OPENSSL_CONF"]:
            env.pop("OPENSSL_CONF")
        return subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, check=False)  # noqa: S603,S607


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


SPEC = """\
secret|db-admin|
key|POSTGRES_PASSWORD|gen|b64url:32|urlsafe;nodefaults;min=16;legacy=main/POSTGRES_PASSWORD
secret|main|
key|REDIS_PASSWORD|gen|b64url:32|urlsafe;min=16
key|CRED|gen|hex:32|hex64
key|RING|gen|ring:k1|min=32
key|S3ID|gen|alnum:16|min=16
key|REDIS_URL|derived|rediss://:%%REDIS_PASSWORD%%@h:1/0|
key|DB_URL|derived|postgresql://u:%%db-admin/POSTGRES_PASSWORD%%@pg/db|
key|STATIC|static|
secret|ext|skip=externalSecret
secret|kek|
key|TENANT_KEK_HEX|gen|hex:32|hex64
marker|TENANT_KEK_HEX|root-hmac-v1
"""


def _all_secret_values(e: Env) -> list[str]:
    vals: list[str] = []
    for p in e.state.glob("*.json"):
        if p.name.startswith("_"):
            continue
        vals += [base64.b64decode(v).decode() for v in json.loads(p.read_text())["data"].values()]
    return [v for v in vals if len(v) >= 16]


def test_missing_is_created_with_correct_shapes_and_no_value_leak(env: Env) -> None:
    r = env.run(SPEC)
    assert r.returncode == 0, r.stderr
    assert "examined=4 created=3 patched=0 unchanged=0 skipped=1" in r.stdout
    pw = env.val("main", "REDIS_PASSWORD")
    assert re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{42}", pw)  # 32 bytes base64url, no leading '-'
    assert re.fullmatch(r"[0-9a-f]{64}", env.val("main", "CRED"))
    assert re.fullmatch(r"k1:[0-9a-f]{64}", env.val("main", "RING"))
    assert re.fullmatch(r"[A-Za-z0-9]{16}", env.val("main", "S3ID"))
    assert env.val("main", "REDIS_URL") == f"rediss://:{pw}@h:1/0"
    assert env.val("main", "DB_URL") == f"postgresql://u:{env.val('db-admin', 'POSTGRES_PASSWORD')}@pg/db"
    assert env.val("main", "STATIC") == ""
    ann = env.get("main")["annotations"]
    assert ann["helm.sh/resource-policy"] == "keep"
    assert ann["argocd.argoproj.io/sync-options"] == "Prune=false"
    assert env.get("ext") is None  # skipped Secret never created
    assert not any("ext" in c for c in env.calls() if "get" in c or "create" in c or "patch" in c)
    blob = r.stdout + r.stderr + json.dumps(env.calls())
    for v in _all_secret_values(env):
        assert v not in blob and base64.b64encode(v.encode()).decode() not in blob


def test_second_run_changes_nothing(env: Env) -> None:
    assert env.run(SPEC).returncode == 0
    before = {p.name: p.read_text() for p in env.state.glob("*.json")}
    (env.state / "_calls.log").write_text("")
    r = env.run(SPEC)
    assert r.returncode == 0, r.stderr
    assert env.mutating() == []
    assert "created=0 patched=0 unchanged=3 skipped=1" in r.stdout
    assert before == {p.name: p.read_text() for p in env.state.glob("*.json")}


def test_existing_real_values_untouched_unowned_keys_kept_placeholder_replaced(env: Env) -> None:
    env.put("main", {"REDIS_PASSWORD": "real-existing-password-value-1", "CRED": "REPLACE_ME_cred", "OWNER_TOKEN": "owner-supplied-secret-xyz", "RING": ""})
    r = env.run(SPEC)
    assert r.returncode == 0, r.stderr
    assert env.val("main", "REDIS_PASSWORD") == "real-existing-password-value-1"
    assert env.val("main", "OWNER_TOKEN") == "owner-supplied-secret-xyz"
    assert re.fullmatch(r"[0-9a-f]{64}", env.val("main", "CRED"))  # placeholder => unset
    assert env.val("main", "RING").startswith("k1:")  # empty => unset
    assert env.val("main", "REDIS_URL") == "rediss://:real-existing-password-value-1@h:1/0"
    assert env.get("main")["annotations"]["helm.sh/resource-policy"] == "keep"  # legacy object gets keep


def test_never_touches_platform_credentials(env: Env) -> None:
    env.put("waddlebot-platform-credentials", {"DISCORD_BOT_TOKEN": "REPLACE_ME"})
    before = env.get("waddlebot-platform-credentials")
    assert env.run(SPEC).returncode == 0
    assert env.get("waddlebot-platform-credentials") == before
    assert not any("waddlebot-platform-credentials" in c for c in env.calls())


def test_create_race_adopts_winner(env: Env) -> None:
    race = {"FAKE_RACE_CREATE": "main", "FAKE_RACE_DATA": json.dumps({"REDIS_PASSWORD": "winner-password-abcdefghijk", "CRED": "a" * 64})}
    r = env.run(SPEC, race)
    assert r.returncode == 0, r.stderr
    assert "race: secret/main was created concurrently" in r.stdout
    assert env.val("main", "REDIS_PASSWORD") == "winner-password-abcdefghijk"
    assert env.val("main", "CRED") == "a" * 64
    assert env.val("main", "REDIS_URL") == "rediss://:winner-password-abcdefghijk@h:1/0"


def test_patch_conflict_retries_and_keeps_concurrent_value(env: Env) -> None:
    env.put("main", {"REDIS_PASSWORD": "real-existing-password-value-1"})
    r = env.run(SPEC, {"FAKE_CONFLICT_ONCE": "main", "FAKE_CONFLICT_DATA": json.dumps({"CRED": "b" * 64})})
    assert r.returncode == 0, r.stderr
    assert "race: secret/main changed concurrently" in r.stdout
    assert env.val("main", "CRED") == "b" * 64  # the concurrent writer's value wins, ours discarded


def test_explicit_wins_at_creation_but_not_over_real_value(env: Env) -> None:
    (env.inputs / "main__REDIS_PASSWORD").write_text("operator-chosen-password-123")
    (env.inputs / "main__STATIC").write_text("pass-through")
    assert env.run(SPEC).returncode == 0
    assert env.val("main", "REDIS_PASSWORD") == "operator-chosen-password-123"
    assert env.val("main", "STATIC") == "pass-through"
    (env.inputs / "main__REDIS_PASSWORD").write_text("a-different-rotated-value-9999")
    r = env.run(SPEC)
    assert r.returncode == 0
    assert env.val("main", "REDIS_PASSWORD") == "operator-chosen-password-123"
    assert "explicit value for secret/main key REDIS_PASSWORD ignored" in r.stdout
    assert "a-different-rotated-value-9999" not in r.stdout + r.stderr


def test_explicit_placeholder_or_weak_value_refused(env: Env) -> None:
    (env.inputs / "db-admin__POSTGRES_PASSWORD").write_text("REPLACE_ME_x")
    r = env.run(SPEC)
    assert r.returncode != 0 and "REPLACE_ME placeholder" in r.stderr
    (env.inputs / "db-admin__POSTGRES_PASSWORD").write_text("changeme-changeme-changeme")
    r = env.run(SPEC)
    assert r.returncode != 0 and "placeholder/default" in r.stderr and "changeme-changeme" not in r.stderr


def test_legacy_import_is_validated_and_preserves_value(env: Env) -> None:
    env.put("main", {"POSTGRES_PASSWORD": "initdb-time-secret-AbC123xyzQ"})
    assert env.run(SPEC).returncode == 0
    assert env.val("db-admin", "POSTGRES_PASSWORD") == "initdb-time-secret-AbC123xyzQ"
    # a planted default must NOT be imported (fails loud instead of silently generating a mismatch)
    shutil.rmtree(env.state)
    env.state.mkdir()
    (env.state / "_calls.log").write_text("")
    env.put("main", {"POSTGRES_PASSWORD": "jwt-secret-change-in-production"})
    r = env.run(SPEC)
    assert r.returncode != 0 and "placeholder/default" in r.stderr


def test_marker_stamped_matches_python_hmac_and_mismatch_fails(env: Env) -> None:
    assert env.run(SPEC).returncode == 0
    kek = env.val("kek", "TENANT_KEK_HEX")
    raw = bytes.fromhex(kek)
    expect = f"v1.{hashlib.sha256(raw).hexdigest()[:16]}.{hmac.new(raw, b'waddles-root-check', hashlib.sha256).hexdigest()}"
    assert env.get("kek")["annotations"]["waddles.penguintech.io/root-marker"] == expect
    # root silently replaced => the release must fail, nothing rewritten
    obj = env.get("kek")
    obj["data"]["TENANT_KEK_HEX"] = base64.b64encode(b"c" * 64).decode()
    (env.state / "kek.json").write_text(json.dumps(obj))
    r = env.run(SPEC)
    assert r.returncode != 0 and "root marker mismatch" in r.stderr


def test_malformed_kek_cannot_be_stamped(env: Env) -> None:
    env.put("kek", {"TENANT_KEK_HEX": "not-hex-and-long-enough-but-wrong-shape-xxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    r = env.run(SPEC)
    assert r.returncode != 0 and "64 lowercase hex" in r.stderr


def test_empty_spec_is_a_failure_not_a_pass(env: Env) -> None:
    r = env.run("# nothing\n")
    assert r.returncode != 0 and "zero Secrets" in r.stderr


def test_script_never_traces_or_puts_values_on_argv() -> None:
    text = _SCRIPT.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"set\s+-[a-z]*x", code) and "xtrace" not in code
    assert "--from-literal" not in code and "stringData" not in code
    assert "set -euo pipefail" in text
