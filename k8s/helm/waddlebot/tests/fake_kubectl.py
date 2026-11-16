#!/usr/bin/env python3
"""Fake `kubectl` for files/provision-secrets.sh tests -- Secrets only, state in a directory.

Supports exactly the calls the script makes: `-n NS get secret NAME -o go-template=...`,
`create -f FILE`, `patch secret NAME --type=merge --patch-file FILE`. State lives in
``$FAKE_KUBE_STATE/<name>.json`` ({"rv": int, "annotations": {}, "data": {k: b64}}); every
invocation's argv is appended to ``$FAKE_KUBE_STATE/_calls.log`` so tests can prove no value
ever reaches argv. Failure injection (env):

* ``FAKE_RACE_CREATE=<name>``: before the first `create` of <name>, another "run" creates it with
  ``FAKE_RACE_DATA`` (json {key: plaintext}) and this create fails AlreadyExists.
* ``FAKE_CONFLICT_ONCE=<name>``: before the first `patch` of <name>, a concurrent writer bumps the
  resourceVersion and sets ``FAKE_CONFLICT_DATA`` (json {key: plaintext}); the patch gets Conflict.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_KUBE_STATE"])


def _path(name: str) -> Path:
    return STATE / f"{name}.json"


def _load(name: str) -> dict | None:
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else None


def _save(name: str, obj: dict) -> None:
    _path(name).write_text(json.dumps(obj))


def _once(flag: str) -> bool:
    marker = STATE / f"_once_{flag}"
    if marker.exists():
        return False
    marker.write_text("1")
    return True


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def main() -> int:
    argv = sys.argv[1:]
    with (STATE / "_calls.log").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    if argv[:1] == ["-n"]:
        argv = argv[2:]
    verb = argv[0]
    if verb == "get":
        name = argv[2]
        obj = _load(name)
        if obj is None:
            sys.stderr.write(f'Error from server (NotFound): secrets "{name}" not found\n')
            return 1
        ann = obj.get("annotations", {})
        if ann:  # real kubectl prints "<no value>" for a missing key inside a non-empty map
            ann = {
                k: ann.get(k, "<no value>")
                for k in (
                    "helm.sh/resource-policy",
                    "waddles.penguintech.io/root-marker",
                    "waddlebot.io/cert-identity",
                    "waddlebot.io/cert-sans-sha256",
                )
            }
        out = [
            f"RV {obj['rv']}",
            f"ANN {ann.get('helm.sh/resource-policy', '')}",
            f"MARK {ann.get('waddles.penguintech.io/root-marker', '')}",
            f"CID {ann.get('waddlebot.io/cert-identity', '')}",
            f"CSAN {ann.get('waddlebot.io/cert-sans-sha256', '')}",
        ]
        out += [f"K {k} {v}" for k, v in obj.get("data", {}).items()]
        sys.stdout.write("\n".join(out) + "\n")
        return 0
    if verb == "create":
        manifest = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
        name = manifest["metadata"]["name"]
        if os.environ.get("FAKE_RACE_CREATE") == name and _once("race_" + name):
            data = {k: _b64(v) for k, v in json.loads(os.environ["FAKE_RACE_DATA"]).items()}
            _save(name, {"rv": 1, "annotations": {}, "data": data})
        if _load(name) is not None:
            sys.stderr.write(f'Error from server (AlreadyExists): secrets "{name}" already exists\n')
            return 1
        _save(
            name,
            {
                "rv": 1,
                "annotations": manifest["metadata"].get("annotations", {}),
                "labels": manifest["metadata"].get("labels", {}),
                "data": manifest.get("data", {}),
            },
        )
        return 0
    if verb == "patch":
        name = argv[2]
        patch = json.loads(Path(argv[argv.index("--patch-file") + 1]).read_text())
        obj = _load(name)
        if obj is None:
            sys.stderr.write(f'Error from server (NotFound): secrets "{name}" not found\n')
            return 1
        if os.environ.get("FAKE_CONFLICT_ONCE") == name and _once("conflict_" + name):
            for k, v in json.loads(os.environ["FAKE_CONFLICT_DATA"]).items():
                obj["data"][k] = _b64(v)
            obj["rv"] += 1
            _save(name, obj)
        rv = patch.get("metadata", {}).get("resourceVersion")
        if rv is not None and str(rv) != str(obj["rv"]):
            sys.stderr.write(
                f'Error from server (Conflict): Operation cannot be fulfilled on secrets "{name}": '
                "the object has been modified; please apply your changes to the latest version\n"
            )
            return 1
        obj.setdefault("annotations", {}).update(patch.get("metadata", {}).get("annotations", {}))
        obj.setdefault("data", {}).update(patch.get("data", {}))
        obj["rv"] += 1
        _save(name, obj)
        return 0
    sys.stderr.write(f"fake kubectl: unsupported verb {verb}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
