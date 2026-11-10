
## App/service boundary (2026-11-07)

This port remains C#. Game rules, wagering, and maximum-bet handling belong
to the app; atomic ledger settlement uses the app's granted host primitive.
The shared economy service is unchanged. Shared balance/credit/debit operations,
where used, invoke its existing tools via the C# SDK; no private ledger is created.
See `docs/superpenguin-port-inventory.md` for the port decisions and remaining work.
