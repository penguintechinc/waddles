# waddles.core.service.gameserver

Always-on `kind: service` bundle (Rust, `world stage-tools`) that owns game-server tooling:
Source RCON + Minecraft RCON command execution and A2S / Minecraft server-list status.
The `game-servers` app bundle (and nothing else) reaches it through gated tool calls.

## Security model (this reaches arbitrary customer hosts)

| Control | Where |
|---|---|
| Address + RCON password are **HOST-side connection secrets**; the guest names a server only by UUID and never sees either | host `gameserver-net` (see "Needs from S0") |
| SSRF guard on the RESOLVED address (loopback / private / link-local / metadata / cluster CIDRs blocked), same guard family as `bundle_host_http` `GuardedClient` | host |
| Per-server registration required (`gameserver.server.register`, admin) before any network tool works | `logic.rs` |
| `rcon.exec` is deny-by-default: command must match the server's allowlist (exact line, or `verb *`; bare `*` refused); single printable line, <= 256 bytes | `logic.rs` |
| Verified moderator/admin scope (`ToolContext`, never from args) | `logic.rs` |
| Mandatory idempotency key; replay refused (`duplicate_request`) so a redelivered `kick`/`ban` never runs twice | `logic.rs` |
| Per-community (30/min exec, 60/min status) AND per-server (default 10/min, max 60) fixed-window limits | `logic.rs` |
| Audit of EVERY exec attempt (allowed or refused): server UUID, actor UUID, command text, outcome code; never output, never a secret. Log line + bounded kv ring (`gameserver.audit.list`, admin) | `logic.rs` |
| Output cap per server (default 2 KiB, max 8 KiB), control chars and Minecraft color codes stripped | `proto.rs` |
| No capability => every network tool fails loud (`target-unavailable`); no fallback, no raw sockets | `net.rs` |

## Tools

| Tool | Access | Gate |
|---|---|---|
| `gameserver.rcon.exec {server, command}` | write, dangerous | moderator or admin |
| `gameserver.status.query {server, include_players?}` | read | any member |
| `gameserver.server.list {}` | read | any member (label/game/enabled only) |
| `gameserver.server.get {server}` | read | any member; allowlist/caps visible to moderator+ |
| `gameserver.server.register {server, label?, game?, enabled?, allowlist?, max_output?, rate_per_min?}` | write, dangerous | admin (`required_scope: community:admin`); upsert |
| `gameserver.server.remove {server}` | write, dangerous | admin |
| `gameserver.audit.list {limit?}` | read | admin |

`server` is the UUID of the host-side `gameserver` connection (created through the hub-api
gateway together with host:port + password); `register` binds non-secret policy to it.
Kick/ban/message/move for TeamSpeak/Mumble voice servers are not part of this bundle (deferred).

Config lives in the bundle kv (community-scoped, keys contain no `:`): `servers`, `server.<uuid>`,
`rl.*`, `idem.*`, `audit.*`. The registry index read-modify-write is not CAS; registration is an
admin-only, low-contention action.

## Needs from S0 (host seams) -- the network tools fail loud until these exist

A new WIT interface `gameserver-net` imported by `world stage-tools` (+ catalog permission
`gameserver.net`, `dangerous`, global-admin approved, first-party core only):

```wit
interface gameserver-net {
  variant error { denied(string), unreachable, timeout, auth-failed, too-large, backend(string) }
  enum scheme { source-rcon, minecraft-rcon }
  resource tcp { write-all: func(bytes: list<u8>) -> result<_, error>;
                 read: func(max: u32) -> result<list<u8>, error>; }   // empty = peer closed
  rcon-open: func(server: string, scheme: scheme) -> result<tcp, error>;  // host logs in with the secret
  tcp-open:  func(server: string) -> result<tcp, error>;                  // unauthenticated (MC ping)
  udp-exchange: func(server: string, payload: list<u8>, max-response: u32) -> result<list<u8>, error>;
}
```

Host obligations: resolve `server` to a connection owned by the invocation's community+tenant (else
`denied("not_registered")`); resolve DNS once, run the egress guard on the resolved IP and connect to
that IP (no re-resolve); fixed per-call deadlines (connect 3 s, read 5 s) and byte budgets (RCON 64 KiB,
UDP <= 1400 B, MC status 128 KiB); for `rcon-open` perform the scheme's login (Source: type-3 auth packet,
expect type-2 with the same id; Minecraft: same framing) and map a rejected login to `auth-failed`;
never return the password/address in any error string; audit every open (tenant, community, connection id).
Also needed: hub-api gateway endpoints to create/test/delete a `gameserver` connection
(host, port, rcon password stored as a connection secret via the existing connection resolver).
The guest side is `net::GameNet` (`src/net.rs`); swapping `UnavailableNet` for the generated-import
adapter in `lib.rs` is the only change once the import exists.

Minecraft status ping sends an empty server-address/port in the handshake (the guest does not know the
target); vanilla servers ignore it, host-header-routing proxies may not.

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked
cargo test && cargo clippy --all-targets -- -D warnings
```

Tests exercise real wire bytes: a loopback TCP Source-RCON server (multi-packet + sentinel), A2S
challenge/info/players datagrams, Minecraft status frames, plus the policy/limit/audit paths.
