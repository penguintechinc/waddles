# waddles.core.example.game-servers

App bundle (Rust, `world stage-next-tools`) giving communities game-server chat commands.
Pure orchestration: it holds no address, no secret and no socket; every action is a tool call to
the `waddles.core.service.gameserver` SERVICE bundle (app -> service only), which enforces
registration, allowlists, scope, rate limits and the audit log.

## Commands

| Command | Who | Does |
|---|---|---|
| `!server` / `!server status [name]` | everyone | status of the named server, or one line per enabled server (max 5) |
| `!server players [name]` | everyone | count + player names (A2S players / Minecraft sample) |
| `!server list` | everyone | enabled server aliases |
| `!rcon <name> <command...>` | moderators | runs an ALLOWLISTED command; output echoed as one line (<= 400 chars) |

`name` is a typed alias of the server label (lowercase, spaces to `-`), resolved to the service's server
UUID at command time via `gameserver.server.list`; omitted when exactly one server is enabled.
The app never decides authorization: a non-moderator `!rcon` is refused by the service from the verified actor.
`!rcon` uses the message id as the idempotency key, so a redelivered message cannot re-run a command.

## Scheduled status posts

Config (`config_schema`): `status_post_enabled`, `status_post_platform` (twitch|discord),
`status_post_channel_id`, `status_post_servers` (aliases; empty = all). Enabled-but-incomplete config is
logged loudly, never silently skipped. Fired by a `schedule.tick` event (permission `platform.scheduled`).

## Admin path (generic bundle-admin gateway, no game-server routes in hub-api)

The gateway maps admin actions 1:1 onto service tools under the admin's verified scope (the tools are
`required_scope: community:admin`, `dangerous`, audited):

| Admin action | Service tool |
|---|---|
| register / update server + allowlist | `gameserver.server.register` |
| remove server | `gameserver.server.remove` |
| list / get | `gameserver.server.list` / `.get` |
| RCON audit log | `gameserver.audit.list` |

Host:port + RCON password are entered through the hub-api connection gateway (host-side secret), never here.

## Parity

| Legacy | Here |
|---|---|
| `bot.py` rcon servers list/CRUD/test/policy/access-log | service `server.*`, `audit.list` (test = `status.query`) |
| `bot.py` rcon `/command` | `gameserver.rcon.exec` / `!rcon` |
| `bot.py` rcon info status/players, `server_status` module | `!server status|players`, `status.query`, scheduled posts |
| kick/ban/channels/move/message (TeamSpeak/Mumble) | deferred (not game RCON) |

## Needs from S0 / other agents

See the service README ("Needs from S0": `gameserver-net` import) and: delivery contract for
`schedule.tick` scheduled events; generic bundle-admin gateway (#1099 P0).

## Contract consumed (fixture-tested in `logic.rs`)

`gameserver.server.list {} -> {servers:[{server,label,enabled,...}]}`;
`gameserver.status.query {server, include_players} -> {players,max_players,map,version,player_names}`;
`gameserver.rcon.exec {server, command} (+idempotency key) -> {output, truncated}`.

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked
cargo test && cargo clippy --all-targets -- -D warnings
```
