"""Per-service Valkey ACL users (infrastructure.valkeyAcl): render, CI-check and real-path tests.

Security finding: every Valkey client authenticated as the shared `default` user with `~*`. The
chart now renders one ACL user per service, scoped to its key families. These tests pin:

* render: every configured user is a `--user` rule on its instance, its password comes from its
  OWN provisioned Secret by secretKeyRef (never envFrom), the provision hook generates distinct
  strong passwords create-if-absent, the legacy `default` user follows legacyDefaultUser, and
  bundle executors carry no Valkey credential;
* scripts/ci/check-valkey-acl-least-privilege.py passes on every env and FAILS on a planted wide
  grant / a planted cross-service grant (a gate that cannot fail is not a gate);
* real path: a real Valkey container started with the EXACT rendered ACL rules enforces them --
  svc-process cannot LPUSH the platform-unban list, nothing but the kv host services can touch
  `bundlekv:*`, unauthenticated / unknown (executor) users are refused.

Docker is REQUIRED for the real-path tests; a missing docker is a failure, never a skip.
Zero rendered users / zero examined items is a hard failure throughout.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from provision_harness import materialize  # type: ignore[import-not-found]

_CHART = Path(__file__).resolve().parent.parent
_REPO = _CHART.parent.parent.parent
_CHECK = _REPO / "scripts" / "ci" / "check-valkey-acl-least-privilege.py"
_ENVS = (
    "values-alpha.yaml",
    "values-beta.yaml",
    "values-gamma.yaml",
    "values-production.yaml",
    "values-ci.yaml",
    "values-dal2alpha.yaml",
)
_INSTANCES = {"valkey-durable": "durable", "valkey-kv": "kv"}
_PW_KEY = {"durable": "VALKEY_DURABLE_PASSWORD", "kv": "VALKEY_KV_PASSWORD"}


def _load_check() -> Any:
    """Import the CI check script as a module (its parser is reused by the real-path tests)."""
    spec = importlib.util.spec_from_file_location("valkey_acl_check", _CHECK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = _load_check()


def _helm(values: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """`helm template` the chart with one env file (+ --set overrides) at the alpha secret tier."""
    args = [
        "helm",
        "template",
        "w",
        str(_CHART),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART / values),
        "--set",
        "global.deploymentTier=alpha",
    ]
    for kv in extra:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _docs(
    values: str = "values-alpha.yaml", extra: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Rendered documents; render failure or an empty render fails the test."""
    r = _helm(values, extra)
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs
    return docs


def _values() -> dict[str, Any]:
    """The chart's base values.yaml."""
    return yaml.safe_load((_CHART / "values.yaml").read_text())


def _configured(inst: str) -> dict[str, dict[str, Any]]:
    """{user: instance config} for every user configured on `inst` in values.yaml."""
    users = _values()["infrastructure"]["valkeyAcl"]["users"]
    out = {n: u[inst] for n, u in users.items() if u.get(inst)}
    assert out, f"no users configured for {inst}"
    return out


def _sts(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Exactly one StatefulSet named `name`."""
    found = [
        d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == name
    ]
    assert len(found) == 1, f"expected exactly one StatefulSet/{name}, got {len(found)}"
    return found[0]


def _run_check(stream: str, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the CI check over a rendered stream."""
    return subprocess.run(
        ["python3", str(_CHECK), *flags],
        input=stream,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("values", _ENVS)
def test_every_configured_user_is_rendered_with_its_own_secret(values: str) -> None:
    """Each user: one `--user` rule on its instance + a secretKeyRef to its own Secret's key."""
    docs = _docs(values)
    examined = 0
    for sts_name, inst in _INSTANCES.items():
        sts = _sts(docs, sts_name)
        c = sts["spec"]["template"]["spec"]["containers"][0]
        users = CHECK.parse_users(c["command"][-1])
        env = {e["name"]: e for e in c["env"]}
        for name, cfg in _configured(inst).items():
            assert name in users, f"{inst}: user {name} not rendered"
            rules = users[name]
            assert rules[:2] == ["reset", "on"] and rules[2] == "#$H", rules
            for k in cfg["keys"]:
                assert k in rules, f"{inst}/{name}: key rule {k} missing"
            ref = env[f"VALKEY_ACL_PW_{name.upper().replace('-', '_')}"]["valueFrom"][
                "secretKeyRef"
            ]
            assert ref == {
                "name": f"w-waddlebot-valkey-user-{name}",
                "key": _PW_KEY[inst],
            }
            examined += 1
        assert users["probe"] == [
            "reset",
            "on",
            "#$PROBE_HASH",
            "resetchannels",
            "-@all",
            "+ping",
        ]
    assert examined >= 10


def test_executor_has_no_user_and_service_secrets_are_never_env_from() -> None:
    """No executor ACL user; per-service Secrets are only ever referenced key-by-key."""
    docs = _docs()
    for sts_name in _INSTANCES:
        users = CHECK.parse_users(
            _sts(docs, sts_name)["spec"]["template"]["spec"]["containers"][0][
                "command"
            ][-1]
        )
        assert not [u for u in users if "executor" in u]
    env_from_refs = 0
    for d in docs:
        pod = (d.get("spec") or {}).get("template", {}).get("spec") or (
            (d.get("spec") or {})
            .get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec")
        )
        if not pod:
            continue
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            for ef in c.get("envFrom", []) or []:
                env_from_refs += 1
                assert "-valkey-user-" not in (ef.get("secretRef") or {}).get(
                    "name", ""
                )
    assert env_from_refs > 0, "no envFrom examined -- render shape changed"
    n_exec, errs = CHECK.check_executors(docs)
    assert n_exec >= 1 and errs == []


def test_provision_hook_generates_distinct_strong_passwords_per_user() -> None:
    """The REAL hook script generates one strong, distinct password per user per instance."""
    docs = _docs()
    secrets = materialize(docs)
    pws: list[str] = []
    for name in _values()["infrastructure"]["valkeyAcl"]["users"]:
        data = secrets[f"w-waddlebot-valkey-user-{name}"]["data"]
        assert data, name
        for key, v in data.items():
            if key == "VALKEY_DURABLE_URL_TLS_AUTH":
                continue  # derived from the password, checked in test_per_user_auth_url_*
            assert key in _PW_KEY.values()
            assert len(v) >= 32 and not v.startswith("REPLACE_ME"), (name, key)
            pws.append(v)
    assert secrets["w-waddlebot-valkey-durable-admin"]["data"][
        "VALKEY_DURABLE_PROBE_PASSWORD"
    ]
    assert secrets["w-waddlebot-valkey-kv-server"]["data"]["VALKEY_KV_PROBE_PASSWORD"]
    assert len(pws) >= 10 and len(set(pws)) == len(pws), (
        "per-service passwords must be distinct"
    )


def test_legacy_default_user_on_is_marked_and_off_is_reset() -> None:
    """legacyDefaultUser=true: legacy user + marker + VALKEY_PASSWORD; false: `default reset`, no marker."""
    for legacy, docs in (
        (True, _docs(extra=("infrastructure.valkeyAcl.legacyDefaultUser=true",))),
        (False, _docs()),
    ):
        for sts_name in _INSTANCES:
            sts = _sts(docs, sts_name)
            c = sts["spec"]["template"]["spec"]["containers"][0]
            users = CHECK.parse_users(c["command"][-1])
            names = {e["name"] for e in c["env"]}
            marker = (sts["metadata"].get("annotations") or {}).get(
                "valkey.waddles/legacy-default-user"
            )
            if legacy:
                assert (
                    users["default"][:2] == ["on", "#$APP_HASH"]
                    and "~*" in users["default"]
                )
                assert marker == "true" and "VALKEY_PASSWORD" in names
            else:
                assert users["default"] == ["reset"]
                assert marker is None and "VALKEY_PASSWORD" not in names
            probe_cmd = " ".join(c["livenessProbe"]["exec"]["command"])
            assert (
                "--user probe" in probe_cmd
                and "VALKEY_PASSWORD"
                not in probe_cmd.replace("VALKEY_PROBE_PASSWORD", "")
            )


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.keys={~*}",
            "literal prefix",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.keys={allkeys}",
            "literal prefix",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.commands=-@all +@all",
            "not allowed",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.commands=-@all +flushall",
            "not allowed",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.commands=+@read",
            "must start with -@all",
        ),
        ("infrastructure.valkeyAcl.users.admin.durable.keys={~x:*}", "reserved"),
        (
            "infrastructure.valkeyAcl.users.Bad_Name.durable.keys={~x:*}",
            "invalid user name",
        ),
    ],
)
def test_render_refuses_wide_or_malformed_grants(override: str, needle: str) -> None:
    """The template fails loud on a wide/malformed grant before anything is applied."""
    r = _helm("values-alpha.yaml", (override,))
    assert r.returncode != 0 and needle in r.stderr, r.stderr[-500:]


# ---------------------------------------------------------------------------------------------
# CI check
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("values", _ENVS)
def test_ci_check_passes_on_every_env(values: str) -> None:
    """The shipped ACL passes the least-privilege gate everywhere (denominator printed)."""
    r = _helm(values)
    assert r.returncode == 0, r.stderr
    flags = (
        ("--require-clients",)
        if values == "values-ci.yaml"
        else ("--require-executor", "--require-clients")
    )
    res = _run_check(r.stdout, *flags)
    assert res.returncode == 0, res.stderr
    assert "examined: 11 service ACL users across 2 instance(s)" in res.stdout
    assert "WARN" not in res.stdout, (
        "legacy default user must be off in every shipped env"
    )


def _plant(stream: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of `old` in the rendered stream (post-render tampering)."""
    assert stream.count(old) == 1, f"plant target {old!r} occurs {stream.count(old)}x"
    return stream.replace(old, new)


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        # planted `~*` for a non-admin service
        (
            "'~waddles:analytics' '~waddles:analytics:dlq'",
            "'~*' '~waddles:analytics:dlq'",
            "wide grant",
        ),
        (
            "'~waddles:analytics' '~waddles:analytics:dlq'",
            "'allkeys' '~waddles:analytics:dlq'",
            "forbidden rule",
        ),
        (
            "'~waddles:analytics' '~waddles:analytics:dlq'",
            "'~waddles:*' '~waddles:analytics:dlq'",
            "wide grant",
        ),
        (
            "'-@all' '+@connection' '+@stream' '-@dangerous'",
            "'-@all' '+@all'",
            "forbidden rule",
        ),
        (
            "'-@all' '+@connection' '+@stream' '-@dangerous'",
            "'-@all' '+@connection' '+keys'",
            "forbidden rule",
        ),
    ],
)
def test_ci_check_fails_on_planted_wide_grant(old: str, new: str, needle: str) -> None:
    """A wide grant planted into the RENDERED manifest must FAIL the gate."""
    r = _helm("values-alpha.yaml")
    assert r.returncode == 0, r.stderr
    assert _run_check(r.stdout, "--require-executor").returncode == 0
    res = _run_check(_plant(r.stdout, old, new), "--require-executor")
    assert res.returncode == 1 and needle in res.stderr, res.stderr


def test_ci_check_fails_on_planted_cross_service_grant() -> None:
    """A syntactically narrow grant reaching another service's protected keys fails ownership."""
    for override, needle in (
        (
            "infrastructure.valkeyAcl.users.svc-process.durable.keys={~waddles:t:*:src:*,~waddles:platform-unban:*}",
            "svc-process",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-analytics.durable.keys={~waddles:analytics,~bundlekv:*}",
            "bundlekv",
        ),
        (
            "infrastructure.valkeyAcl.users.svc-bundles-executor.kv.keys={~bundlekv:*}",
            "executor",
        ),
    ):
        r = _helm("values-alpha.yaml", (override,))
        assert r.returncode == 0, r.stderr
        res = _run_check(r.stdout, "--require-executor")
        assert res.returncode == 1 and needle in res.stderr, (override, res.stderr)


def test_ci_check_fails_on_unmarked_legacy_default_and_on_empty_input() -> None:
    """Legacy `default` without the transition marker fails; zero users examined fails."""
    r = _helm("values-alpha.yaml", ("infrastructure.valkeyAcl.legacyDefaultUser=true",))
    stripped = r.stdout.replace(
        'valkey.waddles/legacy-default-user: "true"', 'valkey.waddles/other: "x"'
    )
    res = _run_check(stripped, "--require-executor")
    assert res.returncode == 1 and "durable/default" in res.stderr
    empty = _run_check("---\nkind: ConfigMap\nmetadata: {name: x}\n")
    assert empty.returncode == 1 and "zero Valkey ACL service users" in empty.stderr


# ---------------------------------------------------------------------------------------------
# real path (docker)
# ---------------------------------------------------------------------------------------------

_COUNTER = itertools.count()


def _docker() -> str:
    """The docker CLI; missing docker is a FAILURE (never a skip)."""
    path = shutil.which("docker")
    assert path, "docker CLI is required for the Valkey ACL real-path tests"
    return path


def _pw(user: str) -> str:
    """Throwaway per-user password for the throwaway container only."""
    return f"acl-real-path-{user}-pw"


def _server_args(users: dict[str, list[str]]) -> list[str]:
    """`--user` argv for the container: the rendered rules with real hashes substituted."""
    args: list[str] = []
    for name, rules in users.items():
        if name == "admin":
            continue
        concrete = [
            "#" + hashlib.sha256(_pw(name).encode()).hexdigest()
            if r.startswith("#$")
            else r
            for r in rules
        ]
        args += ["--user", name, *concrete]
    return args


class _Valkey:
    """A throwaway Valkey container running the rendered ACL; removed on close."""

    def __init__(self, image: str, users: dict[str, list[str]]) -> None:
        self.name = f"wtc-valkeyacl-{os.getpid()}-{next(_COUNTER)}"
        cmd = [
            _docker(),
            "run",
            "-d",
            "--name",
            self.name,
            image,
            "valkey-server",
            "--port",
            "6379",
            "--save",
            "",
            "--appendonly",
            "no",
            *_server_args(users),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert out.returncode == 0, out.stderr
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if "PONG" in self.cli("probe", "PING"):
                return
            time.sleep(0.5)
        logs = subprocess.run(
            [_docker(), "logs", self.name], capture_output=True, text=True, check=False
        )
        self.close()
        raise AssertionError(
            f"valkey never became ready: {logs.stdout[-2000:]} {logs.stderr[-2000:]}"
        )

    def cli(self, user: str | None, *argv: str) -> str:
        """Run one command as `user` (None = unauthenticated); combined output."""
        auth = [] if user is None else ["--user", user, "--pass", _pw(user)]
        out = subprocess.run(
            [
                _docker(),
                "exec",
                self.name,
                "valkey-cli",
                "--no-auth-warning",
                *auth,
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout + out.stderr

    def close(self) -> None:
        """Force-remove the container."""
        subprocess.run(
            [_docker(), "rm", "-f", "-v", self.name], capture_output=True, check=False
        )


def _rendered_users(sts_name: str) -> dict[str, list[str]]:
    """The ACL rules the chart renders for one instance, with legacy `default` OFF (target state)."""
    docs = _docs(extra=("infrastructure.valkeyAcl.legacyDefaultUser=false",))
    users = CHECK.parse_users(
        _sts(docs, sts_name)["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    )
    assert users["default"] == ["reset"] and len(users) >= 5
    return users


def _image(key: str) -> str:
    """The digest-pinned image the chart deploys for one instance."""
    return str(_values()["infrastructure"][key]["image"])


@pytest.fixture(scope="module")
def durable() -> Iterator[_Valkey]:
    """valkey-durable's rendered ACL in a real container."""
    v = _Valkey(_image("valkeyDurable"), _rendered_users("valkey-durable"))
    try:
        yield v
    finally:
        v.close()


@pytest.fixture(scope="module")
def kv() -> Iterator[_Valkey]:
    """valkey-kv's rendered ACL in a real container."""
    v = _Valkey(_image("valkeyKv"), _rendered_users("valkey-kv"))
    try:
        yield v
    finally:
        v.close()


_UNBAN = "waddles:platform-unban:discord:events"


def test_real_svc_process_cannot_lpush_platform_unban(durable: _Valkey) -> None:
    """svc-process (and hub-api, analytics) get NOPERM on the unban list; ingest produces, action consumes."""
    for user in ("svc-process", "hub-api", "svc-analytics", "role-sync-inbound"):
        assert "NOPERM" in durable.cli(user, "LPUSH", _UNBAN, "forged"), user
    assert durable.cli("svc-ingest", "LPUSH", _UNBAN, "signed-event").strip() == "1"
    assert durable.cli("svc-action", "RPOP", _UNBAN).strip() == "signed-event"
    assert "NOPERM" in durable.cli(
        "svc-ingest", "SET", "waddles:platform-unban:discord:seen:x", "1"
    )


def test_real_delivery_streams_are_stage_scoped(durable: _Valkey) -> None:
    """Only the owning stages touch delivery streams; analytics/hub-api cannot forge them."""
    action = "waddles:t:acme:c:main:app:acme.bot:action"
    src = "waddles:t:acme:c:main:src:discord:g1:events"
    assert "-" in durable.cli("svc-process", "XADD", action, "*", "k", "v")
    assert "-" in durable.cli("svc-ingest", "XADD", src, "*", "k", "v")
    for user, key in (
        ("svc-analytics", action),
        ("hub-api", action),
        ("svc-ingest", action),
        ("svc-action", src),
        ("hub-api", src),
    ):
        assert "NOPERM" in durable.cli(user, "XADD", key, "*", "k", "v"), (user, key)
    other_src = "waddles:t:acme:c:main:src:twitch:tw1:events"
    assert "NOPERM" in durable.cli(
        "role-sync-inbound", "XADD", other_src, "*", "k", "v"
    )  # Discord streams only


def test_real_dangerous_commands_and_default_user_refused(
    durable: _Valkey, kv: _Valkey
) -> None:
    """FLUSHALL/KEYS/CONFIG SET are NOPERM for every service user; `default` is off; probe = PING only."""
    for inst, users in (
        (durable, ("svc-ingest", "svc-process", "svc-action", "hub-api")),
        (kv, ("svc-process", "svc-action")),
    ):
        for user in users:
            for argv in (
                ("FLUSHALL",),
                ("KEYS", "*"),
                ("CONFIG", "SET", "maxmemory", "1"),
                ("ACL", "LIST"),
            ):
                assert "NOPERM" in inst.cli(user, *argv), (user, argv)
        assert "NOAUTH" in inst.cli(None, "GET", "anything")
        assert "NOPERM" in inst.cli("probe", "GET", "anything")
        assert "PONG" in inst.cli("probe", "PING")


def test_real_bundlekv_only_reachable_by_kv_host_services(
    kv: _Valkey, durable: _Valkey
) -> None:
    """Bundle kv lives behind the host capability: svc-process/svc-action read+write, executor has no user,
    hub-api may only SCAN+UNLINK data keys (erasure), every other service has no kv user at all."""
    key = "bundlekv:acme:main:acme.other-app:data:secret"
    assert kv.cli("svc-process", "SET", key, "v").strip() == "OK"
    assert kv.cli("svc-action", "GET", key).strip() == "v"
    assert kv.cli("svc-process", "CONFIG", "GET", "maxmemory-policy").strip()
    assert "NOPERM" not in kv.cli(
        "svc-action", "EVAL", "return redis.call('TIME')", "0"
    )
    # no executor ACL user exists: AUTH fails and the connection stays unauthenticated
    for user in (
        "svc-bundles-executor",
        "bundle-executor",
        "svc-ingest",
        "svc-analytics",
    ):
        assert "NOAUTH" in kv.cli(user, "GET", key) or "WRONGPASS" in kv.cli(
            user, "GET", key
        ), user
    assert "NOPERM" in kv.cli("hub-api", "GET", key)
    assert key in kv.cli("hub-api", "SCAN", "0", "MATCH", "bundlekv:*:data:*")
    assert "NOPERM" in kv.cli(
        "hub-api", "UNLINK", "bundlekv:acme:main:acme.other-app:meta"
    )
    assert kv.cli("hub-api", "UNLINK", key).strip() == "1"
    # bundle kv never lives on the durable instance for anyone
    for user in ("svc-process", "svc-action", "hub-api"):
        assert "NOPERM" in durable.cli(user, "GET", key), user


# ---------------------------------------------------------------------------------------------
# client wiring (part B): every client authenticates as its OWN user
# ---------------------------------------------------------------------------------------------

_CLIENTS = {
    # workload name -> (container user, instances it must authenticate to)
    "w-waddlebot-svc-ingest-rust": ("svc-ingest", ("durable",)),
    "w-waddlebot-svc-process-rust": ("svc-process", ("durable", "kv")),
    "w-waddlebot-svc-action-rust": ("svc-action", ("durable", "kv")),
    "w-waddlebot-svc-analytics": ("svc-analytics", ("durable",)),
    "w-waddlebot-hub-api-v3": ("hub-api", ("durable", "kv")),
    "w-waddlebot-landing-stats-refresh": ("landing-stats", ("durable",)),
    "w-waddlebot-core-bundle-seeder": ("core-bundle-seeder", ("durable",)),
    "w-waddlebot-role-sync-inbound": ("role-sync-inbound", ("durable",)),
}
_ALL_CLIENTS_ON = (
    "pipeline.svcAnalytics.enabled=true",
    "pipeline.roleSync.enabled=true",
    "pipeline.roleSync.inbound.enabled=true",
)


def _pod_spec(d: dict[str, Any]) -> dict[str, Any]:
    """Pod spec of a Deployment/Job/CronJob."""
    if d["kind"] == "CronJob":
        return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return d["spec"]["template"]["spec"]


def test_every_client_authenticates_as_its_own_user() -> None:
    """Each Valkey client: VALKEY_USERNAME/VALKEY_KV_USERNAME = its user, passwords from ITS Secret."""
    docs = _docs(extra=_ALL_CLIENTS_ON)
    found = 0
    for d in docs:
        if (
            d["kind"] not in ("Deployment", "Job", "CronJob")
            or d["metadata"]["name"] not in _CLIENTS
        ):
            continue
        user, insts = _CLIENTS[d["metadata"]["name"]]
        env = {e["name"]: e for e in _pod_spec(d)["containers"][0]["env"]}
        sec = f"w-waddlebot-valkey-user-{user}"
        if "durable" in insts:
            assert env["VALKEY_USERNAME"]["value"] == user
            assert env["VALKEY_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
                "name": sec,
                "key": "VALKEY_DURABLE_PASSWORD",
            }
            url_ref = env["VALKEY_URL"]["valueFrom"]["secretKeyRef"]
            assert url_ref["key"] in (
                "VALKEY_DURABLE_URL_TLS",
                "VALKEY_DURABLE_URL_TLS_AUTH",
            )
            if url_ref["key"].endswith("_AUTH"):
                assert url_ref["name"] == sec, (
                    "an auth URL must carry the client's OWN user"
                )
        if "kv" in insts:
            assert env["VALKEY_KV_USERNAME"]["value"] == user
            assert env["VALKEY_KV_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
                "name": sec,
                "key": "VALKEY_KV_PASSWORD",
            }
        found += 1
    assert found == len(_CLIENTS), (
        f"expected {len(_CLIENTS)} client workloads, found {found}"
    )


def test_per_user_auth_url_embeds_its_own_user_and_password() -> None:
    """The provisioned per-user VALKEY_DURABLE_URL_TLS_AUTH = rediss://<user>:<own pw>@durable."""
    secrets = materialize(_docs())
    n = 0
    for name, cfg in _values()["infrastructure"]["valkeyAcl"]["users"].items():
        if not cfg.get("durable"):
            continue
        data = secrets[f"w-waddlebot-valkey-user-{name}"]["data"]
        url = data["VALKEY_DURABLE_URL_TLS_AUTH"]
        assert url.startswith(
            f"rediss://{name}:{data['VALKEY_DURABLE_PASSWORD']}@infra-valkey-durable."
        ), name
        assert "ssl_cert_reqs=required" in url
        n += 1
    assert n >= 8


def test_ci_check_fails_when_a_client_uses_the_shared_password() -> None:
    """A client planted back onto the legacy shared password fails the gate."""
    r = _helm("values-alpha.yaml")
    head = "- name: VALKEY_PASSWORD\n          valueFrom:\n            secretKeyRef:\n              name: "
    old = head + "w-waddlebot-valkey-user-svc-ingest\n              key: VALKEY_DURABLE_PASSWORD"
    new = head + "w-waddlebot-secrets\n              key: VALKEY_DURABLE_PASSWORD"
    res = _run_check(_plant(r.stdout, old, new), "--require-executor", "--require-clients")
    assert res.returncode == 1 and "shared legacy password" in res.stderr, res.stderr


def test_client_wiring_refuses_a_user_without_an_acl_entry() -> None:
    """Removing a client's ACL user fails the render (no silent fallback to `default`)."""
    r = _helm(
        "values-alpha.yaml", ("infrastructure.valkeyAcl.users.landing-stats=null",)
    )
    assert (
        r.returncode != 0
        and "valkeyAcl.users.landing-stats.durable is not configured" in r.stderr
    )


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ("-@all +xadd", "not allowed in a selector"),
        ("+xgroup|create", "must start with -@all"),
    ],
)
def test_render_refuses_wide_selectors(override: str, needle: str) -> None:
    """Selectors may only grant consumer-group admin on specific key families."""
    sel = "infrastructure.valkeyAcl.users.hub-api.durable.selectors[0]"
    r = _helm("values-alpha.yaml", (f"{sel}.keys={{~waddles:t:*:src:*}}", f"{sel}.commands={override}"))
    assert r.returncode != 0 and needle in r.stderr, r.stderr[-400:]
    wide = _helm("values-alpha.yaml", (f"{sel}.keys={{~*}}", f"{sel}.commands=-@all +xgroup|create"))
    assert wide.returncode != 0 and "literal prefix" in wide.stderr, wide.stderr[-400:]


def test_ci_check_fails_on_planted_wide_selector() -> None:
    """A selector widened in the rendered manifest fails the gate."""
    r = _helm("values-alpha.yaml")
    planted = _plant(
        r.stdout,
        "'(~waddles:t:*:src:* ~waddles:t:*:app:* -@all +xgroup|create +xgroup|destroy)'",
        "'(~waddles:t:*:src:* ~waddles:t:*:app:* -@all +xadd)'",
    )
    res = _run_check(planted, "--require-executor")
    assert res.returncode == 1 and "only consumer-group admin" in res.stderr, res.stderr


def test_real_hub_api_may_only_provision_groups_on_spine_streams(
    durable: _Valkey,
) -> None:
    """hub-api's selector: XGROUP CREATE/DESTROY on spine streams works; XADD/XREADGROUP are NOPERM."""
    src = "waddles:t:acme:c:main:src:discord:g9:events"
    stage = "waddles:t:acme:c:_tenant:app:acme.bot:process"
    for key in (src, stage):
        assert (
            durable.cli(
                "hub-api", "XGROUP", "CREATE", key, "acme.bot", "$", "MKSTREAM"
            ).strip()
            == "OK"
        )
        assert "NOPERM" in durable.cli("hub-api", "XADD", key, "*", "k", "v")
        assert "NOPERM" in durable.cli(
            "hub-api",
            "XREADGROUP",
            "GROUP",
            "acme.bot",
            "c1",
            "COUNT",
            "1",
            "STREAMS",
            key,
            ">",
        )
        assert (
            durable.cli("hub-api", "XGROUP", "DESTROY", key, "acme.bot").strip() == "1"
        )
    assert (
        durable.cli(
            "core-bundle-seeder", "XGROUP", "CREATE", stage, "seed", "$", "MKSTREAM"
        ).strip()
        == "OK"
    )
    assert "NOPERM" in durable.cli(
        "core-bundle-seeder", "XGROUP", "CREATE", src, "seed", "$", "MKSTREAM"
    )
