"""Helm-template assertions for bundle object storage (`bundleStorage`).

bundled (default, every env): bucket created idempotently by the bucket-init hook Job with
default SSE-S3 encryption, a bucket-scoped `bundle-storage` S3 identity, host-stage env from
the chart Secret (secretKeyRef, never literals). external (opt-in): endpoint/bucket/
existingSecret, no bundled bucket/identity, SSE assertion mandatory. Zero rendered documents
or zero matching consumers is a hard failure.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent
_ENVS = (
    "values-alpha.yaml",
    "values-beta.yaml",
    "values-gamma.yaml",
    "values-production.yaml",
    "values-ci.yaml",
    "values-local.yaml",
)
_CONSUMERS = ("svc-process-rust", "svc-action-rust")
_BUCKET = "waddles-bundle-storage"
_EXTERNAL = (
    "bundleStorage.mode=external",
    "bundleStorage.external.endpoint=https://s3.example.com",
    "bundleStorage.external.bucket=ext-bundles",
    "bundleStorage.external.existingSecret=ext-s3-creds",
    "bundleStorage.external.serverSideEncryptionConfirmed=true",
)


def _helm(values, extra=()):
    args = [
        "helm", "template", "w", str(_CHART), "--kube-version", "1.30.0",
        "--values", str(_CHART / values),
        "--set", "global.deploymentTier=alpha",
        "--set", "infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek",
        # The Rust host stages are the consumers; force them on in every env.
        "--set", "pipeline.rustDataPlane.enabled=true",
        "--set", "pipeline.rustDataPlane.svcProcess.enabled=true",
        "--set", "pipeline.rustDataPlane.svcAction.enabled=true",
    ]
    for kv in extra:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603


def _docs(values, extra=()):
    r = _helm(values, extra)
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs
    return docs


def _consumer_envs(docs):
    out = {}
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(_CONSUMERS):
            c = d["spec"]["template"]["spec"]["containers"][0]
            out[d["metadata"]["name"]] = {
                e["name"]: e for e in c["env"]
            }
    assert len(out) == len(_CONSUMERS), f"consumers found: {sorted(out)}"
    return out


def _seaweed_script(docs):
    dep = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("seaweedfs")]
    assert len(dep) == 1
    c = dep[0]["spec"]["template"]["spec"]["containers"][0]
    return (c.get("args") or c["command"])[-1]


def _init_script(docs):
    jobs = [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "seaweedfs-bucket-init"]
    assert len(jobs) == 1
    c = jobs[0]["spec"]["template"]["spec"]["containers"][0]
    return (c.get("args") or c["command"])[-1]


@pytest.mark.parametrize("values", _ENVS)
def test_bundled_is_default_in_every_env(values):
    docs = _docs(values)
    init = _init_script(docs)
    assert f'"{_BUCKET}"' in init
    # SSE-S3 default bucket encryption is applied to every bucket in the loop, incl. ours
    assert "put-bucket-encryption" in init
    script = _seaweed_script(docs)
    m = re.search(r'"name": "bundle-storage".*?"actions": \[(.*?)\]', script, re.S)
    assert m, "bundle-storage identity missing"
    actions = re.findall(r'"([^"]+)"', m.group(1))
    assert sorted(actions) == sorted(f"{a}:{_BUCKET}" for a in ("Read", "Write", "List", "Tagging"))
    assert not any(a in ("Admin",) or ":" not in a for a in actions)
    for name, env in _consumer_envs(docs).items():
        assert env["BUNDLE_STORAGE_BUCKET"]["value"] == _BUCKET, name
        assert env["BUNDLE_STORAGE_ENDPOINT"]["value"].startswith("http://") and "seaweedfs" in env["BUNDLE_STORAGE_ENDPOINT"]["value"], name
        for k in ("BUNDLE_STORAGE_ACCESS_KEY_ID", "BUNDLE_STORAGE_SECRET_ACCESS_KEY"):
            assert "value" not in env[k] and "secretKeyRef" in env[k]["valueFrom"], f"{name}:{k}"


def test_bundled_identity_not_in_any_non_secret_document():
    docs = _docs("values-alpha.yaml")
    secret = [d for d in docs if d["kind"] == "Secret" and "BUNDLE_STORAGE_S3_ACCESS_KEY_ID" in (d.get("stringData") or d.get("data") or {})]
    assert len(secret) == 1
    s = secret[0].get("stringData") or {}
    values = [v for k, v in s.items() if k.startswith("BUNDLE_STORAGE_S3_")]
    assert len(values) == 2 and all(values)
    for d in docs:
        if d is secret[0]:
            continue
        dump = yaml.safe_dump(d)
        assert not any(v in dump for v in values), d["metadata"]["name"]


@pytest.mark.parametrize("values", _ENVS)
def test_external_mode_uses_existing_secret_and_no_bundled_bucket(values):
    docs = _docs(values, _EXTERNAL)
    assert f'"{_BUCKET}"' not in _init_script(docs)
    assert '"name": "bundle-storage"' not in _seaweed_script(docs)
    for name, env in _consumer_envs(docs).items():
        assert env["BUNDLE_STORAGE_ENDPOINT"]["value"] == "https://s3.example.com", name
        assert env["BUNDLE_STORAGE_BUCKET"]["value"] == "ext-bundles", name
        ref = env["BUNDLE_STORAGE_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "ext-s3-creds", "key": "S3_SECRET_ACCESS_KEY"}, name


@pytest.mark.parametrize(
    "override,needle",
    [
        (("bundleStorage.external.endpoint=",), "requires bundleStorage.external.endpoint"),
        (("bundleStorage.external.existingSecret=",), "requires bundleStorage.external.endpoint"),
        (("bundleStorage.external.endpoint=http://plain.example.com",), "must be https://"),
        (("bundleStorage.external.serverSideEncryptionConfirmed=false",), "serverSideEncryptionConfirmed"),
    ],
)
def test_external_mode_fails_closed(override, needle):
    r = _helm("values-alpha.yaml", _EXTERNAL + override)
    assert r.returncode != 0 and needle in r.stderr, r.stderr[-300:]


def test_bad_mode_and_bundled_without_seaweedfs_fail():
    r = _helm("values-alpha.yaml", ("bundleStorage.mode=bogus",))
    assert r.returncode != 0 and "must be bundled or external" in r.stderr
    r = _helm("values-alpha.yaml", ("infrastructure.seaweedfs.enabled=false",))
    assert r.returncode != 0 and "requires infrastructure.seaweedfs.enabled=true" in r.stderr
