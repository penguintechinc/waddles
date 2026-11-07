# Slash-command registry API (hub-api, internal data plane)

Hub-api side of `docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md`
(sections 2 and 4). Machine JWT, `system` tenant only; tenant is derived from the source pairing,
never from the caller. Entities cross the boundary as `public_id` UUIDs only (never numeric PKs,
never names); `user` is the tokenized identity UUID.

| Method | Path (`/api/v1/internal/dataplane/...`) | Scope | Purpose |
|---|---|---|---|
| GET | `app-commands/sources?platform=&limit=&offset=` | `dataplane:app-commands` | Reconcile work-list: sources with a paired community (limit 1-500, default 100) |
| GET | `app-commands/desired?platform=&source_id=[&if_version=]` | `dataplane:app-commands` | Desired slash commands for one source + idempotency `version` |
| GET | `active-context?platform=&source_id=&user=` | `dataplane:active-context` | User's active community (default: first-registered) + switchable list |
| PUT | `active-context` `{platform, sourceId, user, community}` | `dataplane:active-context` | Switch (`community` = community `public_id` UUID only) |
| DELETE | `active-context?platform=&source_id=&user=` | `dataplane:active-context` | Revert to the default |

## Desired state

* Union of `webhook`-type (`command_types & 2`), platform-matching (or `*`), non-wildcard
  `app_commands` rows of every community paired to the source, plus the tenant-wide sentinel (0).
  `app_commands` is maintained on bundle activate/deactivate, so only installed + enabled bundles appear.
* The built-in `context` command is always first (`builtin: true`); an app declaring `context` is ignored.
* `version` = sha256 over exactly the registered payload (name + description): an unchanged version
  means the connector's bulk `PUT /applications/{app}/commands/guilds/{guild}` is a no-op.
  `if_version=<v>` matching returns `changed: false` with an empty list.
* More than 100 commands (Discord cap) -> first 100 by registration order, `truncated: true`, WARN log.
* Source paired across tenants -> 409; unknown/unpaired -> 404; bad input -> 400.

## Active context

`user` is the tokenized identity UUID (no FK, no PII). Names never resolve: for a `/context <name>`
UX the bundle reads `communities[{publicId, displayName, position}]` from the GET, matches the name
itself, then PUTs the UUID (non-UUID `community` -> 400, unpaired UUID -> 404). A stored choice no longer paired to the
source falls back to the default (`explicit: false`). Table: `user_active_context` (migration 0082).
