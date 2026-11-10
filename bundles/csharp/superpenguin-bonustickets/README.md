# superpenguin-bonustickets (C#)

`!bonus` and `!addactive`, ported from [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
(`TicketGames/BonusTickets.cs`, `BonusTicketsStreamStartedHandler.cs`, `TicketGames/AddActive.cs`; MIT, notice verbatim in
`bundle.yaml`). Third-party app bundle: `provider: thirdparty`, author `github/psychoboy`, collection `superpenguintv`,
not a first-party core row. This is the **event-award policy**; the economy service owns the ledger.

| | |
|---|---|
| App id | `waddles.integrations.superpenguin.bonustickets` |
| Flag (default OFF) | `waddles.command-superpenguin-bonustickets` |
| World | `stage-next-tools` (C# NativeAOT-LLVM component) |
| Consumes | `chat.message` (twitch, discord): every line, to track who is active |
| Economy | service tools `points_view` (read), `adjust_balance` (write, retry-keyed); no WIT economy grant |
| State | kv only, community-scoped, keys use `.` |

## Commands

| Command | Who | Behaviour |
|---|---|---|
| `!bonus` | anyone linked | one claim per UTC day, award uniform in 25..50 (upstream defaults); a second claim is silently ignored |
| `!addactive <amount>` | moderator/broadcaster | adds `min(amount, 100)` to a pool; 5 s after the last add the pool is paid to everyone who chatted in the last 10 min |

## Idempotency and money

* `!bonus`: the roll and claim are recorded under the event replay key (message id + platform + actor + command) before the
  single `adjust_balance` credit; a redelivered event re-sends the identical credit (the host de-duplicates per key) and reprints
  the same line. A blank message id is refused.
* Settlement: the frozen payout (amount, recipients) lives in kv; each event pays one chunk of at most 20 recipients, and the chunk
  is recorded under the event key before any credit so a redelivery replays exactly that chunk. A recipient who left the community
  or hit a mint cap is skipped and logged; `denied`/`unavailable` fail loudly (the event is retried).
* "Active" users are tracked by this bundle in 16 roster shards (a chatter is rewritten at most once a minute); bots (`is_bot`
  badge) are never recipients. No host capability lists active users.

## Deviations and limits (none silent)

* Upstream resets `!bonus` per **stream** and refuses while offline; no stream-online capability exists in any WIT world, so it
  resets per UTC day and the offline gate is dropped.
* The pool settles only when another chat event reaches the bundle (no timer exists for app bundles); the announcement goes to that
  event's channel.
* kv has no compare-and-swap: simultaneous `!addactive` calls or roster writes can lose an update (under-pays, never over-pays).
* Moderator status comes from the `is_mod`/`is_broadcaster` payload badges; absent badges deny (fail-closed). App config JSON is not
  forwarded by the host yet, so award bounds, delay and window use the constants documented above.

## Test and build

```bash
make test-superpenguin-bonustickets           # xUnit + >=90% line coverage floor
make build-superpenguin-bonustickets-bundle   # real WASM component, export check
python3 -I scripts/gen-superpenguin-manifests.py bonustickets   # regenerate bundle.yaml + hub-manifest.yaml
```

No overlay component: upstream shows no visual for these games.
