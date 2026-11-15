# bundle-host-gameserver-net

Host side of the `gameserver-net` WIT interface (`wit/waddle-bundle/tool.wit`, `world stage-tools` only):
guarded RCON / TCP / UDP access to **admin-registered** game-server connections. A path dependency of
`core/svc_process`. Spec for the guest contract: `bundles/rust/gameserver/README.md`.

```
guest --rcon-open(server uuid)--> executor --host-call(db,"gameserver_net.*")--> svc_process::capabilities::gameserver_net
   1. first-party core namespace only (waddles.core.*)
   2. gate: `gameserver.net` (dangerous, global-admin approved)
   3. wiring + flag `waddles.bundle-gameserver-net` (default OFF; fail-loud not_implemented / feature_disabled)
   4. GameNet: directory lookup (tenant+community scoped) -> rate limit -> SSRF guard on RESOLVED IP
      -> concurrency permit -> connect to that IP -> host-side RCON login -> budgeted session
```

| Control | Where |
|---|---|
| Guest names a connection by UUID only; host/port/password resolved host-side, scoped to the invoking tenant AND community | `directory.rs` |
| SSRF guard on every resolved IP (private, loopback, link-local, CGNAT, metadata, multicast, ULA, 6to4/Teredo, v4-embedded v6, reserved); resolve once, connect to that IP | `guard.rs` (reuses `bundle_host_http::egress::is_forbidden_address`) |
| RCON login performed by the host; password zeroizing, redacted `Debug`, absent from errors/audit/results | `rcon.rs`, `secret.rs` |
| Per-connection + per-community rate limits, per-community + global concurrency, connect 3 s / read 5 s | `limits.rs`, `config.rs` |
| Byte budgets (RCON 64 KiB in / 4 KiB out, TCP 128 KiB in / 1 KiB out, UDP 1400 B in / 512 B out); command <= 512 B, single printable line, no re-auth, whole well-formed packets only | `net.rs`, `rcon.rs` |
| Audit of every open / RCON command / close: server id, command text, sizes, outcome -- never password, host or IP | `audit.rs` |
| Loopback allowed ONLY with the `test-loopback` cargo feature (dev-dependency of svc_process; absent from release builds) | `config.rs` |

Tests: `cargo test --features test-loopback` (unit + real loopback RCON/TCP/UDP servers + normal-mode
refusal of every private class + directory scoping).
