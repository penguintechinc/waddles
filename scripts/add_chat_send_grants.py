#!/usr/bin/env python3
"""Declare chat.send:{discord,twitch} grants on reply bundles and bump their patch version.

Idempotent: bundles already declaring both grants are skipped. A bundle is only granted
if its src/ uses the relay host import (least privilege). Versions are bumped consistently
across hub-manifest.yaml, bundle.yaml, pyproject.toml and bundles/core-bundles.yaml.
Usage: add_chat_send_grants.py name [name ...]
"""
import re
import sys
from pathlib import Path

B = Path(__file__).resolve().parent.parent / "bundles"
PLATS = ("discord", "twitch")


def add_perms(text: str, name: str) -> str:
    """Append missing chat.send grants to the top-level permissions list."""
    m = re.search(r"^permissions:\n((?:[ ]+.*\n|\n)*)", text, re.MULTILINE)
    if not m:
        raise SystemExit(f"{name}: no permissions block")
    body = m.group(1).rstrip("\n") + "\n"
    add = ""
    for p in PLATS:
        if f"chat.send:{p}" not in body:
            add += (f'  - id: "chat.send:{p}"\n    justification: "{name} replies to the '
                    f'invoking user/channel via the relay host import (relay.push -> '
                    f'chat.send:{p})"\n')
    return text[: m.start(1)] + body + add + text[m.end(1):]


def bump(v: str) -> str:
    """Patch-bump a semver string."""
    a, b, c = v.split(".")
    return f"{a}.{b}.{int(c) + 1}"


def main() -> None:
    """Process each named python bundle."""
    core = (B / "core-bundles.yaml").read_text()
    for name in sys.argv[1:]:
        d = B / "python" / name
        src = "".join(p.read_text() for p in (d / "src").rglob("*.py"))
        if not re.search(r"\brelay\b", src):
            print(f"SKIP(no relay) {name}")
            continue
        hm = (d / "hub-manifest.yaml").read_text()
        if all(f"chat.send:{p}" in hm for p in PLATS):
            print(f"SKIP(already) {name}")
            continue
        old = re.search(r"^version: (\S+)", hm, re.MULTILINE).group(1)
        new = bump(old)
        app_id = re.search(r"^app_id: (\S+)", hm, re.MULTILINE).group(1)
        hm = add_perms(hm, name)
        hm = re.sub(r"^version: \S+", f"version: {new}", hm, count=1, flags=re.MULTILINE)
        (d / "hub-manifest.yaml").write_text(hm)
        bp = d / "bundle.yaml"
        bt = bp.read_text()
        bt = add_perms(bt, name)
        bt = re.sub(r"^version: \S+", f"version: {new}", bt, count=1, flags=re.MULTILINE)
        bt = bt.replace(f'tested_with: "{old}"', f'tested_with: "{new}"')
        bt = bt.replace(f'min_version: "{old}"', f'min_version: "{new}"')
        bp.write_text(bt)
        pp = d / "pyproject.toml"
        if pp.exists():
            pp.write_text(re.sub(r'^version = "[^"]+"', f'version = "{new}"',
                                 pp.read_text(), count=1, flags=re.MULTILINE))
        pat = re.compile(rf'(- app_id: {re.escape(app_id)}\n\s+version: ")[^"]+(")')
        core, n = pat.subn(rf"\g<1>{new}\g<2>", core)
        print(f"OK {name} {old}->{new}" + ("" if n else "  NOT-IN-core-bundles.yaml"))
    (B / "core-bundles.yaml").write_text(core)


main()
