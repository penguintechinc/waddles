# superpenguin-ffa

C# port of PenguinTwitchBot `PastyGames/FFA.cs` (MIT, used with permission). `!ffa` opens a join window (180 s), every `!ffa`
pays the 100 fee and joins, a double join is refused, one entrant gets the fee back, otherwise one uniformly random entrant wins
`entrants * fee`; 300 s cooldown; upstream chat wording kept (including "you are to late").

## Architecture

| Concern | Where |
|---|---|
| Rules, odds, window, cooldown, messages | this app (`FfaLogic.cs`) |
| Balance / debit / credit | economy SERVICE tools `get_balance`, `adjust_balance` (I/O only), `points_view` for the currency alias |
| Join window | SDK `TickScheduler` emulation (no host timer exists) |
| State | host `kv` (`storage.kv`): record, one key per entrant slot, claims, cooldown, per-event money plan |

## Money safety

- A fee leaves a balance only on the payer's own command (actor-only debit); the burned fees are the pot, paid to the winner by a mint of the same amount (supply-neutral).
- Each event writes a write-ahead plan (winner already rolled) before its first economy call; a redelivery re-executes the same plan and the host de-duplicates each call.
- Entries claim their own slot atomically; no shared list is rewritten (kv has no CAS). Settlement takes an atomic claim; a join whose game was settled first is refunded.

## Deviations from upstream (documented)

- No timer: the window is settled when the NEXT `!ffa` arrives after the deadline; that command is answered "too late" and never joins.
  A settlement defers up to 5 minutes while a join is in flight. Fees of a game nobody settles within 24 h need an operator.
- Max 30 entrants (kv 64-ops-per-invoke budget). One reply per event (no separate broadcast line).
- The upstream "default command event" trigger is not ported (no event-trigger capability).
- The host does not yet forward per-community settings, so upstream defaults apply (join time 180 s per the code, not the 60 s settings-page default).

## Overlay / media assets

None. Upstream PenguinTwitchBot (revision `df27ab1a`) ships no images, sounds or overlay HTML/CSS/JS for FFA: its `wwwroot/`
holds only generic alert/other-game pages (`alerts.html` has no FFA reference) and the FFA command is chat-only. There is
therefore nothing MIT-covered to package, and this bundle declares no static assets and no svc-presentation component.

## Build and test

```bash
make test-superpenguin-ffa            # xUnit + 90% line-coverage floor (tests in tests/)
make build-superpenguin-ffa-bundle    # pinned Dockerfile -> /tmp/superpenguin-ffa-out/superpenguin_ffa.wasm
make test-superpenguin-ports-e2e      # real WASM components through the production executor
```

Gated behind PostHog flag `waddles.command-superpenguin-ffa` (default OFF). Attribution: author `github/psychoboy`, collection
`superpenguintv`, MIT notice verbatim in `bundle.yaml` and `LICENSE`. See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
