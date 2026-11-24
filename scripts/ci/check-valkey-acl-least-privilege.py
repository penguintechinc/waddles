#!/usr/bin/env python3
"""Least-privilege gate for the Valkey ACL users the chart renders.

Security finding (fix/valkey-per-service-acls): every Valkey client used to authenticate as the
shared `default` user with `~*`, so any workload could read/write any key (delivery streams,
bundle kv, dedupe/replay keys, platform-unban lists). The chart now renders one ACL user per
service (infrastructure.valkeyAcl.users). This script reads a `helm template` stream on stdin and
fails when the RENDERED ACL (not the values) drifts back towards a wide grant:

1. Every service user: password given as a SHA-256 hash (`#...`), `resetchannels` and no channel
   grant, key patterns `~`/`%R~`/`%W~`/`%RW~` with a specific literal prefix (never `~*`,
   `allkeys`, a leading wildcard or a whole namespace such as `waddles:*` / `waddles:t:*`), commands starting at `-@all`
   and never `+@all`/`allcommands`/`+@dangerous`/`+@admin`/FLUSHALL/FLUSHDB/KEYS/ACL/CONFIG
   (only `+config|get`), never `nopass`. ACL selectors `(...)` may only grant consumer-group
   admin (XGROUP CREATE/DESTROY, XINFO GROUPS/STREAM) and are exempt from the ownership matrix.
2. `admin` (full access) only on valkey-durable (replication masteruser); `probe` = PING only;
   `default` either `reset` (off) or the TRANSITIONAL legacy user, tolerated only while the
   StatefulSet carries `valkey.waddles/legacy-default-user: "true"` (reported as a warning).
3. Key-ownership matrix: for each protected sample key (delivery streams, bundle kv, the
   platform-unban list, ...) only the listed users may hold a pattern that matches it.
4. Bundle executor workloads carry no Valkey credential at all (no VALKEY_*/REDIS_* env, no
   reference to a valkey Secret, no envFrom of the shared app Secret).

5. Client wiring: every container holding a Valkey password authenticates as its OWN user
   (`-valkey-user-<name>` + matching VALKEY_USERNAME / VALKEY_KV_USERNAME), never the shared one.

`--require-clients` fails when zero client credentials are examined; `--require-executor` additionally fails when no executor workload is rendered (CI passes it for the
alpha/beta topologies, which deploy executors). Zero ACL users examined is a FAILURE, never a pass (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import sys
from typing import Any

import yaml

INSTANCES = {"valkey-durable": "durable", "valkey-kv": "kv"}
LEGACY_ANNOTATION = "valkey.waddles/legacy-default-user"

FORBIDDEN_COMMANDS = {
    "+@all",
    "allcommands",
    "+@dangerous",
    "+@admin",
    "+flushall",
    "+flushdb",
    "+keys",
    "+acl",
    "+config",
    "+debug",
    "+shutdown",
    "+replicaof",
    "+slaveof",
    "+module",
    "+migrate",
    "+swapdb",
    "allchannels",
    "nopass",
    "allkeys",
}
TOO_WIDE_PREFIXES = {"", "waddles:", "waddles.", "waddles:t:", "bundle:", "keys:"}
KEY_RE = re.compile(r"^(~|%R~|%W~|%RW~)(.+)$")
# The ONLY commands an ACL selector `(keys commands)` may grant on keys the user does not own:
# consumer-group provisioning (hub-api / the seeder create groups on spine streams).
SELECTOR_COMMANDS = {
    "-@all",
    "+xgroup|create",
    "+xgroup|destroy",
    "+xinfo|groups",
    "+xinfo|stream",
}

# Protected sample keys -> (instance, users allowed to hold ANY pattern matching them).
OWNERSHIP: list[tuple[str, str, set[str]]] = [
    ("durable", "waddles:platform-unban:discord:events", {"svc-ingest", "svc-action"}),
    ("durable", "waddles:platform-unban:discord:seen:abc", {"svc-action"}),
    (
        "durable",
        "waddles:t:acme:c:main:src:discord:g1:events",
        {"svc-ingest", "svc-process", "role-sync-inbound"},
    ),
    (
        "durable",
        "waddles:t:acme:c:main:app:acme.bot:action",
        {"svc-process", "svc-action"},
    ),
    ("durable", "waddles:transport:irc:discord:outbound", {"svc-ingest", "svc-action"}),
    ("durable", "waddles:transport:discord:ack:op1", {"svc-ingest", "svc-action"}),
    ("durable", "outbound:seen:discord:digest", {"svc-ingest"}),
    ("durable", "waddles:webhook:dedup:x", {"svc-ingest"}),
    ("durable", "waddles:dlq:process", {"svc-process"}),
    ("durable", "waddles:dlq:action", {"svc-action"}),
    ("durable", "waddles:usage", {"svc-action", "hub-api"}),
    ("durable", "waddles:jwt:rev:jti:x", {"hub-api"}),
    ("durable", "bundlekv:acme:main:acme.bot:data:k", set()),
    (
        "kv",
        "bundlekv:acme:main:acme.bot:data:k",
        {"svc-process", "svc-action", "hub-api"},
    ),
    ("kv", "bundlekv:acme:main:acme.bot:meta", {"svc-process", "svc-action"}),
    ("kv", "media:job:1:2:x", {"svc-process"}),
]


def _script(doc: dict[str, Any]) -> str:
    """The container's `sh -c` script of a Valkey StatefulSet."""
    cmd = doc["spec"]["template"]["spec"]["containers"][0]["command"]
    return cmd[-1]


def parse_users(script: str) -> dict[str, list[str]]:
    """{user: rule tokens} from every `--user NAME rules...` occurrence in the script."""
    users: dict[str, list[str]] = {}
    for raw in script.splitlines():
        line = raw.strip()
        if "--user " not in line or line.startswith("#"):
            continue
        tokens = shlex.split(line.rstrip("\\"), posix=True)
        i = 0
        while i < len(tokens):
            if tokens[i] == "--user":
                name = tokens[i + 1]
                j = i + 2
                rules: list[str] = []
                while j < len(tokens) and not tokens[j].startswith("--"):
                    rules.append(tokens[j])
                    j += 1
                if name in users:
                    raise SystemExit(f"FAIL: ACL user {name!r} rendered twice")
                users[name] = rules
                i = j
            else:
                i += 1
    return users


def literal_prefix(pattern: str) -> str:
    """Characters before the first glob metacharacter."""
    m = re.search(r"[*?\[]", pattern)
    return pattern if m is None else pattern[: m.start()]


def check_service_user(inst: str, name: str, rules: list[str]) -> list[str]:
    """Violations for one service user's rule tokens."""
    errs: list[str] = []
    where = f"{inst}/{name}"
    if "on" not in rules:
        errs.append(f"{where}: user is not `on` (dead config)")
    if not any(r.startswith("#") for r in rules):
        errs.append(f"{where}: password must be a SHA-256 hash rule (#...)")
    if any(r.startswith(">") for r in rules):
        errs.append(f"{where}: cleartext password rule (>...) in the ACL")
    if "resetchannels" not in rules or any(r.startswith("&") for r in rules):
        errs.append(f"{where}: pub/sub channels must be reset, never granted")
    keys = [r for r in rules if KEY_RE.match(r)]
    if not keys:
        errs.append(f"{where}: no key patterns")
    for k in keys:
        pat = KEY_RE.match(k).group(2)  # type: ignore[union-attr]
        lit = literal_prefix(pat)
        if lit == "" or (lit in TOO_WIDE_PREFIXES and pat[len(lit) :].strip("*") == ""):
            errs.append(
                f"{where}: key pattern {k!r} is a wide grant (no specific literal prefix)"
            )
    for sel in (r for r in rules if r.startswith("(")):
        parts = sel.strip("()").split()
        skeys = [p for p in parts if KEY_RE.match(p)]
        scmds = [p for p in parts if not KEY_RE.match(p)]
        if not skeys or not scmds or scmds[0] != "-@all":
            errs.append(f"{where}: selector {sel!r} must be `(keys... -@all +cmd...)`")
        for p in skeys:
            lit = literal_prefix(KEY_RE.match(p).group(2))  # type: ignore[union-attr]
            if lit == "" or (
                lit in TOO_WIDE_PREFIXES
                and KEY_RE.match(p).group(2)[len(lit) :].strip("*") == ""
            ):  # type: ignore[union-attr]
                errs.append(f"{where}: selector key {p!r} is a wide grant")
        bad = [c for c in scmds if c.lower() not in SELECTOR_COMMANDS]
        if bad:
            errs.append(
                f"{where}: selector {sel!r} grants {bad} -- only consumer-group admin is allowed in a selector"
            )
    cmds = [r for r in rules if r[:1] in "+-" or r in ("allcommands", "nocommands")]
    if not cmds or cmds[0] != "-@all":
        errs.append(f"{where}: command rules must start with -@all, got {cmds[:1]}")
    for r in rules:
        low = r.lower()
        if low in FORBIDDEN_COMMANDS or (
            low.startswith("+config") and low != "+config|get"
        ):
            errs.append(f"{where}: forbidden rule {r!r}")
    return errs


def patterns(rules: list[str]) -> list[str]:
    """Bare glob patterns of a user's key rules."""
    out = []
    for r in rules:
        m = KEY_RE.match(r)
        if m:
            out.append(m.group(2))
        elif r == "allkeys":
            out.append("*")
    return out


def check_executors(docs: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Bundle executor workloads must carry no Valkey credential."""
    errs: list[str] = []
    seen = 0
    for d in docs:
        if d.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        name = d["metadata"]["name"]
        if "executor" not in name:
            continue
        seen += 1
        pod = d["spec"]["template"]["spec"]
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            for e in c.get("env", []) or []:
                ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
                if e["name"].startswith(("VALKEY_", "REDIS_")) or "valkey" in ref.get(
                    "name", ""
                ):
                    errs.append(f"executor {name}: Valkey credential env {e['name']!r}")
            for ef in c.get("envFrom", []) or []:
                sref = (ef.get("secretRef") or {}).get("name", "")
                if sref:
                    errs.append(
                        f"executor {name}: envFrom Secret {sref!r} (may carry Valkey creds)"
                    )
    return seen, errs


def _pod(d: dict[str, Any]) -> dict[str, Any] | None:
    """Pod spec of a workload (CronJob one level deeper), else None."""
    kind = d.get("kind")
    if kind == "CronJob":
        return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return d["spec"]["template"]["spec"]
    return None


def check_client_wiring(
    docs: list[dict[str, Any]], users: dict[str, dict[str, list[str]]]
) -> tuple[int, list[str]]:
    """Every client that holds a Valkey password must hold ITS OWN ACL user's password + username.

    For each instance that has ACL users (and therefore `default` reset): a container reading
    VALKEY_PASSWORD / VALKEY_KV_PASSWORD must take it from `-valkey-user-<name>` with
    VALKEY_USERNAME / VALKEY_KV_USERNAME = <name>, and <name> must be a rendered user on that
    instance. Reading the legacy shared password (`-secrets`/VALKEY_DURABLE_PASSWORD,
    `-valkey-kv`/VALKEY_KV_PASSWORD) is a failure. The Valkey server pods are exempt.
    """
    errs: list[str] = []
    clients = 0
    pairs = (
        ("VALKEY_PASSWORD", "VALKEY_USERNAME", "durable", "VALKEY_DURABLE_PASSWORD"),
        ("VALKEY_KV_PASSWORD", "VALKEY_KV_USERNAME", "kv", "VALKEY_KV_PASSWORD"),
    )
    for d in docs:
        pod = _pod(d)
        if pod is None or d["metadata"]["name"] in INSTANCES:
            continue
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            env = {e["name"]: e for e in c.get("env", []) or []}
            for pw_var, user_var, inst, legacy_key in pairs:
                if inst not in users or pw_var not in env:
                    continue
                ref = (env[pw_var].get("valueFrom") or {}).get("secretKeyRef") or {}
                if ref.get("key") != legacy_key:
                    continue  # e.g. the cache-instance fallback (REDIS_PASSWORD): no ACL users there
                where = f"{d['metadata']['name']}/{c['name']}"
                clients += 1
                name = (env.get(user_var) or {}).get("value", "")
                if "-valkey-user-" not in ref.get("name", ""):
                    errs.append(
                        f"{where}: {pw_var} reads the shared legacy password ({ref.get('name')}/{legacy_key}) -- use its own ACL user"
                    )
                    continue
                if not name or not ref["name"].endswith(f"-valkey-user-{name}"):
                    errs.append(
                        f"{where}: {user_var}={name!r} does not match its password Secret {ref['name']!r}"
                    )
                elif name not in users[inst]:
                    errs.append(
                        f"{where}: {user_var}={name!r} has no ACL user on {inst}"
                    )
    return clients, errs


def main() -> int:
    """Run every check over the helm-template stream on stdin."""
    docs = [d for d in yaml.safe_load_all(sys.stdin) if isinstance(d, dict)]
    errs: list[str] = []
    warns: list[str] = []
    examined = 0
    by_inst: dict[str, dict[str, list[str]]] = {}
    for d in docs:
        if d.get("kind") != "StatefulSet" or d["metadata"]["name"] not in INSTANCES:
            continue
        inst = INSTANCES[d["metadata"]["name"]]
        users = parse_users(_script(d))
        by_inst[inst] = users
        legacy_ok = (d["metadata"].get("annotations") or {}).get(
            LEGACY_ANNOTATION
        ) == "true"
        for name, rules in users.items():
            if name == "admin":
                if inst != "durable":
                    errs.append(
                        f"{inst}/admin: full-access admin is only allowed on valkey-durable"
                    )
                continue
            if name == "probe":
                if [r for r in rules if not r.startswith("#")] != [
                    "reset",
                    "on",
                    "resetchannels",
                    "-@all",
                    "+ping",
                ]:
                    errs.append(
                        f"{inst}/probe: must be exactly `reset on #<hash> resetchannels -@all +ping`, got {rules}"
                    )
                continue
            if name == "default":
                if rules == ["reset"]:
                    continue
                if legacy_ok:
                    warns.append(
                        f"{inst}/default: TRANSITIONAL legacy shared-password user still enabled (valkeyAcl.legacyDefaultUser=true)"
                    )
                    continue
                errs.append(
                    f"{inst}/default: must be `reset` (got {rules}) -- legacy user without the {LEGACY_ANNOTATION} marker"
                )
                continue
            if "executor" in name:
                errs.append(
                    f"{inst}/{name}: bundle executors must not have a Valkey user"
                )
            examined += 1
            errs.extend(check_service_user(inst, name, rules))

    if examined == 0:
        print(
            "FAIL: zero Valkey ACL service users examined (no valkey-durable/valkey-kv StatefulSet or no users)",
            file=sys.stderr,
        )
        return 1

    checked_samples = 0
    for inst, key, allowed in OWNERSHIP:
        users = by_inst.get(inst)
        if users is None:
            continue
        checked_samples += 1
        for name, rules in users.items():
            if name in ("admin", "probe", "default"):
                continue
            hits = [p for p in patterns(rules) if fnmatch.fnmatchcase(key, p)]
            if hits and name not in allowed:
                errs.append(
                    f"{inst}/{name}: pattern(s) {hits} reach protected key {key!r} (allowed: {sorted(allowed) or 'nobody'})"
                )

    n_clients, wiring_errs = check_client_wiring(docs, by_inst)
    errs.extend(wiring_errs)
    if "--require-clients" in sys.argv[1:] and n_clients == 0:
        errs.append("--require-clients: zero Valkey client containers examined")

    n_exec, exec_errs = check_executors(docs)
    errs.extend(exec_errs)
    if n_exec == 0 and "--require-executor" in sys.argv[1:]:
        errs.append(
            "--require-executor: no bundle executor workload in the render -- executor credential check examined nothing"
        )

    for w in warns:
        print(f"WARN: {w}")
    print(
        f"examined: {examined} service ACL users across {len(by_inst)} instance(s), "
        f"{checked_samples} protected-key ownership samples, {n_clients} client credential(s), "
        f"{n_exec} executor workload(s)"
    )
    if errs:
        for e in errs:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("PASS: Valkey ACL least-privilege")
    return 0


if __name__ == "__main__":
    sys.exit(main())
