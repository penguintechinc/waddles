# Bundle platform-table access grants (`table_access`)

Status: implementing (2026-11-06). Direction from the owner: a bundle manifest
must be able to REQUEST ACCESS TO OTHER (platform) TABLES -- e.g. a role-sync
app writing `community_members.role` -- with a prominent BOLD RED warning to the
global admin (approval) and the community admin (activation). This REPLACES
one-off host imports such as a `community.role.set`.

## 0. Service vs app split

- **Service bundles** (`kind: service`) own platform definitions and tools (e.g. the
  Discord role tools) and expose them via `exported_tools`; **app bundles** invoke
  those through `tool.call`/`tool.invoke` (bundle-MCP-tools framework).
- **`table_access` is for APP bundles only**, touching Waddles platform tables
  (e.g. `role_sync` writing `community_members.role`). A `kind: service` manifest
  that declares `table_access` is rejected (`table_access_app_only`): platform-side
  behaviour belongs in a service tool, not in a raw table grant.

## 1. Manifest (schema v2, additive)

```yaml
table_access:
  - table: community_members
    columns: [role]
    ops: [read, write]
    reason: "Mirror Discord role -> community role"   # 1-280 chars, mandatory
```

- `table` MUST be in the platform-table allowlist (section 2); `columns` MUST be a
  non-empty subset of that table's allowlisted columns FOR EACH requested op;
  `ops` is a non-empty subset of `read|write`; `reason` is mandatory plain text.
- Parser folds every `(op, table, column)` into one permission id
  `platform.table.<op>:<table>.<column>` (same pattern as `requested_tools` ->
  `tool.call:*`) so it rides the existing approval -> `app_permission_requests`
  -> `community_permission_grants` -> gate path, the dangerous-ack gate,
  `permission_hash` re-consent and revocation with no parallel flow.
- Unknown table/column/op, or an unlisted pair, is `invalid_table_access` (422) at
  parse time; the same ids are re-validated in the Rust gate parser (closed catalog).

## 2. Allowlist (initial, deliberately narrow)

| table | column | read | write | notes |
|---|---|---|---|---|
| `community_members` | `role` | yes | yes | role sync. Write values limited to `member`/`vip`/`moderator`; rows whose current role is `community-owner`/`community-admin` are never touched |
| `community_members` | `is_active` | yes | no | membership state (Wix/membership sync reads it) |
| `community_members` | `joined_at` | yes | no | tenure |

Never allowlisted (enforced by an explicit deny test): `hub_users*`, `hub_user_identities`,
credential/secret/token tables, `platform_connections`, any table with email/password/
handle/display-name PII, and every `community_members` PII column (`display_name`,
`avatar_url`, `bio`, `social_links`, `platform_user_id`). Widening the allowlist is a
code change in both `hub_api/services/bundle_table_access.py` and
`core/bundle_capability_gate` (parity-tested against
`core/bundle_capability_gate/tests/fixtures/platform_table_allowlist.json`).

## 3. Risk class and auto-grant

`platform.table.*` is `dangerous`. It is NEVER auto-granted -- not by the
`system:core-seeder` path either. The core seeder today blanket-pre-grants every
permission a `waddles.core.*` manifest declares (including `connector.pii.read`);
that behaviour is left untouched for existing ids and NOT extended: the seeder
(`seed_core_permission_requests` / `record_permission_requests` system path) filters
out `platform.table.*` ids, so a core bundle's table access stays unapproved until a
human global admin approves it.

## 4. Approval, consent, audit, revocation

- Global admin: the approval summary (`GET .../permission-summary`) carries
  `tableAccess: [{table, columns, ops, reason}]`; the review panel renders it in
  a bold red warning block. `tableAccess` is part of the hashed summary, so a
  widened manifest (new table/column/op) changes `permissionHash` and
  `classify_diff` reports `widened` -> re-approval; the old grant does not cover the
  new ids (grants are exact-id), so widened access is SUSPENDED until re-approved.
- Community admin: activation requires `acknowledgedTableAccess` listing every
  table-access permission id of the version; a missing ack is 422
  `table_access_ack_required` carrying the access list; `GET .../activation/<app>/table-access`
  powers the red confirm dialog.
- Audit: existing `bundle_audit` rows (`permissions_approved_globally`,
  `community_permissions_granted`) record who/when/what ids; each host write is
  additionally audited (section 5). Revocation = existing `deactivate_permission`
  / grant revoke; the gate snapshot invalidation makes it immediate.

## 5. Host enforcement (`core/bundle_host_platform_table`, WIT `platform-table` interface)

Guest API is key-addressed, never SQL: `read(user, columns)` / `write(user, column-values)`
on a single allowlisted table (initially implied `community_members`; the table is
a parameter validated against the allowlist). Order of checks, all fail-closed:

1. Gate `authorize(PlatformTable{op,table,column})` for EVERY named column (grant
   exists, instance policy, quota/rate).
2. Allowlist re-check inside the store (defense in depth; independent of the gate).
3. Value validation (role value set); a value outside is `invalid`.
4. SQL built from compile-time constants only (table/column identifiers never from
   the guest string), values bound as parameters. The statement is ALWAYS scoped by
   host-injected `community_id` AND `tenant_id` (`community_members` joined to
   `communities.tenant_id`); the guest supplies only the target user UUID. Zero rows
   affected -> `not-a-member` (cross-community/tenant is indistinguishable from absent).
5. Writes: per-(community, app) rate limit, one `bundle_platform_table_audit` row
   per write in the same transaction (app, version, community, tenant, user, column,
   old/new value), metric `waddles_bundle_platform_table_denied_total{reason}` per denial.
Flag: `waddles.bundle-platform-table-capability` (default OFF; unwired -> `not_implemented`).

## 6. Delivery (stacked PRs into release/v3.0.X)

1. spec + manifest/catalog/gate + seeder carve-out (this doc is PR 1).
2. host store + WIT + dispatch.
3. hub-api approval/ack + webui red warning.

## 7. Generic grant-kind machinery (reused by `storage_access`)

The approval/audit/re-approval machinery is kind-agnostic. A grant kind
(`table`, `storage_prefix`, ...) is one `AccessGrantKind` registered in
`hub_api/services/bundle_access_grants.py`:

```python
register_kind(AccessGrantKind(
    kind="storage_prefix", id_prefix="platform.storage.", summary_key="storageAccess",
    requests_of=lambda manifest: manifest.storage_access,   # validated requests
    summarize=lambda reqs: [...],     # JSON-safe entries; each carries "reason"
    atoms=lambda entry: {...},        # diff atoms -> widened set forces re-approval
))
```

What a registered kind gets for free: (a) NEVER auto-granted (core seeder
carve-out + system approval path cannot write or erase its ids); (b) direct
declaration in `permissions:` rejected (`use_table_access_block`-style); (c)
consent-summary block `summary[summary_key]` -- hashed, so widening changes
`permissionHash`/`classify_diff == widened` -> re-approval; (d) the global
approval ack (`incomplete_dangerous_ack`) and community consent
(`consent_required`) because the expanded ids are `dangerous`; (e) audit +
revoke through the existing permission ledger; (f) the bold-red webui block
`AccessGrantWarning` (`admin/hub_module/frontend/src/components/bundles/`) which
renders every `summary[summary_key]` entry registered in its `KINDS` map.

A new kind MUST still provide: its own manifest parser (fold into dangerous ids
`<id_prefix><...>`), `resolve_risk`/`permission_family` entries in
`bundle_permission_catalog.py`, the Rust `PermissionFamily` + parser in
`core/bundle_capability_gate`, and its host enforcement. Rust gate/host pieces
are per kind; the hub-api + UI pieces are not.
