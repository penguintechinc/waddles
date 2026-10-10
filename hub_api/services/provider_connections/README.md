# provider_connections

The standard provider/connection framework: one adapter interface, one connection API and one
encrypted per-community token store for every external platform (Twitch first).

Guide, API reference, security model, data-plane contract and the "adding a provider" checklist:
[`docs/PROVIDER_CONNECTIONS.md`](../../../docs/PROVIDER_CONNECTIONS.md).

| Module | Job |
|---|---|
| `adapter.py` | `AdapterSpec` (declarative provider description) + `ProviderAdapter` protocol + metadata/credential validation + `AdapterRegistry`. No Waddles-specific imports. |
| `twitch.py` | The first adapter: bot account token -> channel resolution -> refresh via the platform Twitch app. |
| `registry.py` | **The only wiring point** -- adding a provider is its adapter module plus one line here. |
| `vault.py` | The only code that encrypts/decrypts a connection secret (`TokenVault`; default AES-256-GCM platform baseline). |
| `store.py` | penguin-dal persistence over `platform_connections` / `community_connection_access`; every query is tenant- *and* community-scoped; admin reads never select a token column. |
| `service.py` | Orchestration: create / update / delete (community-scoped) and data-plane list / resolve (refresh under a row lock). |
| `telemetry.py` | OTel metrics + spans and PII-safe failure logging. |
| `errors.py` | Fixed, application-authored error taxonomy; mapped to HTTP only in the blueprint. |

Rules this package holds itself to (each is pinned by a test): framework code never names a
provider; a token is never logged, traced, labelled, returned to an admin, or selected by an admin
read; a provider outage never changes stored state; a rejected refresh token marks the link expired
instead of deleting it.
