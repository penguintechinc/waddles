# superpenguin-heist

C# port of PenguinTwitchBot `PastyGames/Heist.cs` (MIT, used with permission). `!heist AMOUNT|ALL|MAX|N%` opens a 300 s crew;
each entrant stakes their own bet (min 10, max 500); each then survives with 60% (a 1..100 roll over 40); survivors are paid
`bet + round(bet * 1.5)`, caught entrants lose the bet; 300 s cooldown; upstream chat wording kept.

## Architecture

| Concern | Where |
|---|---|
| Bet bounds, odds, payouts, window, cooldown, messages | this app (`HeistLogic.cs`) |
| Balance / debit / credit | economy SERVICE tools `get_balance`, `adjust_balance` (I/O only), `points_view` for the alias |
| Join window | SDK `TickScheduler` emulation |
| State | host `kv`: record, per-entrant slot keys (with bet), claims, cooldown, per-event money plan |

## Money safety

- Bets leave a balance only on the payer's own command. Every survival roll is made once, inside the write-ahead plan, before any credit; a redelivery repeats the same fates and the host de-duplicates each credit.
- Survivor payouts are mints (upstream pays 2.5x the bet) subject to the economy's caps; `over_cap` fails loud and the redelivery retries.
- Per-entrant slot claims, an atomic settle claim, and a refund for a join that registers after its heist was settled.

## Deviations from upstream (documented)

- No timer: settled by the next `!heist` after the deadline (that command is told it cannot join). Bets of a heist nobody settles within 24 h need an operator.
- The four-message story (5 s apart) becomes one reply (the caught line + the survivor summary); StageOne/Two/Four lines are not sent. Later joiners get no chat line (as upstream).
- The 512-character survivor-list guard becomes "more than 8 survivors -> counts only" (names are rendered at egress).
- Max 25 entrants; max bet 500 is app policy (no host wager grant is requested). Settings are not forwarded yet, so defaults apply (min bet 10 per the code, not the 100 shown on the settings page).
- The upstream default-command event triggers are not ported.

## Overlay / media assets

None. Upstream PenguinTwitchBot (revision `df27ab1a`) ships no images, sounds or overlay HTML/CSS/JS for Heist: its `wwwroot/`
holds only generic alert/other-game pages (`alerts.html` has no Heist reference) and the Heist command is chat-only. There is
therefore nothing MIT-covered to package, and this bundle declares no static assets and no svc-presentation component.

## Build and test

```bash
make test-superpenguin-heist            # xUnit + 90% line-coverage floor (tests in tests/)
make build-superpenguin-heist-bundle    # pinned Dockerfile -> /tmp/superpenguin-heist-out/superpenguin_heist.wasm
make test-superpenguin-ports-e2e      # real WASM components through the production executor
```

Gated behind PostHog flag `waddles.command-superpenguin-heist` (default OFF). Attribution: author `github/psychoboy`, collection
`superpenguintv`, MIT notice verbatim in `bundle.yaml` and `LICENSE`. See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
