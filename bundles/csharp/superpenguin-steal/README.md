# superpenguin-steal

C# port of PenguinTwitchBot `PastyGames/Steal.cs` + `StealGameOptions.cs` (MIT, used with permission). `!steal TARGET`: a random
100..10,000 coins, 33.33% success; the thief must hold at least the maximum; a target holding less is "too poor" and the thief
GIVES them the amount; a failed steal makes the thief pay the target; 300 s per-thief cooldown; upstream wording kept.
`!steal optout` / `!steal optin` are additions (below).

## ALL safe-wager logic is in this app; the economy service is only I/O

| Rule | Value |
|---|---|
| Odds / amount | 33.33% (roll 0..9999 under 3333); amount uniform 100..10,000 |
| Per-steal cap | 10,000 |
| Per-thief daily limit (UTC day, amount at risk, any outcome) | 30,000; the last attempt is capped to what is left |
| Per-victim daily loss limit | 10,000; the last theft is capped; then "robbed enough" |
| Cooldown | 300 s per thief (released if the transfer is refused or disabled); 120 s victim immunity after a theft |
| Protected | opted-out targets (they also cannot steal), `protected_users` UUIDs in the app config (e.g. mods), low-balance targets (< 10,000), self |
| Failure penalty | the thief pays the victim the same amount, in the same atomic transfer |

## Economy contract: `transfer_from` (dangerous, admin-approved)

Moving coins out of the victim needs `economy.transfer_from` (branch `feature/economy-transfer-from`). This app codes to the contract
in `contract/transfer_from.json`: args `{from, to, amount, reason}`, idempotency key in the tool-invoke key slot, refusals
`insufficient_funds | over_cap | not_a_member`. The SDK pins the wire shape (`EconomyServiceClientTests`), the CI test
`scripts/ci/tests/test_superpenguin_service_boundaries.py` checks the economy catalog against the fixture once the tool ships.
The result shape is not pinned (success = no refusal) -- reconcile when the service merges.

**Until the grant exists, every steal path replies `steal not enabled` and moves nothing** (cooldown released, record discarded,
warning logged). Nothing is faked and there is no private ledger. It needs the approval of the dangerous `transfer_from` grant to work.

## Exactly-once

The roll, amount and direction are recorded (SDK `IdempotentOutcome`, bound to message id + platform + actor + command + args hash)
BEFORE the single transfer; a redelivery re-issues the same transfer with the same key and the host de-duplicates it. Daily counters
are committed once per message after a successful transfer (a crash between the two under-counts, never over-moves; counters use kv
read-modify-write so racing thefts on one victim can under-count by one attempt).

## Deviations from upstream (documented)

- "Stream must be online" is not checked (no such capability in any world).
- Upstream `MovePoints` removed points from BOTH users (a bug); this port performs a true transfer.
- Opt-out, daily limits, victim immunity and the protected list are safety additions. The host does not yet forward the app config,
  so `protected_users` is empty until it does; moderators can use `!steal optout` meanwhile.
- The upstream default-command event triggers are not ported.

## Overlay / media assets

None. Upstream PenguinTwitchBot (revision `df27ab1a`) ships no images, sounds or overlay HTML/CSS/JS for Steal: its `wwwroot/`
holds only generic alert/other-game pages (`alerts.html` has no Steal reference) and the Steal command is chat-only. There is
therefore nothing MIT-covered to package, and this bundle declares no static assets and no svc-presentation component.

## Build and test

```bash
make test-superpenguin-steal            # xUnit + 90% line-coverage floor (tests in tests/)
make build-superpenguin-steal-bundle    # pinned Dockerfile -> /tmp/superpenguin-steal-out/superpenguin_steal.wasm
make test-superpenguin-ports-e2e      # real WASM components through the production executor
```

Gated behind PostHog flag `waddles.command-superpenguin-steal` (default OFF). Attribution: author `github/psychoboy`, collection
`superpenguintv`, MIT notice verbatim in `bundle.yaml` and `LICENSE`. See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
