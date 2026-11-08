# Kick provider connection (hub-api)

hub-api's Kick adapter (`hub_api/services/provider_connections/kick.py`) plugs Kick into the standard provider/connection framework ([`docs/PROVIDER_CONNECTIONS.md`](../PROVIDER_CONNECTIONS.md)). It supplies exactly what the Kick host side needs: a fresh OAuth token and the three binding ids the data plane keys on.

## Connect

`POST /api/v1/communities/{id}/provider-connections` (scope `connections:admin`)

```json
{
  "provider": "kick",
  "credentials": {"access_token": "...", "refresh_token": "..."},
  "metadata": {"channel_slug": "my-channel"}
}
```

- The bot account's token needs scopes `user:read` + `channel:read` (and `chat:write` to post). `GET /api/v1/connection-providers` lists the descriptor, including `maxPerCommunity: 1`.
- The admin supplies **only** `channel_slug`. The hub fetches everything else from Kick **at connect time** and stores it; ids can never be typed in, so they cannot be forged.

| Stored / served key | Source | Used by |
|---|---|---|
| `self_user_id` | `GET api.kick.com/public/v1/users` (token owner) | self-echo filter |
| `broadcaster_user_id` (also `resource_id`) | `GET api.kick.com/public/v1/channels?slug=` | webhook route binding |
| `chatroom_id` | `GET kick.com/api/v2/channels/<slug>` (the public API does not expose it; same lookup the Pusher receiver uses) | inbound Pusher |

If the two channel lookups disagree on the owner, or the channel has no chatroom, the connect is refused (`422 RESOURCE_NOT_FOUND`). Kick unreachable or 5xx is `502 PROVIDER_UNAVAILABLE`; nothing is stored.

## One connection per community

A community may hold **one** live Kick connection (the data plane refuses an ambiguous binding).

- **API validation:** a second connect is `409 CONFLICT` before any Kick call is made.
- **Database constraint:** migration `0082_kick_connection` installs a trigger on `community_connection_access` that raises `unique_violation` (constraint `uq_one_kick_connection_per_community`) for a second non-revoked Kick grant, serialised per community by an advisory lock, so two racing connects admit exactly one. Revoked grants do not count; reconnect after revoking.
- Upgrade refuses (loudly) if a community already holds duplicates: revoke the extras, then re-run.

## Token lifecycle

- Tokens are sealed with the platform AES-256-GCM vault (row-bound AAD) in `platform_connections`; no admin response ever contains one.
- The data plane calls `POST /api/v1/internal/communities/{c}/provider-connections/{id}/resolve` and receives kind `1`, `accessToken`, `expiresAt` and the binding `metadata`.
- **Refresh before expiry:** when the token is within 120 s of `expiresAt` the hub exchanges the (rotating, single-use) refresh token using the platform's Kick app (`KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`) under a row lock: **one exchange per connection no matter how many concurrent resolvers**. The new pair is re-sealed and the new `expiresAt` served.
- **Failures are explicit, never a stale token:**

| Situation | Result |
|---|---|
| Kick rejects the refresh token | connection marked `expired` (`refresh_rejected`), resolve `409 {"reason":"refresh_rejected"}`; admin must reconnect |
| Kick down / 5xx / malformed refresh answer | resolve `502`; nothing changed, next resolve retries |
| Token minted by a different Kick app than the platform's | `409 {"reason":"refresh_not_configured"}`, no call to Kick |
| No refresh token / platform app not configured | `409 token_expired` / `refresh_not_configured` |

## Operations

Set `KICK_CLIENT_ID` and `KICK_CLIENT_SECRET` (operator-managed secret) to enable refresh. Metrics: `waddles_provider_connection_token_refresh_total{provider="kick",outcome}`, provider-call latency histogram; the channel slug and tokens never reach logs, spans or labels.
