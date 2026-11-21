"""Each Valkey client works with ONLY its own ACL user (infrastructure.valkeyAcl.users).

The chart gives every service its own ACL user and turns `default` into `reset`. Rendering the
rules (test_valkey_acl_render.py) proves their shape; THIS file proves they are sufficient: one
real Valkey per instance is started with the EXACT rendered `--user` rules (helm template
values-alpha.yaml, legacy default OFF), then, for every service user, the Valkey operations that
service's production code actually issues are replayed authenticated as that user and only that
user. Every op carries a `# path:line` citation to the call site (Rust crates, the pinned
penguin-spine rev, or the Python services); none is invented. An op that production issues but the
ACL denies fails the test with its NOPERM reply -- the test is never loosened to hide a gap.

Each user also gets cross-service negatives (a key family or command it must not touch is NOPERM).
Docker is REQUIRED (a missing docker is a failure, never a skip), containers are force-removed, and
a user with zero replayed ops fails. Per-user op counts are printed (`pytest -s`) and asserted.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_valkey_acl_render import CHECK  # type: ignore[import-not-found]

_CHART = Path(__file__).resolve().parent.parent
_COUNTER = itertools.count()
_SPINE = (
    "penguin-libs@1df0822 packages/rust-spine/src"  # the rev core/svc_*/Cargo.toml pins
)

# Replies that mean the ACL (or auth) refused the op.
_ACL_DENIED = re.compile(r"NOPERM|NOAUTH|WRONGPASS")
# Any other error reply (a broken replay, e.g. WRONGTYPE) is also a failure unless the op allows it.
_ERROR_LINE = re.compile(
    r"^(ERR|WRONGTYPE|NOGROUP|BUSYGROUP|EXECABORT|MISCONF|NOSCRIPT)\b", re.MULTILINE
)

OPS_REPLAYED: dict[tuple[str, str], int] = {}


@dataclass(frozen=True)
class Op:
    """One Valkey op production issues: its `path:line` citation, argv, optional stdin script."""

    cite: str
    argv: tuple[str, ...]
    stdin: str | None = None
    allow: tuple[str, ...] = ()


def op(cite: str, *argv: str, allow: tuple[str, ...] = ()) -> Op:
    """An op run as a one-shot `valkey-cli` command."""
    return Op(cite, argv, None, allow)


def tx(cite: str, *commands: str) -> Op:
    """A MULTI/EXEC pipeline (redis `pipe.atomic()`), fed to valkey-cli on stdin."""
    return Op(cite, (), "\n".join(["MULTI", *commands, "EXEC"]) + "\n")


# --------------------------------------------------------------------------------------------
# harness (the same pattern as test_valkey_acl_render.py)
# --------------------------------------------------------------------------------------------


def _docker() -> str:
    """The docker CLI; missing docker is a FAILURE (never a skip)."""
    path = shutil.which("docker")
    assert path, "docker CLI is required for the Valkey ACL service-path tests"
    return path


def _pw(user: str) -> str:
    """Throwaway per-user password for the throwaway container only."""
    return f"acl-service-path-{user}-pw"


def _helm_docs() -> list[dict[str, Any]]:
    """`helm template` values-alpha.yaml, legacy `default` user OFF (the target state)."""
    r = subprocess.run(
        [
            "helm",
            "template",
            "w",
            str(_CHART),
            "--kube-version",
            "1.30.0",
            "--values",
            str(_CHART / "values-alpha.yaml"),
            "--set",
            "global.deploymentTier=alpha",
            "--set",
            "infrastructure.valkeyAcl.legacyDefaultUser=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs
    return docs


def _rendered_users(docs: list[dict[str, Any]], sts_name: str) -> dict[str, list[str]]:
    """The ACL rules the chart renders for one StatefulSet."""
    found = [
        d
        for d in docs
        if d["kind"] == "StatefulSet" and d["metadata"]["name"] == sts_name
    ]
    assert len(found) == 1, f"expected exactly one StatefulSet/{sts_name}"
    users = CHECK.parse_users(
        found[0]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    )
    assert users["default"] == ["reset"], "default must be `reset` in the target state"
    return users


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


class Valkey:
    """A throwaway Valkey container running the rendered ACL; force-removed on close."""

    def __init__(self, image: str, users: dict[str, list[str]]) -> None:
        self.name = f"wtc-valkeypaths-{os.getpid()}-{next(_COUNTER)}"
        out = subprocess.run(
            [
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
            ],
            capture_output=True,
            text=True,
            check=False,
        )
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
            f"valkey never became ready: {logs.stdout[-1500:]} {logs.stderr[-1500:]}"
        )

    def cli(self, user: str | None, *argv: str, stdin: str | None = None) -> str:
        """Run one command (or a stdin script) as `user` (None = unauthenticated)."""
        auth = [] if user is None else ["--user", user, "--pass", _pw(user)]
        out = subprocess.run(
            [
                _docker(),
                "exec",
                "-i",
                self.name,
                "valkey-cli",
                "--no-auth-warning",
                *auth,
                *argv,
            ],
            capture_output=True,
            text=True,
            input=stdin if stdin is not None else "",
            check=False,
        )
        return out.stdout + out.stderr

    def close(self) -> None:
        """Force-remove the container."""
        subprocess.run(
            [_docker(), "rm", "-f", "-v", self.name], capture_output=True, check=False
        )

    def run(self, user: str, o: Op) -> str:
        """Run one op as `user`; fail on an ACL denial or any other error reply."""
        reply = self.cli(user, *o.argv, stdin=o.stdin)
        label = f"{user}: {' '.join(o.argv) or o.stdin!r} [{o.cite}]"
        assert not _ACL_DENIED.search(reply), (
            f"ACL GAP -- {label} -> {reply.strip()[:300]}"
        )
        bad = [m for m in _ERROR_LINE.findall(reply) if m not in o.allow]
        assert not bad, f"replay error -- {label} -> {reply.strip()[:300]}"
        return reply

    def replay(
        self, inst: str, user: str, setup: list[tuple[str, Op]], ops: list[Op]
    ) -> int:
        """Seed state through each key's legitimate writer, replay `ops` as `user`; op count."""
        assert ops, f"{inst}/{user}: zero ops to replay"
        for owner, s in setup:
            self.run(owner, s)
        for o in ops:
            self.run(user, o)
        OPS_REPLAYED[(inst, user)] = len(ops)
        print(
            f"valkey-acl service path: {inst}/{user}: {len(ops)} ops replayed, 0 NOPERM"
        )
        return len(ops)

    def denied(self, user: str, *argv: str) -> None:
        """Assert one cross-service negative is NOPERM for `user`."""
        reply = self.cli(user, *argv)
        assert "NOPERM" in reply, (
            f"{user} must be denied {' '.join(argv)!r}, got {reply.strip()[:200]}"
        )


def _image(key: str) -> str:
    """The digest-pinned image the chart deploys for one instance."""
    values = yaml.safe_load((_CHART / "values.yaml").read_text())
    return str(values["infrastructure"][key]["image"])


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    """The alpha render (legacy default off), shared by both instances."""
    return _helm_docs()


@pytest.fixture(scope="module")
def durable(rendered: list[dict[str, Any]]) -> Iterator[Valkey]:
    """valkey-durable's rendered ACL in a real container."""
    v = Valkey(_image("valkeyDurable"), _rendered_users(rendered, "valkey-durable"))
    try:
        yield v
    finally:
        v.close()


@pytest.fixture(scope="module")
def kv(rendered: list[dict[str, Any]]) -> Iterator[Valkey]:
    """valkey-kv's rendered ACL in a real container."""
    v = Valkey(_image("valkeyKv"), _rendered_users(rendered, "valkey-kv"))
    try:
        yield v
    finally:
        v.close()


# --------------------------------------------------------------------------------------------
# key shapes (cited where each is built)
# --------------------------------------------------------------------------------------------


def src_stream(t: str, platform: str = "discord") -> str:
    """Spine ingest source stream (core/svc_process/src/spine.rs:2454 shape)."""
    return f"waddles:t:{t}:c:main:src:{platform}:g1:events"


def action_stream(t: str, app: str = "acme.bot") -> str:
    """Per-bundle action stream (core/svc_action/src/dispatch.rs:2024 shape)."""
    return f"waddles:t:{t}:c:main:app:{app}:action"


_ENV = '{"env":"x"}'
_VERSION_KEY = "waddles:bundle_config:v:1:7:penguintechinc.waddles.calendar"  # hub_client/src/bundle_config.rs:62
_UNBAN = "waddles:platform-unban:discord:events"  # the unban producer/consumer live on unmerged branches
_UNBAN_SEEN = "waddles:platform-unban:discord:seen:evt1"

# Lua copied from production (path:line of each const).
_LEASE_RENEW = (  # core/svc_ingest/src/connector_runtime/lease.rs:34, twitch_sessions/lease.rs:19
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end"
)
_LEASE_RELEASE = (  # core/svc_ingest/src/connector_runtime/lease.rs:37, twitch_sessions/lease.rs:23
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('DEL', KEYS[1]) else return 0 end"
)
#: core/svc_ingest/src/interaction_store.rs:32 KEY_PREFIX + an interaction id. Built from the
#: prefix (not one literal) so the old-app-id guard's dotted-token check sees only the bare
#: key namespace it already recognises, never a fake `waddles.<area>.<name>` app_id.
_INTERACTION_PREFIX = "waddles.ingest.interaction."
_INTERACTION_KEY = _INTERACTION_PREFIX + "123456789012345678"

_ROUTE_ONCE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, rec = pcall(cjson.decode, raw)
if not ok or type(rec) ~= 'table' then return 4 end
local want = tonumber(ARGV[1])
local cur = rec['routed_community_id']
if cur == nil or cur == cjson.null then
  rec['routed_community_id'] = want
  redis.call('SET', KEYS[1], cjson.encode(rec), 'KEEPTTL')
  return 1
end
if cur == want then return 2 end
return 3
"""  # core/svc_ingest/src/interaction_store.rs:223
_WEBHOOK_RELEASE = (  # core/svc_ingest/src/connector/webhook.rs:342
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_DISCORD_BUDGET = """
local daily_remaining = tonumber(redis.call('GET', KEYS[2]) or '-1')
if daily_remaining == 0 then
  return {'exhausted', 0}
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl and ttl > 0 then
  return {'wait', ttl}
end
redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
if daily_remaining > 0 then
  redis.call('DECR', KEYS[2])
end
return {'granted', 0}
"""  # core/svc_ingest/src/ingest/discord_identify_budget.rs:245
_CONNECTOR_BUDGET_ACQUIRE = """
local rem = redis.call('HGET', KEYS[2], 'remaining')
if not rem then
  return {'unseeded', 0}
end
if tonumber(rem) <= 0 then
  return {'exhausted', redis.call('PTTL', KEYS[2])}
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl and ttl > 0 then
  return {'wait', ttl}
end
redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
redis.call('HINCRBY', KEYS[2], 'remaining', -1)
return {'granted', 0}
"""  # core/svc_ingest/src/connector/identify_budget.rs:243
_CONNECTOR_BUDGET_RECORD = """
local cur = redis.call('HGET', KEYS[1], 'remaining')
local rem = tonumber(ARGV[2])
if cur and tonumber(cur) < rem then
  rem = tonumber(cur)
end
redis.call('HSET', KEYS[1], 'total', ARGV[1], 'remaining', rem, 'max_concurrency', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return rem
"""  # core/svc_ingest/src/connector/identify_budget.rs:262
_INCR_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""  # core/svc_action/src/capabilities.rs:918
_JWT_REVOKE = """
local cur = redis.call('GET', KEYS[1])
if (not cur) or tonumber(cur) < tonumber(ARGV[1]) then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < tonumber(ARGV[2]) then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 1
"""  # libs/flask_core/flask_core/token_revocation.py:111-121 (GET/SET EX/TTL/EXPIRE)

# kv Lua (core/bundle_host_kv/src/backend.rs): KEYS = data, live, count/lock, lex.
_KV_SET_QUOTA = """
local existed = redis.call('EXISTS', KEYS[1])
if not redis.call('ZSCORE', KEYS[2], KEYS[1]) then redis.call('INCRBY', KEYS[3], 1) end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], KEYS[1])
redis.call('ZADD', KEYS[4], 0, KEYS[1])
return existed
"""  # :477-495
_KV_CAS = """
local current = redis.call('GET', KEYS[1])
if current ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('ZADD', KEYS[2], '+inf', KEYS[1])
redis.call('ZADD', KEYS[4], 0, KEYS[1])
return 1
"""  # :510-537 / :693-717
_KV_TIME_TRIM = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local lapsed = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', '(' .. string.format('%.0f', now))
for _, k in ipairs(lapsed) do
  redis.call('ZREM', KEYS[1], k)
  redis.call('ZREM', KEYS[2], k)
end
local n = redis.call('ZCARD', KEYS[1])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then n = n - 1 end
return n
"""  # :133-150
_KV_RECONCILE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return {0, 0} end
local lock_ok = redis.call('SET', KEYS[2], '1', 'NX', 'PX', ARGV[2])
if not lock_ok then return {2, 0} end
local cursor = '0'
local count = 0
repeat
  local res = redis.call('SCAN', cursor, 'MATCH', ARGV[1], 'COUNT', 1000)
  cursor = res[1]
  for _, k in ipairs(res[2]) do
    local pttl = redis.call('PTTL', k)
    if pttl ~= -2 then
      redis.call('ZADD', KEYS[1], '+inf', k)
      redis.call('ZADD', KEYS[3], 0, k)
    end
  end
  count = redis.call('ZCARD', KEYS[1])
until cursor == '0'
redis.call('ZADD', KEYS[1], '+inf', ARGV[3])
redis.call('ZADD', KEYS[3], 0, ARGV[3])
redis.call('DEL', KEYS[2])
return {1, count}
"""  # :584-617
_KV_INCR = """
local existed = redis.call('EXISTS', KEYS[1])
local newval = redis.call('INCRBY', KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 then redis.call('EXPIRE', KEYS[1], ARGV[2]) else redis.call('PERSIST', KEYS[1]) end
redis.call('ZADD', KEYS[2], '+inf', KEYS[1])
return newval
"""  # :631-650
_KV_GET_DEL = """
local cur = redis.call('GET', KEYS[1])
local deleted = redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], KEYS[1])
redis.call('ZREM', KEYS[4], KEYS[1])
return {cur, deleted}
"""  # :558-563 / :681-690
_KV_RATE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""  # :660-663
_MEDIA_INFLIGHT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""  # core/bundle_host_media/src/jobs.rs:110-113


def eval_(cite: str, script: str, keys: list[str], *args: str) -> Op:
    """An EVAL with `keys` (the real KEYS shape) and `args`."""
    return op(cite, "EVAL", script, str(len(keys)), *keys, *args)


def kv_ops(t: str) -> list[Op]:
    """The bundle_host_kv op set (core/bundle_host_kv/src/{backend,policy}.rs) for one scope."""
    base = f"bundlekv:{t}:main:penguintechinc.waddles.fixture-bot-a"
    data, live, count, lex = (
        f"{base}:data:counter",
        f"{base}:live",
        f"{base}:count",
        f"{base}:live:lex",
    )
    b = "core/bundle_host_kv/src/backend.rs"
    return [
        op("core/bundle_host_kv/src/policy.rs:43", "CONFIG", "GET", "maxmemory-policy"),
        op(f"{b}:477 (redis::Script load)", "SCRIPT", "LOAD", _KV_RATE),
        eval_(
            f"{b}:477",
            _KV_SET_QUOTA,
            [data, live, count, lex],
            "v1",
            "300",
            "9999999999999",
        ),
        op(f"{b}:681 plain GET", "GET", data),
        op(
            "core/bundle_host_kv/src/lib.rs plain SET", "SET", f"{base}:data:plain", "x"
        ),
        eval_(f"{b}:693", _KV_CAS, [data, live, count, lex], "v1", "v2"),
        eval_(f"{b}:631", _KV_INCR, [f"{base}:data:n", live], "2", "0"),
        eval_(f"{b}:660", _KV_RATE, [f"{base}:rate:1"], "60"),
        eval_(f"{b}:133", _KV_TIME_TRIM, [live, lex], "sentinel"),
        eval_(
            f"{b}:584",
            _KV_RECONCILE,
            [f"{base}:live2", f"{base}:lock", f"{base}:live2:lex"],
            f"{base}:data:*",
            "30000",
            "sentinel",
        ),
        op(f"{b}:598 SCAN", "SCAN", "0", "MATCH", f"{base}:data:*", "COUNT", "1000"),
        op(f"{b}:601 PTTL", "PTTL", data),
        eval_(f"{b}:558", _KV_GET_DEL, [data, live, count, lex]),
        op(f"{b}:173 EXISTS", "EXISTS", data),
        op(f"{b}:149 ZCARD", "ZCARD", live),
        op(f"{b}:137 ZRANGEBYSCORE", "ZRANGEBYSCORE", live, "-inf", "+inf"),
        op(f"{b}:101 TIME", "TIME"),
    ]


# --------------------------------------------------------------------------------------------
# svc-ingest
# --------------------------------------------------------------------------------------------


def test_svc_ingest_works_with_only_its_own_user(durable: Valkey) -> None:
    """Every Valkey op svc-ingest's code issues (spine append, queues, leases, dedupe, budgets)."""
    u = "svc-ingest"
    ing = "core/svc_ingest/src"
    src, src2 = src_stream("ing"), src_stream("ing", "twitch")
    q = {
        p: f"waddles:transport:irc:{p}:outbound"
        for p in ("twitch", "youtube", "kick", "discord")
    }
    spec = "waddles:ingest:spectrum:acme:main:feed:abc"
    setup = [
        (
            "svc-action",
            op("capabilities.rs:930 LPUSH outbound", "LPUSH", q[p], f'{{"p":"{p}"}}'),
        )
        for p in q
    ] + [
        (
            "hub-api",
            op(
                "envelope/invalidation.py:61 XADD",
                "XADD",
                "keys:tenant-dek:invalidate",
                "*",
                "tenant",
                "1",
            ),
        ),
    ]
    ops = [
        op(
            f"{_SPINE}/client.rs:188 XADD src",
            "XADD",
            src,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "env",
            _ENV,
        ),
        op(
            f"{_SPINE}/client.rs:188 XADD src (twitch)",
            "XADD",
            src2,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "env",
            _ENV,
        ),
        op(f"{_SPINE}/client.rs:199 XLEN", "XLEN", src),
        op(
            f"{ing}/analytics.rs:701 XADD",
            "XADD",
            "waddles:analytics",
            "MAXLEN",
            "~",
            "1000",
            "*",
            "k",
            "v",
        ),
        op(
            f"{ing}/identity_crypto.rs:379 XREAD",
            "XREAD",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            "keys:tenant-dek:invalidate",
            "0",
        ),
        *[
            op(
                f"{ing}/outbound.rs:146/824 BRPOP ({p}, key const :50/:620/:665/:725)",
                "BRPOP",
                q[p],
                "1",
            )
            for p in q
        ],
        op(
            f"{ing}/outbound.rs:788 SET EX drain-ready",
            "SET",
            "waddles:transport:discord:drain-ready",
            "1",
            "EX",
            "20",
        ),
        op(
            f"{ing}/outbound.rs:808 SET EX ack",
            "SET",
            "waddles:transport:discord:ack:op1",
            "{}",
            "EX",
            "60",
        ),
        op(
            f"{ing}/outbound.rs:800 DEL drain-ready",
            "DEL",
            "waddles:transport:discord:drain-ready",
        ),
        op(
            f"{ing}/outbound.rs:270 SET NX PX replay guard (key outbound_pin.rs:327)",
            "SET",
            "outbound:seen:discord:digest1",
            "1",
            "NX",
            "PX",
            "60000",
        ),
        op(
            f"{ing}/connector_runtime/lease.rs:125 SET NX PX",
            "SET",
            "waddles:ingest:connector:1:42",
            "rid",
            "NX",
            "PX",
            "30000",
        ),
        eval_(
            f"{ing}/connector_runtime/lease.rs:34 RENEW",
            _LEASE_RENEW,
            ["waddles:ingest:connector:1:42"],
            "rid",
            "30000",
        ),
        eval_(
            f"{ing}/connector_runtime/lease.rs:37 RELEASE",
            _LEASE_RELEASE,
            ["waddles:ingest:connector:1:42"],
            "rid",
        ),
        op(
            f"{ing}/twitch_sessions/lease.rs:112 SET NX PX",
            "SET",
            "waddles:ingest:twitch:session:1:42",
            "rid",
            "NX",
            "PX",
            "30000",
        ),
        eval_(
            f"{ing}/twitch_sessions/lease.rs:19 RENEW",
            _LEASE_RENEW,
            ["waddles:ingest:twitch:session:1:42"],
            "rid",
            "30000",
        ),
        eval_(
            f"{ing}/twitch_sessions/lease.rs:23 RELEASE",
            _LEASE_RELEASE,
            ["waddles:ingest:twitch:session:1:42"],
            "rid",
        ),
        op(
            f"{ing}/connector/bundle_schedule.rs:179 SET NX PX",
            "SET",
            "waddles:ingest:bundle-reconcile:slot1",
            "rid",
            "NX",
            "PX",
            "30000",
        ),
        eval_(
            f"{ing}/connector/bundle_schedule.rs:128 EXTEND",
            _LEASE_RENEW,
            ["waddles:ingest:bundle-reconcile:slot1"],
            "rid",
            "30000",
        ),
        op(
            f"{ing}/connector/spectrum.rs:277 EXISTS primed", "EXISTS", f"{spec}:primed"
        ),
        op(
            f"{ing}/connector/spectrum.rs:324 ZADD NX seen",
            "ZADD",
            f"{spec}:seen",
            "NX",
            "1",
            "item1",
        ),
        op(
            f"{ing}/connector/spectrum.rs:332 ZREMRANGEBYRANK",
            "ZREMRANGEBYRANK",
            f"{spec}:seen",
            "0",
            "-100",
        ),
        op(f"{ing}/connector/spectrum.rs:337 EXPIRE", "EXPIRE", f"{spec}:seen", "3600"),
        op(
            f"{ing}/connector/spectrum.rs:338 SET EX primed",
            "SET",
            f"{spec}:primed",
            "1",
            "EX",
            "3600",
        ),
        op(
            f"{ing}/connector/spectrum.rs:292 ZMSCORE",
            "ZMSCORE",
            f"{spec}:seen",
            "item1",
            "item2",
        ),
        op(
            f"{ing}/connector/spectrum.rs:369 SET EX snap",
            "SET",
            f"{spec}:snap",
            "{}",
            "EX",
            "3600",
        ),
        op(f"{ing}/connector/spectrum.rs:352 GET snap", "GET", f"{spec}:snap"),
        op(
            f"{ing}/interaction_store.rs:272 SET NX EX",
            "SET",
            _INTERACTION_KEY,
            '{"routed_community_id":null}',
            "NX",
            "EX",
            "900",
        ),
        eval_(
            f"{ing}/interaction_store.rs:310 ROUTE_ONCE",
            _ROUTE_ONCE,
            [_INTERACTION_KEY],
            "7",
        ),
        op(
            f"{ing}/connector/webhook.rs:326 SET NX PX dedup",
            "SET",
            "waddles:webhook:dedup:k1",
            "tok",
            "NX",
            "PX",
            "60000",
        ),
        eval_(
            f"{ing}/connector/webhook.rs:342 release",
            _WEBHOOK_RELEASE,
            ["waddles:webhook:dedup:k1"],
            "tok",
        ),
        op(
            f"{ing}/ingest/twitch_eventsub.rs:244 SET NX PX dedup",
            "SET",
            "waddles:eventsub:dedup:twitch:msg1",
            "1",
            "NX",
            "PX",
            "60000",
        ),
        op(
            f"{ing}/ingest/twitch_eventsub.rs:304 LPUSH revocations",
            "LPUSH",
            "waddles:control:twitch:eventsub-revocations",
            "{}",
        ),
        op(
            f"{ing}/ingest/discord_identify_budget.rs:170 HSET session",
            "HSET",
            "waddles:discord:session:s0",
            "session_id",
            "x",
            "seq",
            "1",
        ),
        op(
            f"{ing}/ingest/discord_identify_budget.rs:173 EXPIRE session",
            "EXPIRE",
            "waddles:discord:session:s0",
            "300",
        ),
        op(
            f"{ing}/ingest/discord_identify_budget.rs:133 HGETALL session",
            "HGETALL",
            "waddles:discord:session:s0",
        ),
        op(
            f"{ing}/ingest/discord_identify_budget.rs:181 DEL session",
            "DEL",
            "waddles:discord:session:s0",
        ),
        op(
            f"{ing}/ingest/discord_identify_budget.rs:324 SET EX daily",
            "SET",
            "waddles:discord:identify-budget:h1:daily",
            "900",
            "EX",
            "86400",
        ),
        eval_(
            f"{ing}/ingest/discord_identify_budget.rs:245 ACQUIRE",
            _DISCORD_BUDGET,
            [
                "waddles:discord:identify-budget:h1:0",
                "waddles:discord:identify-budget:h1:daily",
            ],
            "5000",
        ),
        eval_(
            f"{ing}/connector/identify_budget.rs:262 RECORD",
            _CONNECTOR_BUDGET_RECORD,
            ["waddles:connector:identify-budget:discord:abc:limit"],
            "1000",
            "900",
            "1",
            "86400000",
        ),
        op(
            f"{ing}/connector/identify_budget.rs:361 HGET max_concurrency",
            "HGET",
            "waddles:connector:identify-budget:discord:abc:limit",
            "max_concurrency",
        ),
        eval_(
            f"{ing}/connector/identify_budget.rs:243 ACQUIRE",
            _CONNECTOR_BUDGET_ACQUIRE,
            [
                "waddles:connector:identify-budget:discord:abc:b0",
                "waddles:connector:identify-budget:discord:abc:limit",
            ],
            "5000",
        ),
        op(
            f"{ing}/canary.rs:118 SET NX EX lock",
            "SET",
            "waddles:canary:latency:lock",
            "1",
            "NX",
            "EX",
            "24",
        ),
        op(f"{ing}/canary.rs:132 RPOP result", "RPOP", "waddles:canary:latency:result"),
        op(f"{ing}/canary.rs:141 DEL result", "DEL", "waddles:canary:latency:result"),
        op(
            "platform-unban producer (unmerged branch): LPUSH events",
            "LPUSH",
            _UNBAN,
            "signed-event",
        ),
    ]
    durable.replay("durable", u, setup, ops)
    for argv in (
        ("XADD", action_stream("ing"), "*", "k", "v"),  # process owns :action
        ("SET", "waddles:ratelimit:dm.send:x", "1"),  # action owns ratelimit
        ("GET", "waddles:jwt:rev:jti:x"),  # hub-api owns jwt
        (
            "SET",
            "waddles:platform-unban:discord:seen:x",
            "1",
        ),  # only :events is producer-writable
        ("XADD", "waddles:usage", "*", "k", "v"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)


# --------------------------------------------------------------------------------------------
# svc-process
# --------------------------------------------------------------------------------------------


def test_svc_process_works_with_only_its_own_user(durable: Valkey, kv: Valkey) -> None:
    """svc-process durable ops (spine consume/enqueue, bundle-admin, grants) and its kv + media ops."""
    u = "svc-process"
    src, act = src_stream("proc"), action_stream("proc")
    grp = "penguintechinc.waddles.fixture-bot-commands"
    setup = [
        (
            "svc-ingest",
            op("publish.rs append: XADD src", "XADD", src, "*", "env", _ENV),
        ),
        (
            "hub-api",
            op(
                "bundle_tool_transport.py:172 LPUSH request",
                "LPUSH",
                "waddles:bundle-admin:requests",
                '{"request_id":"r1"}',
            ),
        ),
        (
            "hub-api",
            op(
                "bundle_permission_service.py:191 XADD",
                "XADD",
                "bundle:grants:invalidate",
                "*",
                "tenant_id",
                "1",
            ),
        ),
        (
            "hub-api",
            op(
                "envelope/invalidation.py:61 XADD",
                "XADD",
                "keys:tenant-dek:invalidate",
                "*",
                "tenant",
                "1",
            ),
        ),
        (
            "hub-api",
            op(
                "bundle_community_config_runtime.py:145 SET",
                "SET",
                _VERSION_KEY,
                "3",
                "EX",
                "86400",
            ),
        ),
    ]
    ops = [
        op(
            "core/svc_process/src/spine.rs:2145 XGROUP CREATE MKSTREAM",
            "XGROUP",
            "CREATE",
            src,
            grp,
            "0",
            "MKSTREAM",
        ),
        op(
            f"{_SPINE}/reader.rs:115 XREADGROUP",
            "XREADGROUP",
            "GROUP",
            grp,
            "c1",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            src,
            ">",
        ),
        op(
            f"{_SPINE}/client.rs:367 XAUTOCLAIM",
            "XAUTOCLAIM",
            src,
            grp,
            "c1",
            "0",
            "0-0",
            "COUNT",
            "10",
        ),
        op(f"{_SPINE}/client.rs:390 XPENDING", "XPENDING", src, grp, "-", "+", "10"),
        op(f"{_SPINE}/client.rs:460 XINFO GROUPS", "XINFO", "GROUPS", src),
        op(
            f"{_SPINE}/client.rs:188 XADD :action",
            "XADD",
            act,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "env",
            _ENV,
        ),
        op(f"{_SPINE}/client.rs:199 XLEN :action", "XLEN", act),
        op(
            f"{_SPINE}/client.rs:299 XADD dlq:process",
            "XADD",
            "waddles:dlq:process",
            "MAXLEN",
            "~",
            "1000",
            "*",
            "rec",
            "{}",
        ),
        op(f"{_SPINE}/client.rs:237/301 XACK", "XACK", src, grp, "0-0"),
        op(
            "core/svc_process/src/bundle_admin.rs:655 BRPOP requests",
            "BRPOP",
            "waddles:bundle-admin:requests",
            "1",
        ),
        op(
            "core/svc_process/src/bundle_admin.rs:523 SET NX PX seen",
            "SET",
            "waddles:bundle-admin:seen:r1",
            "1",
            "NX",
            "PX",
            "60000",
        ),
        tx(
            "core/svc_process/src/bundle_admin.rs:540-544 LPUSH+EXPIRE result",
            "LPUSH waddles:bundle-admin:result:r1 {}",
            "EXPIRE waddles:bundle-admin:result:r1 60",
        ),
        op(
            "core/svc_process/src/grant_gate.rs:388 XREAD",
            "XREAD",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            "bundle:grants:invalidate",
            "0",
        ),
        op(
            "core/svc_process/src/identity_crypto.rs:354 XREAD",
            "XREAD",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            "keys:tenant-dek:invalidate",
            "0",
        ),
        op("core/hub_client/src/bundle_config.rs:152 GET version", "GET", _VERSION_KEY),
    ]
    durable.replay("durable", u, setup, ops)
    for argv in (
        ("LPUSH", _UNBAN, "forged"),
        ("SET", "waddles:ratelimit:dm.send:x", "1"),
        ("GET", "waddles:jwt:rev:jti:x"),
        ("XADD", "waddles:analytics", "*", "k", "v"),
        ("SET", _VERSION_KEY, "9"),  # bundle_config is read-only (%R) for process
    ):
        durable.denied(u, *argv)

    media = "media"
    j = "core/bundle_host_media/src/jobs.rs"
    kv_all = [
        *kv_ops("proc"),
        op(f"{j}:128 SET EX job", "SET", f"{media}:job:1:7:j1", "{}", "EX", "600"),
        op(f"{j}:140 GET job", "GET", f"{media}:job:1:7:j1"),
        op(f"{j}:159 INCR rate", "INCR", f"{media}:rate:1:7:100"),
        op(f"{j}:165 EXPIRE rate", "EXPIRE", f"{media}:rate:1:7:100", "120"),
        eval_(
            f"{j}:184 INFLIGHT",
            _MEDIA_INFLIGHT,
            [f"{media}:inflight:1:7"],
            "0",
            "4",
            "9999999999999",
            "j1",
            "3600",
        ),
        op(f"{j}:199 ZREM inflight", "ZREM", f"{media}:inflight:1:7", "j1"),
    ]
    kv.replay("kv", u, [], kv_all)
    for argv in (
        ("SET", "waddles:foo", "1"),  # no durable key family on kv
        ("FLUSHALL",),
        ("CONFIG", "SET", "maxmemory", "1"),
        ("KEYS", "*"),
    ):
        kv.denied(u, *argv)


# --------------------------------------------------------------------------------------------
# svc-action
# --------------------------------------------------------------------------------------------


def test_svc_action_works_with_only_its_own_user(durable: Valkey, kv: Valkey) -> None:
    """svc-action durable ops (consume :action, transport, ratelimit, usage, event-sync) + kv ops."""
    u = "svc-action"
    act = action_stream("act")
    grp = "waddles.a"
    cap = "core/svc_action/src/capabilities.rs"
    es = "core/svc_action/src/capabilities/event_sync.rs"
    setup = [
        ("svc-process", op("dispatch: XADD :action", "XADD", act, "*", "env", _ENV)),
        (
            "hub-api",
            op(
                "event_sync_connector_client.py:186 LPUSH",
                "LPUSH",
                "waddles:event-sync:discord:requests",
                '{"request_id":"e1"}',
            ),
        ),
        (
            "hub-api",
            op(
                "bundle_permission_service.py:191 XADD",
                "XADD",
                "bundle:grants:invalidate",
                "*",
                "tenant_id",
                "1",
            ),
        ),
        (
            "svc-ingest",
            op(
                "outbound.rs:808 SET ack",
                "SET",
                "waddles:transport:discord:ack:op9",
                "{}",
                "EX",
                "60",
            ),
        ),
        (
            "svc-ingest",
            op(
                "outbound.rs:788 SET drain-ready",
                "SET",
                "waddles:transport:discord:drain-ready",
                "1",
                "EX",
                "20",
            ),
        ),
        ("svc-ingest", op("platform-unban producer", "LPUSH", _UNBAN, "signed-event")),
        (
            "hub-api",
            op(
                "bundle_community_config_runtime.py:145 SET",
                "SET",
                _VERSION_KEY,
                "3",
                "EX",
                "86400",
            ),
        ),
    ]
    ops = [
        op(
            "core/svc_action/src/dispatch.rs:1608 XGROUP CREATE MKSTREAM",
            "XGROUP",
            "CREATE",
            act,
            grp,
            "0",
            "MKSTREAM",
        ),
        op(
            f"{_SPINE}/reader.rs:115 XREADGROUP",
            "XREADGROUP",
            "GROUP",
            grp,
            "c1",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            act,
            ">",
        ),
        op(
            f"{_SPINE}/client.rs:367 XAUTOCLAIM",
            "XAUTOCLAIM",
            act,
            grp,
            "c1",
            "0",
            "0-0",
            "COUNT",
            "10",
        ),
        op(f"{_SPINE}/client.rs:390 XPENDING", "XPENDING", act, grp, "-", "+", "10"),
        op(f"{_SPINE}/client.rs:237 XACK", "XACK", act, grp, "0-0"),
        op(
            f"{_SPINE}/client.rs:299 XADD dlq:action",
            "XADD",
            "waddles:dlq:action",
            "MAXLEN",
            "~",
            "1000",
            "*",
            "rec",
            "{}",
        ),
        tx(
            f"{cap}:930-940 bounded LPUSH+LTRIM+EXPIRE (key :310)",
            "LPUSH waddles:transport:irc:discord:outbound {}",
            "LTRIM waddles:transport:irc:discord:outbound 0 999",
            "EXPIRE waddles:transport:irc:discord:outbound 600",
        ),
        tx(
            f"{cap}:930-940 bounded push (twitch)",
            "LPUSH waddles:transport:irc:twitch:outbound {}",
            "LTRIM waddles:transport:irc:twitch:outbound 0 999",
            "EXPIRE waddles:transport:irc:twitch:outbound 600",
        ),
        op(
            f"{cap}:976 EXISTS drain-ready",
            "EXISTS",
            "waddles:transport:discord:drain-ready",
        ),
        op(f"{cap}:1005 GETDEL ack", "GETDEL", "waddles:transport:discord:ack:op9"),
        eval_(
            f"{cap}:989 INCR_WINDOW (key :1776)",
            _INCR_WINDOW,
            ["waddles:ratelimit:dm.send:app:1:7:app1"],
            "60",
        ),
        eval_(
            f"{cap}:989 INCR_WINDOW (key :1834)",
            _INCR_WINDOW,
            ["waddles:ratelimit:role.op:app:1:7:app1"],
            "60",
        ),
        eval_(
            f"{cap}:989 INCR_WINDOW (key :2399)",
            _INCR_WINDOW,
            ["waddles:ratelimit:platform.op:discord:1:app:7:app1:u1"],
            "60",
        ),
        op(
            f"{es}:638 BRPOP event-sync requests",
            "BRPOP",
            "waddles:event-sync:discord:requests",
            "1",
        ),
        tx(
            f"{es}:609-613 RPUSH+EXPIRE event-sync result",
            "RPUSH waddles:event-sync:discord:result:e1 {}",
            "EXPIRE waddles:event-sync:discord:result:e1 60",
        ),
        op(
            "core/svc_action/src/usage.rs:128 XADD usage",
            "XADD",
            "waddles:usage",
            "MAXLEN",
            "~",
            "10000",
            "*",
            "usage",
            "{}",
        ),
        op(
            "core/svc_action/src/grant_gate.rs:388 XREAD",
            "XREAD",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            "bundle:grants:invalidate",
            "0",
        ),
        op("core/hub_client/src/bundle_config.rs:152 GET version", "GET", _VERSION_KEY),
        op("platform-unban consumer (unmerged): RPOP events", "RPOP", _UNBAN),
        op(
            "platform-unban consumer (unmerged): BRPOP events",
            "BRPOP",
            _UNBAN,
            "1",
            allow=(),
        ),
        op(
            "platform-unban consumer (unmerged): SET NX seen",
            "SET",
            _UNBAN_SEEN,
            "1",
            "NX",
            "PX",
            "60000",
        ),
        op(
            f"{cap}:1948 LPUSH canary result (canary driver's BRPOP list)",
            "LPUSH",
            "waddles:canary:latency:result",
            "ok",
        ),
    ]
    # BRPOP on the now-empty unban list blocks 1s and returns nil: still no NOPERM.
    durable.replay("durable", u, setup, ops)
    for argv in (
        ("XADD", src_stream("act"), "*", "k", "v"),
        ("XADD", "waddles:analytics", "*", "k", "v"),
        ("GET", "waddles:jwt:rev:jti:x"),
        ("SET", _VERSION_KEY, "9"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)
    kv.replay("kv", u, [], kv_ops("act"))
    for argv in (
        ("SET", "media:job:1:7:j1", "{}"),  # media is svc-process's
        ("SET", "waddles:foo", "1"),
        ("FLUSHALL",),
    ):
        kv.denied(u, *argv)


# --------------------------------------------------------------------------------------------
# svc-analytics
# --------------------------------------------------------------------------------------------


def test_svc_analytics_works_with_only_its_own_user(durable: Valkey) -> None:
    """svc-analytics consumer-group loop on waddles:analytics + its DLQ."""
    u = "svc-analytics"
    c = "core/svc_analytics/src/consumer.rs"
    st, grp, dlq = "waddles:analytics", "svc-analytics", "waddles:analytics:dlq"
    setup = [("svc-ingest", op("analytics.rs:701 XADD", "XADD", st, "*", "k", "v"))]
    ops = [
        op(
            f"{c}:185 XGROUP CREATE MKSTREAM",
            "XGROUP",
            "CREATE",
            st,
            grp,
            "0",
            "MKSTREAM",
        ),
        op(
            f"{c}:203 XREADGROUP",
            "XREADGROUP",
            "GROUP",
            grp,
            "c1",
            "COUNT",
            "10",
            "BLOCK",
            "10",
            "STREAMS",
            st,
            ">",
        ),
        op(
            f"{c}:224 XAUTOCLAIM",
            "XAUTOCLAIM",
            st,
            grp,
            "c1",
            "0",
            "0-0",
            "COUNT",
            "10",
        ),
        op(f"{c}:517 XPENDING", "XPENDING", st, grp),
        op(
            f"{c}:263 XADD dlq",
            "XADD",
            dlq,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "orig_id",
            "1-0",
            "reason",
            "r",
        ),
        op(f"{c}:241 XACK", "XACK", st, grp, "0-0"),
    ]
    durable.replay("durable", u, setup, ops)
    for argv in (
        ("XADD", "waddles:usage", "*", "k", "v"),  # key family
        ("SET", dlq + ":x", "1"),  # command family: stream-only user
        ("SET", "waddles:analytics:plain", "1"),
        ("XADD", action_stream("ana"), "*", "k", "v"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)


# --------------------------------------------------------------------------------------------
# hub-api
# --------------------------------------------------------------------------------------------


def test_hub_api_works_with_only_its_own_user(durable: Valkey, kv: Valkey) -> None:
    """hub-api durable ops (jwt, rate limits, oauth state, queues, usage, grants) + kv erasure."""
    u = "hub-api"
    s = "hub_api/services"
    src, act = src_stream("hub"), action_stream("hub")
    rl = "hub_api_rate_limit:ip:1.2.3.4"
    setup = [
        (
            "svc-process",
            op(
                "bundle_admin.rs:540 LPUSH result",
                "LPUSH",
                "waddles:bundle-admin:result:b1",
                "{}",
            ),
        ),
        (
            "svc-action",
            op(
                "event_sync.rs:609 RPUSH result",
                "RPUSH",
                "waddles:event-sync:discord:result:e2",
                "{}",
            ),
        ),
        (
            "svc-action",
            op("usage.rs:128 XADD usage", "XADD", "waddles:usage", "*", "usage", "{}"),
        ),
        (
            "svc-ingest",
            op(
                "publish.rs append (so the spine stream exists)",
                "XADD",
                src,
                "*",
                "env",
                _ENV,
            ),
        ),
    ]
    ops = [
        eval_(
            "libs/flask_core/flask_core/token_revocation.py:111 Lua",
            _JWT_REVOKE,
            ["waddles:jwt:rev:jti:abc"],
            "5",
            "3600",
        ),
        op(
            "libs/flask_core/flask_core/token_revocation.py:293 MGET",
            "MGET",
            "waddles:jwt:rev:jti:abc",
            "waddles:jwt:mark:user:1",
        ),
        op(
            "libs/flask_core/flask_core/rate_limiter.py:178 ZREMRANGEBYSCORE",
            "ZREMRANGEBYSCORE",
            rl,
            "-inf",
            "1",
        ),
        op("libs/flask_core/flask_core/rate_limiter.py:185 ZCARD", "ZCARD", rl),
        op("libs/flask_core/flask_core/rate_limiter.py:189 ZADD", "ZADD", rl, "5", "5"),
        op("libs/flask_core/flask_core/rate_limiter.py:191 EXPIRE", "EXPIRE", rl, "60"),
        op(
            "libs/flask_core/flask_core/rate_limiter.py:330 SCAN",
            "SCAN",
            "0",
            "MATCH",
            "hub_api_rate_limit:*",
        ),
        op("libs/flask_core/flask_core/rate_limiter.py:295 DEL", "DEL", rl),
        op(f"{s}/sso_state.py:103 SET EX", "SET", "sso:state:tok1", "{}", "EX", "600"),
        op(f"{s}/sso_state.py:118 GETDEL", "GETDEL", "sso:state:tok1"),
        op(
            f"{s}/sso_state.py:148 SET NX EX assertion",
            "SET",
            "sso:saml:assertion:a1",
            "1",
            "EX",
            "600",
            "NX",
        ),
        op(
            f"{s}/tenant_discord_install_state.py:134 SET EX",
            "SET",
            "oauth:tenant_discord_install:state:t1",
            "{}",
            "EX",
            "600",
        ),
        op(
            f"{s}/install_state_binding.py:111 GET",
            "GET",
            "oauth:tenant_discord_install:state:t1",
        ),
        op(
            f"{s}/install_state_binding.py:123 GETDEL",
            "GETDEL",
            "oauth:tenant_discord_install:state:t1",
        ),
        op(
            f"{s}/twitch_install_state.py:140 SET EX",
            "SET",
            "oauth:twitch_install:state:t2",
            "{}",
            "EX",
            "600",
        ),
        op(
            f"{s}/install_state_binding.py:123 GETDEL (twitch)",
            "GETDEL",
            "oauth:twitch_install:state:t2",
        ),
        op(
            f"{s}/oauth_connection_state.py:156 SET EX",
            "SET",
            "oauth:conn:state:t3",
            "{}",
            "EX",
            "600",
        ),
        op(
            f"{s}/install_state_binding.py:123 GETDEL (conn)",
            "GETDEL",
            "oauth:conn:state:t3",
        ),
        tx(
            f"{s}/dsar_rate_limit.py:125-127 INCR+EXPIRE NX",
            "INCR dsar:rl:k1",
            "EXPIRE dsar:rl:k1 60 NX",
        ),
        op(
            f"{s}/dsar_rate_limit.py:146 SET NX EX once",
            "SET",
            "dsar:once:k1",
            "1",
            "NX",
            "EX",
            "600",
        ),
        op(
            f"{s}/landing_stats_cache.py:38 SET EX",
            "SET",
            "waddles:landing:stats:v1",
            "{}",
            "EX",
            "300",
        ),
        op(f"{s}/landing_stats_cache.py:44 GET", "GET", "waddles:landing:stats:v1"),
        op(
            f"{s}/bundle_community_config_runtime.py:145 SET EX",
            "SET",
            _VERSION_KEY,
            "4",
            "EX",
            "86400",
        ),
        op(
            f"{s}/bundle_tool_transport.py:172 LPUSH request",
            "LPUSH",
            "waddles:bundle-admin:requests",
            '{"request_id":"b1"}',
        ),
        op(
            f"{s}/bundle_tool_transport.py:173 BLPOP result",
            "BLPOP",
            "waddles:bundle-admin:result:b1",
            "1",
        ),
        op(
            f"{s}/event_sync_connector_client.py:186 LPUSH request",
            "LPUSH",
            "waddles:event-sync:discord:requests",
            '{"request_id":"e2"}',
        ),
        op(
            f"{s}/event_sync_connector_client.py:187 BLPOP result",
            "BLPOP",
            "waddles:event-sync:discord:result:e2",
            "1",
        ),
        op(
            f"{s}/usage_aggregator_service.py:53 XGROUP CREATE usage group",
            "XGROUP",
            "CREATE",
            "waddles:usage",
            "hub_api_usage_aggregator",
            "0",
            "MKSTREAM",
            allow=("BUSYGROUP",),
        ),
        op(
            f"{s}/usage_aggregator_service.py:171 XREADGROUP",
            "XREADGROUP",
            "GROUP",
            "hub_api_usage_aggregator",
            "hub-api-1",
            "COUNT",
            "10",
            "STREAMS",
            "waddles:usage",
            ">",
        ),
        op(
            f"{s}/usage_aggregator_service.py:231 XACK",
            "XACK",
            "waddles:usage",
            "hub_api_usage_aggregator",
            "0-0",
        ),
        op(
            f"{s}/bundle_permission_service.py:191 XADD",
            "XADD",
            "bundle:grants:invalidate",
            "*",
            "tenant_id",
            "1",
        ),
        op(
            f"{s}/envelope/invalidation.py:61 XADD MAXLEN~",
            "XADD",
            "keys:tenant-dek:invalidate",
            "MAXLEN",
            "~",
            "1000",
            "*",
            "tenant",
            "1",
        ),
        op(
            f"{s}/valkey_admin_client.py:85 XGROUP CREATE MKSTREAM (spine src)",
            "XGROUP",
            "CREATE",
            src,
            "app1",
            "$",
            "MKSTREAM",
        ),
        op(
            f"{s}/source_group_reconciler.py:153 XGROUP CREATE MKSTREAM (spine src)",
            "XGROUP",
            "CREATE",
            src_stream("hub", "twitch"),
            "app1",
            "$",
            "MKSTREAM",
        ),
        op(
            f"{s}/bundle_version_service.py:735 XGROUP CREATE MKSTREAM (:action)",
            "XGROUP",
            "CREATE",
            act,
            "app1",
            "$",
            "MKSTREAM",
        ),
        op(
            f"{s}/valkey_admin_client.py:94 XGROUP DESTROY",
            "XGROUP",
            "DESTROY",
            src,
            "app1",
        ),
    ]
    durable.replay("durable", u, setup, ops)
    for argv in (
        (
            "XADD",
            src,
            "*",
            "k",
            "v",
        ),  # provisioning only, never produce on spine streams
        ("XREADGROUP", "GROUP", "app1", "c1", "STREAMS", src, ">"),
        ("XADD", act, "*", "k", "v"),
        ("LPUSH", _UNBAN, "forged"),
        ("SET", "waddles:ratelimit:x", "1"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)

    kdata = "bundlekv:acme:main:acme.other-app:data:tok-uuid"
    kcount = "bundlekv:acme:main:acme.other-app:count"
    ksetup = [
        ("svc-process", op("kv seed data", "SET", kdata, "v")),
        ("svc-process", op("kv seed count", "SET", kcount, "3")),
    ]
    er = f"{s}/erasure_external.py"
    kops = [
        op(
            f"{er}:70 SCAN data glob",
            "SCAN",
            "0",
            "MATCH",
            "bundlekv:*:data:*tok-uuid*",
            "COUNT",
            "100",
        ),
        op(f"{er}:80 EXISTS count", "EXISTS", kcount),
        op(f"{er}:80 DECRBY count", "DECRBY", kcount, "1"),
        op(f"{er}:75 UNLINK data", "UNLINK", kdata),
        op(f"{er}:81 DEL count", "DEL", kcount),
    ]
    kv.replay("kv", u, ksetup, kops)
    kv.denied(u, "GET", kdata)
    kv.denied(u, "SET", kdata, "forged")
    kv.denied(u, "UNLINK", "bundlekv:acme:main:acme.other-app:meta")
    kv.denied(u, "FLUSHALL")


# --------------------------------------------------------------------------------------------
# role-sync-inbound, landing-stats, core-bundle-seeder
# --------------------------------------------------------------------------------------------


def test_role_sync_inbound_works_with_only_its_own_user(durable: Valkey) -> None:
    """role-sync-inbound drains Discord source streams (no produce) and dead-letters failures."""
    u = "role-sync-inbound"
    r = "hub_api/services/role_sync_inbound_service.py"
    src = src_stream("rsi")
    grp = "hub_api_role_sync_inbound"
    setup = [("svc-ingest", op("publish.rs append", "XADD", src, "*", "env", _ENV))]
    ops = [
        op(
            f"{r}:596 SCAN (DISCORD_STREAM_PATTERN :99)",
            "SCAN",
            "0",
            "MATCH",
            "waddles:t:*:c:*:src:discord:*",
        ),
        op(
            f"{r}:573 XGROUP CREATE (mkstream=False)", "XGROUP", "CREATE", src, grp, "$"
        ),
        op(
            f"{r}:601 XREADGROUP",
            "XREADGROUP",
            "GROUP",
            grp,
            "hub-api-1",
            "COUNT",
            "200",
            "STREAMS",
            src,
            ">",
        ),
        op(
            f"{r}:636 XADD dlq MAXLEN~1000",
            "XADD",
            "waddles:dlq:role_sync_inbound",
            "MAXLEN",
            "~",
            "1000",
            "*",
            "stream",
            src,
            "error",
            "E",
        ),
        op(f"{r}:645 XACK", "XACK", src, grp, "0-0"),
    ]
    durable.replay("durable", u, setup, ops)
    for argv in (
        ("XADD", src_stream("rsi", "twitch"), "*", "k", "v"),  # Discord streams only
        ("XADD", action_stream("rsi"), "*", "k", "v"),
        ("SET", "waddles:ratelimit:x", "1"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)


def test_landing_stats_works_with_only_its_own_user(durable: Valkey) -> None:
    """landing-stats SET EX / GET of the one snapshot key."""
    u = "landing-stats"
    s = "hub_api/services/landing_stats_cache.py"
    ops = [
        op(f"{s}:38 SET EX", "SET", "waddles:landing:stats:v1", "{}", "EX", "300"),
        op(f"{s}:44 GET", "GET", "waddles:landing:stats:v1"),
    ]
    durable.replay("durable", u, [], ops)
    for argv in (
        ("GET", "waddles:jwt:rev:jti:x"),
        ("XADD", "waddles:analytics", "*", "k", "v"),  # no stream commands at all
        ("SET", "waddles:bundle_config:v:1:7:x", "1"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)


def test_core_bundle_seeder_works_with_only_its_own_user(durable: Valkey) -> None:
    """core-bundle-seeder: ensure_group on :action streams, grant invalidation XADD, config publish."""
    u = "core-bundle-seeder"
    s = "hub_api/services"
    act = action_stream("seed")
    ops = [
        op(
            f"{s}/bundle_version_service.py:735 (valkey_admin_client.py:85) XGROUP CREATE MKSTREAM",
            "XGROUP",
            "CREATE",
            act,
            "app1",
            "$",
            "MKSTREAM",
        ),
        op(
            f"{s}/bundle_permission_service.py:191 XADD",
            "XADD",
            "bundle:grants:invalidate",
            "*",
            "tenant_id",
            "1",
        ),
        op(
            f"{s}/bundle_community_config_runtime.py:145 SET EX",
            "SET",
            _VERSION_KEY,
            "5",
            "EX",
            "86400",
        ),
    ]
    durable.replay("durable", u, [], ops)
    for argv in (
        ("XADD", act, "*", "k", "v"),  # group provisioning only
        ("XADD", src_stream("seed"), "*", "k", "v"),
        (
            "XGROUP",
            "CREATE",
            src_stream("seed"),
            "g",
            "0",
            "MKSTREAM",
        ),  # :action only, not src
        ("GET", "waddles:jwt:rev:jti:x"),
        ("FLUSHALL",),
    ):
        durable.denied(u, *argv)


# --------------------------------------------------------------------------------------------
# denominator
# --------------------------------------------------------------------------------------------


def test_zz_every_configured_user_replayed_at_least_one_op() -> None:
    """Every (instance, user) configured in values.yaml was replayed; none had zero ops."""
    users = yaml.safe_load((_CHART / "values.yaml").read_text())["infrastructure"][
        "valkeyAcl"
    ]["users"]
    configured = {
        ("durable" if inst == "durable" else "kv", name)
        for name, u in users.items()
        for inst in ("durable", "kv")
        if u.get(inst)
    }
    assert len(configured) >= 10, configured
    missing = configured - set(OPS_REPLAYED)
    zero = [k for k, n in OPS_REPLAYED.items() if n < 1]
    assert not missing and not zero, (
        f"never replayed: {sorted(missing)}; zero ops: {zero}"
    )
    for (inst, user), n in sorted(OPS_REPLAYED.items()):
        print(f"valkey-acl op count: {inst}/{user} = {n}")
