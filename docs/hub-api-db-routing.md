# hub-api DB routing: primary vs read replica

Owner directive (2026-11-15): pure reads go to the Postgres read replica wherever possible;
the `bundle_active_set` watermark job stays on the primary by design. Mechanism:
`hub_api/services/db_routing.py`. Tests: `hub_api/tests/test_db_routing.py`.
Regenerate the table below: `python3 scripts/hubapi_db_inventory.py routes.json &&
python3 scripts/hubapi_db_classify.py routes.json out.md`.

## Mechanism

| Piece | Behaviour |
|---|---|
| `get_read_dal(route, binders=())` / `get_read_dals(...)` | Raw pydal DAL (or `(async_dal, dal)`) a replica-eligible read must build its queries from. Replica when `DATABASE_READ_REPLICA_URL` is set, else primary. |
| `@reads_from_replica("mod.fn")` | Marker + metric/log label on a handler. A guard test fails if a marked handler accepts anything but GET. |
| `record_primary_read(route)` | Records a read deliberately kept on primary. |
| Fallback | No replica configured: serve from primary, ONE WARN per process (`db_routing.replica_not_configured`), metric target `primary_fallback`. Never an error. |
| Observability | DEBUG log `db_routing.read{target,route}` per call; counter `hub_api_db_reads_total{target=replica\|primary_fallback\|primary, route}`. |
| Startup | `app.py` tags the replica DAL, binds the reference tables on it (`_bind_reference_tables`), and exposes it as `config["read_dal"]` so `install_db_resilience(dal_key="read_dal")` ends its transaction at request teardown (no idle-in-transaction on the standby). |

A pydal query runs on the DAL its table objects came from, so routing is by *which DAL the
query is built from*. Nothing is ambient: only code that calls `get_read_dal(s)` can reach the replica.

### Replica-lag tolerance

Streaming-replication lag is usually sub-second but unbounded during a replica fault. A route may
use the replica only if its caller tolerates **seconds of staleness**: public pages, catalog
browse, overlay polls, dashboards, stats, append-only listings. Anything that (a) writes then reads
in the same request or flow, (b) is the page a user re-fetches right after editing, or (c) makes a
security/authz/freshness decision stays on the primary. There is no lag budget or health check
yet: if the replica is down, replica reads error like any DB outage (follow-up: lag-aware fallback).

## Classes

| Class | Target | Rule |
|---|---|---|
| READ-ONLY | replica | pure GET, lag-tolerant, no authz/membership helper sharing its DAL |
| READ-ONLY (eligible, not moved) | primary | lag-tolerant in principle; shares its DAL with an authz helper or edit-then-refetch UI, so it needs a per-route split first |
| READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console GETs that edit-then-refetch, or not yet lag-reviewed |
| WRITE | primary | any mutating route |
| AUTH/SESSION/SECURITY | primary | auth, session, OAuth state, MFA/passkey, tokens, consent/DSAR, rate-limit, idempotency, secret material, in-handler tenant/membership authz |
| NO-DB | n/a | handler touches no DAL (proxy, cache, flag, static) |
| WATERMARK | primary | `bundle_active_set_watermark_job` (`pg_is_in_recovery()` guard); refuses a replica-tagged DAL and is built only from `install_dal` on `cfg.database_url` |

The classification is a mechanical first pass (AST + module/authz heuristics), reviewed by hand
for the moved set. "UNREVIEWED" rows are conservative: they stay primary until someone reviews lag
and read-after-write for that route.

## Non-route DB access

| Module | Access | Class | Target | Reason |
|---|---|---|---|---|
| `services/bundle_active_set_watermark_job.py` | `install_dal` AsyncDB | WATERMARK | primary | by design; guarded in code + test |
| `grpc_internal/dataplane_servicers.py` ListBundles | `async_dal.read_dal` | READ-ONLY | replica (pre-existing) | already routed |
| auth chain (`resolve_tenant_context`, `config["dal"]`) | every authenticated request | AUTH/SESSION/SECURITY | primary | tenant resolution + scope checks |
| `services/*_service.py` called from WRITE/AUTH routes | shared `dal` | follows its route | primary | one DAL per request |
| `install_dal` users (bundle install/approval/marketplace lifecycle, sso, workstream usage, audit) | penguin-dal AsyncDB on `database_url` | WRITE/AUTH mix | primary | no replica pool exists for AsyncDB; follow-up if read-heavy ones matter |
| background loops (purge, reconcilers, usage aggregator) | primary | WRITE | primary | they mutate |

## Findings

* `flask_core.AsyncDAL.count_async` silently used `read_dal` for every caller once a replica
  was configured (auth and read-after-write counts included). Fixed in `libs/flask_core`: it now
  runs on the DAL the query was built from, like `select_async`. No behaviour change while no
  replica is configured.
* Lazy `bind_*_tables` functions run only on the primary today; replica reads need them applied
  to the replica DAL: pass them as `binders=`.

## Per-route table (571 route handlers; ~650 `dal` access sites in 100+ files)

<!-- generated by scripts/hubapi_db_classify.py: 571 handlers -->

| class | handlers | moved to replica |
|---|---|---|
| WRITE | 317 | 0 |
| NO-DB | 97 | 0 |
| AUTH/SESSION/SECURITY | 76 | 0 |
| READ-AFTER-WRITE / UNREVIEWED | 53 | 0 |
| READ-ONLY | 15 | 14 |
| READ-ONLY (eligible, not moved) | 13 | 0 |
| **total examined** | **571** | **14** |

| module | route/function | class | target | reason |
|---|---|---|---|---|
| `v1/access_token.py` | 4: GET /scopes (user_list_scopes); GET /pat (get_pat); GET /<int:community_id>/tokens/scopes (community_list_scopes) (+1 more) | AUTH/SESSION/SECURITY | primary | API tokens: revocation must be immediate |
| `v1/access_token.py` | 4: POST /pat (create_pat); DELETE /pat (revoke_pat); POST /<int:community_id>/tokens/cats (create_cat) (+1 more) | WRITE | primary | mutating route |
| `v1/admin.py` | 9: GET /<int:community_id>/settings (get_settings); GET /<int:community_id>/members (get_members); GET /<int:community_id>/reputation/config (get_reputation_config) (+6 more) | AUTH/SESSION/SECURITY | primary | admin console: authz + read-after-write |
| `v1/admin.py` | 9: PUT /<int:community_id>/settings (update_settings); PUT /<int:community_id>/members/<int:user_id>/role (update_member_role); PUT /<int:community_id>/members/<int:user_id>/reputation (adjust_reputation) (+6 more) | WRITE | primary | mutating route |
| `v1/admin_data_privacy.py` | 3: GET /<tenant_slug>/privacy/users/<user_id>/export (export_user); POST /<tenant_slug>/privacy/users/<user_id>/erase (erase_user); PUT /<tenant_slug>/privacy/users/<user_id>/do-not-sell (do_not_sell_user) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/admin_data_privacy.py` | 1: POST /<tenant_slug>/privacy/bulk (bulk_dsar) | WRITE | primary | mutating route |
| `v1/ai_routing.py` | 6: GET /<int:community_id>/ai/config (get_config); PUT /<int:community_id>/ai/config (set_config); GET /<int:community_id>/ai/byok-keys (list_byok_keys) (+3 more) | WRITE | primary | mutating route |
| `v1/analytics.py` | 2: GET /community/<int:community_id>/members/<int:user_id>/stats (get_member_stats); GET /community/<int:community_id>/members/<int:user_id>/reputation (get_member_reputation) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/analytics.py` | 7: GET /me/stats (get_my_stats); GET /me/reputation (get_my_reputation); GET /platform/growth (get_platform_growth) (+4 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/analytics.py` | 2: GET /platform/overview (get_platform_overview); GET /platform/reputation (get_platform_reputation) | READ-ONLY (eligible, not moved) | primary | aggregate dashboard; authz helper shares the dal |
| `v1/app_commands_internal.py` | 2: GET /app-commands/sources (list_sources_route); GET /app-commands/desired (desired_route) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/auth.py` | 1: GET /tenant/<slug> (tenant_login_info) | AUTH/SESSION/SECURITY | primary | login/session/OAuth-state/MFA: freshness + security |
| `v1/auth.py` | 3: POST /link-oauth (link_oauth_legacy); GET /providers (list_auth_providers); POST /passkey/login/start (passkey_login_start) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/auth.py` | 18: POST /register (register); GET /verify-email (verify_email); POST /resend-verification (resend_verification) (+15 more) | WRITE | primary | mutating route |
| `v1/bot.py` | 12: GET /<int:community_id>/shoutout/config (get_shoutout_config); GET /<int:community_id>/shoutout/creators (get_shoutout_creators); GET /<int:community_id>/shoutout/history (get_shoutout_history) (+9 more) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/bot.py` | 2: GET /<int:community_id>/ai-chatter/config (get_ai_chatter_config); PUT /<int:community_id>/ai-chatter/config (update_ai_chatter_config) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/bot.py` | 21: PUT /<int:community_id>/shoutout/config (update_shoutout_config); POST /<int:community_id>/shoutout/creators (add_shoutout_creator); DELETE /<int:community_id>/shoutout/creators/<int:creator_id> (remove_shoutout_creator) (+18 more) | WRITE | primary | mutating route |
| `v1/bundle_activation.py` | 4: GET /<int:community_id>/activation (list_activation); GET /<int:community_id>/activation/<app_id>/access-grants (activation_access_grants); POST /<int:community_id>/activation (activate) (+1 more) | WRITE | primary | mutating route |
| `v1/bundle_admin.py` | 1: GET /bundle-versions (list_bundle_versions) | WRITE | primary | mutating route |
| `v1/bundle_admin_gateway.py` | 1: GET /admin (get_descriptor) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/bundle_admin_gateway.py` | 5: GET /config (get_config); PUT /config (put_config); GET /config/history (get_config_history) (+2 more) | WRITE | primary | mutating route |
| `v1/bundle_approvals.py` | 5: GET /<app_id>/versions/<version>/permissions (get_permissions); POST /<app_id>/versions/<version>/approve (post_approve); POST /<app_id>/versions/<version>/deny (post_deny) (+2 more) | WRITE | primary | mutating route |
| `v1/bundle_approvals_bulk.py` | 1: POST /approvals/bulk (post_bulk_approvals) | WRITE | primary | mutating route |
| `v1/bundle_permissions.py` | 10: POST /<app_id>/versions/<version>/permissions/approve (approve_permissions); GET /<app_id>/versions/<version>/access-grants (get_version_access_grants); GET /community/<int:community_id>/<app_id>/access-grants (get_community_access_grants) (+7 more) | WRITE | primary | mutating route |
| `v1/bundle_superadmin.py` | 3: GET / (list_bundles); POST /<app_id>/disable (post_disable); POST /<app_id>/enable (post_enable) | WRITE | primary | mutating route |
| `v1/bundle_tenant_availability.py` | 3: GET /<tenant_slug>/availability (list_availability); POST /<tenant_slug>/availability (enable_availability); DELETE /<tenant_slug>/availability/<app_id> (disable_availability) | WRITE | primary | mutating route |
| `v1/bundle_versions.py` | 3: POST /<app_id>/versions (post_version); GET /<app_id>/versions (list_versions_route); GET /<app_id>/versions/<version> (get_version) | WRITE | primary | mutating route |
| `v1/calls.py` | 15: GET /<int:community_id>/calls/rooms (get_call_rooms); POST /<int:community_id>/calls/rooms (create_call_room); GET /<int:community_id>/calls/rooms/<room_name> (get_call_room) (+12 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/community_activity.py` | 3: GET /<int:community_id>/leaderboard/messages (message_leaderboard); GET /<int:community_id>/activity/my-stats (my_activity_stats); GET /<int:community_id>/leaderboard-config (leaderboard_config_get) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/community_activity.py` | 6: GET /<int:community_id>/leaderboard/watch-time (watch_time_leaderboard); PUT /<int:community_id>/leaderboard-config (leaderboard_config_update); POST /watch-session (watch_session_ingest) (+3 more) | WRITE | primary | mutating route |
| `v1/community_announcements.py` | 5: DELETE /<int:community_id>/announcements/<int:announcement_id> (delete_route); POST /<int:community_id>/announcements/<int:announcement_id>/publish (publish_route); PUT /<int:community_id>/announcements/<int:announcement_id>/pin (pin_route) (+2 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/community_announcements.py` | 2: GET /<int:community_id>/announcements/<int:announcement_id> (get_route); GET /<int:community_id>/announcements/<int:announcement_id>/broadcast-status (broadcast_status_route) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/community_announcements.py` | 4: GET /<int:community_id>/announcements (list_route); POST /<int:community_id>/announcements (create_route); PUT /<int:community_id>/announcements/<int:announcement_id> (update_route) (+1 more) | WRITE | primary | mutating route |
| `v1/community_chat.py` | 1: GET /<int:community_id>/chat/channels (chat_channels) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/community_chat.py` | 1: GET /<int:community_id>/chat/history (chat_history) | WRITE | primary | mutating route |
| `v1/community_connections.py` | 1: POST /communities/<int:community_id>/connections/<provider>/authorize (authorize_connection) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/community_connections.py` | 4: GET /communities/<int:community_id>/connections (list_connections); GET /connections/callback/<provider> (oauth_callback); DELETE /communities/<int:community_id>/connections/<provider> (delete_connection) (+1 more) | WRITE | primary | mutating route |
| `v1/community_forms.py` | 2: GET /<int:community_id>/forms/<int:form_id> (get_form); GET /<int:community_id>/forms/<int:form_id>/submissions (form_submissions) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/community_forms.py` | 3: GET /<int:community_id>/forms (list_forms); POST /<int:community_id>/forms (create_form); DELETE /<int:community_id>/forms/<int:form_id> (delete_form) | WRITE | primary | mutating route |
| `v1/community_interaction.py` | 3: GET /<int:community_id>/interaction/roles (admin_list_roles); GET /<int:community_id>/interaction/channels/<int:channel_id>/permissions (admin_get_overrides); GET /<int:community_id>/interact/forum/<int:channel_id>/posts/<int:post_id> (member_forum_post) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/community_interaction.py` | 16: GET /<int:community_id>/interaction/channels (admin_list_channels); POST /<int:community_id>/interaction/channels (admin_create_channel); PUT /<int:community_id>/interaction/channels/<int:channel_id> (admin_update_channel) (+13 more) | WRITE | primary | mutating route |
| `v1/community_inventory.py` | 5: GET /<int:community_id>/inventory/checkouts (list_checkouts_route); GET /<int:community_id>/inventory/summary (summary_route); GET /<int:community_id>/inventory/log (audit_log_route) (+2 more) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/community_inventory.py` | 8: GET /<int:community_id>/inventory/items (list_items_route); POST /<int:community_id>/inventory/items (create_item_route); PUT /<int:community_id>/inventory/items/<int:item_id> (update_item_route) (+5 more) | WRITE | primary | mutating route |
| `v1/community_join.py` | 1: POST /<int:community_id>/join (join_community) | WRITE | primary | mutating route |
| `v1/community_loyalty.py` | 3: GET /loyalty/balance (internal_get_balance); GET /loyalty/leaderboard (internal_leaderboard); GET /loyalty/items (internal_list_items) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/community_loyalty.py` | 1: GET /<int:community_id>/loyalty/leaderboard (get_leaderboard) | READ-ONLY (eligible, not moved) | primary | leaderboard aggregate; authz helper shares the dal |
| `v1/community_loyalty.py` | 1: GET /<int:community_id>/loyalty/stats (get_stats) | READ-ONLY (eligible, not moved) | primary | stats aggregate; authz helper shares the dal |
| `v1/community_loyalty.py` | 7: GET /<int:community_id>/loyalty/config (get_config); PUT /<int:community_id>/loyalty/config (update_config); PUT /<int:community_id>/loyalty/user/<platform_user_id>/balance (adjust_balance) (+4 more) | WRITE | primary | mutating route |
| `v1/community_music_queue.py` | 2: GET /<int:community_id>/music-station/policy (get_policy); GET /<int:community_id>/music-station/queue (list_queue) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/community_music_queue.py` | 2: GET /music/status (internal_music_status); GET /music/queue (internal_get_live_queue) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/community_music_queue.py` | 11: PUT /<int:community_id>/music-station/policy (set_policy); POST /<int:community_id>/music-station/queue/requests (enqueue_song_request); POST /<int:community_id>/music-station/queue/playlists (enqueue_playlist_request) (+8 more) | WRITE | primary | mutating route |
| `v1/community_polls.py` | 1: GET /<int:community_id>/polls/<int:poll_id> (get_poll) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/community_polls.py` | 3: GET /<int:community_id>/polls (list_polls); POST /<int:community_id>/polls (create_poll); DELETE /<int:community_id>/polls/<int:poll_id> (delete_poll) | WRITE | primary | mutating route |
| `v1/community_profile.py` | 1: GET /public/communities/<int:community_id>/profile (get_community_profile) | READ-ONLY (eligible, not moved) | primary | public profile; needs tenant/visibility helper split |
| `v1/community_profile.py` | 5: PUT /admin/<int:community_id>/profile (update_community_profile); POST /admin/<int:community_id>/logo (upload_community_logo); DELETE /admin/<int:community_id>/logo (delete_community_logo) (+2 more) | WRITE | primary | mutating route |
| `v1/community_raffle.py` | 4: GET /<int:community_id>/raffle-customization (list_route); PUT /<int:community_id>/raffle-customization/<event_type> (upsert_route); DELETE /<int:community_id>/raffle-customization/<event_type> (delete_route) (+1 more) | WRITE | primary | mutating route |
| `v1/community_reputation.py` | 2: GET /<int:community_id>/reputation/me (get_my_reputation); GET /<int:community_id>/reputation/leaderboard (get_reputation_leaderboard) | WRITE | primary | mutating route |
| `v1/compliance_audit.py` | 4: GET /events (list_events); GET /head (get_head); GET /verify (verify_chain) (+1 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/cookie_consent.py` | 3: GET /policy (get_current_policy); GET /policy/history (get_policy_history); GET /audit (get_audit_log) | AUTH/SESSION/SECURITY | primary | consent record: statutory, read-after-write |
| `v1/cookie_consent.py` | 6: GET / (get_consent); POST / (save_consent); PATCH /preferences (update_preferences) (+3 more) | WRITE | primary | mutating route |
| `v1/data_privacy.py` | 1: GET / (export_user_data) | AUTH/SESSION/SECURITY | primary | DSAR/erasure: statutory, freshness |
| `v1/data_privacy.py` | 1: DELETE / (request_data_deletion) | WRITE | primary | mutating route |
| `v1/discord_bot_install.py` | 1: GET /discord/bot-install (get_bot_install_url) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/distribution.py` | 1: GET /bundles (list_distribution_bundles) | READ-ONLY | replica (pre-existing) | already routed to `read_dal` before this change |
| `v1/event_discord_sync.py` | 1: GET /communities/<int:community_id>/calendar/event-sync (get_event_sync) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/event_discord_sync.py` | 3: PUT /communities/<int:community_id>/calendar/event-sync (put_event_sync); POST /calendar/events/sync-discord (sync_discord_event); POST /calendar/guild-pairings/event-sync (set_event_sync_enabled) | WRITE | primary | mutating route |
| `v1/eventsub_reconcile.py` | 1: GET /secret/<callback_key> (secret_route) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/eventsub_reconcile.py` | 2: POST /reconcile (reconcile_route); POST /results (results_route) | WRITE | primary | mutating route |
| `v1/flags.py` | 1: GET /flags (get_resolved_flags) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/gameserver_connections.py` | 3: POST ? (create_gameserver); POST ? (test_gameserver); DELETE ? (delete_gameserver) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/github_sync.py` | 1: GET /<community_id>/github-sync/connections (list_connections) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/github_sync.py` | 5: POST /webhook (receive_webhook); POST /<community_id>/github-sync/connections (create_connection); DELETE /<community_id>/github-sync/connections/<connection_id> (delete_connection) (+2 more) | WRITE | primary | mutating route |
| `v1/guild_pairing.py` | 3: GET /communities/<int:community_id>/guild-pairings (list_pairings); GET /communities/<int:community_id>/discord/roles (list_discord_roles); GET /communities/<int:community_id>/discord/role-mirrors (list_role_mirrors) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/guild_pairing.py` | 9: POST /communities/<int:community_id>/guild-pairings (create_pairing); POST /communities/<int:community_id>/discord/server-install/authorize (start_server_install); PATCH /communities/<int:community_id>/guild-pairings/<int:pairing_id> (update_pairing) (+6 more) | WRITE | primary | mutating route |
| `v1/identity.py` | 2: GET / (list_identities); GET /primary (get_primary_identity) | AUTH/SESSION/SECURITY | primary | identity linking/verification codes: security + read-after-write |
| `v1/identity.py` | 4: PUT /primary (set_primary_identity); POST /link/<platform> (start_identity_link); GET /link/<platform>/callback (identity_link_callback) (+1 more) | WRITE | primary | mutating route |
| `v1/ingest_sources.py` | 4: GET / (list_ingest_sources); POST / (create_ingest_source); PATCH /<int:ingest_source_id> (update_ingest_source) (+1 more) | WRITE | primary | mutating route |
| `v1/join_request.py` | 5: POST /<int:community_id>/join-requests (submit_request); GET /<int:community_id>/join-requests/mine (get_my_request); GET /admin/<int:community_id>/join-requests (list_requests) (+2 more) | WRITE | primary | mutating route |
| `v1/live_activity.py` | 2: GET /community/<int:community_id>/live-activity (list_live_activity); GET /community/<int:community_id>/live-activity/stream (stream_live_activity) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/live_status.py` | 2: GET /communities/<int:community_id>/live (get_public_live_status); GET /live (get_internal_live_status) | READ-ONLY | replica (moved) | overlay poll: lag delays a live flip by replication lag only |
| `v1/marketplace_admin_review.py` | 4: GET /settings (get_marketplace_settings); GET /vendor-submissions (get_vendor_submissions_for_review); GET /vendor-submissions/<int:submission_id> (get_vendor_submission_details) (+1 more) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/marketplace_admin_review.py` | 2: GET /vendor-requests (get_vendor_requests); GET /submissions (get_submissions) | READ-ONLY (eligible, not moved) | primary | review queue: act-then-refetch risk |
| `v1/marketplace_admin_review.py` | 10: POST /vendor-requests/<request_id>/approve (approve_vendor_request); POST /vendor-requests/<request_id>/reject (reject_vendor_request); POST /submissions/<int:submission_id>/approve (approve_submission) (+7 more) | WRITE | primary | mutating route |
| `v1/marketplace_billing.py` | 2: GET /status/<int:community_id> (get_premium_status); GET /<int:community_id>/subscriptions (list_subscriptions) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/marketplace_billing.py` | 4: POST /checkout (create_checkout); POST /subscriptions/<provider>/<sub_id>/cancel (cancel_payment_subscription); POST /refunds (create_refund) (+1 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/marketplace_billing.py` | 2: GET /pricing (get_pricing); GET / (list_discount_codes) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/marketplace_billing.py` | 10: POST /subscribe (subscribe_premium); POST /cancel (cancel_premium); POST / (create_discount_code) (+7 more) | WRITE | primary | mutating route |
| `v1/marketplace_catalog.py` | 6: GET / (browse_catalog); GET / (vendors); GET /categories (categories) (+3 more) | READ-ONLY | replica (moved) | catalog browse: seconds of staleness tolerated |
| `v1/marketplace_lifecycle.py` | 2: GET /community/<int:community_id>/bundles (list_activated); GET /community/<int:community_id>/resolved (get_resolved_bundles) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/marketplace_lifecycle.py` | 3: GET /bundles (list_bundles); GET /bundles/collections (list_bundle_collections); GET /tenant/<tenant_slug>/bundles (list_available) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/marketplace_lifecycle.py` | 6: POST /bundles (install_bundle); DELETE /bundles/<app_id> (uninstall_bundle); POST /tenant/<tenant_slug>/bundles (make_available) (+3 more) | WRITE | primary | mutating route |
| `v1/marketplace_vendor.py` | 1: GET /analytics/api-usage (get_api_usage_metrics) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/marketplace_vendor.py` | 10: GET /profile (get_vendor_profile); GET /analytics/installs (get_install_time_series); GET /analytics/discount-codes (get_discount_code_performance) (+7 more) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/marketplace_vendor.py` | 1: GET /analytics/sales (get_sales_metrics) | READ-ONLY (eligible, not moved) | primary | vendor analytics aggregate |
| `v1/marketplace_vendor.py` | 1: GET /dashboard (get_vendor_dashboard) | READ-ONLY (eligible, not moved) | primary | vendor dashboard; edit-then-refetch risk |
| `v1/marketplace_vendor.py` | 7: POST /profile (create_vendor_profile); PUT /profile (update_vendor_profile); POST /modules (create_vendor_module) (+4 more) | WRITE | primary | mutating route |
| `v1/marketplace_webhooks.py` | 2: POST /stripe (stripe_webhook); POST /paypal (paypal_webhook) | WRITE | primary | mutating route |
| `v1/media_byok.py` | 1: GET /<int:community_id>/media/byok-keys (get_media_byok_status) | AUTH/SESSION/SECURITY | primary | BYOK key material: security |
| `v1/media_byok.py` | 2: PUT /<int:community_id>/media/byok-keys (set_media_byok_key); DELETE /<int:community_id>/media/byok-keys/<provider> (delete_media_byok_key) | WRITE | primary | mutating route |
| `v1/music.py` | 2: GET /<int:community_id>/music/providers (get_providers); GET /<int:community_id>/music/radio-stations (get_radio_stations) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/music.py` | 6: GET /<int:community_id>/music/settings (get_music_settings); PUT /<int:community_id>/music/settings (update_music_settings); POST /<int:community_id>/music/providers/<provider>/oauth (start_oauth) (+3 more) | WRITE | primary | mutating route |
| `v1/one_time_secrets.py` | 2: POST / (create_secret); POST /pull (pull_secret) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/overlay.py` | 1: GET /<int:community_id>/overlay/stats (get_overlay_stats) | READ-ONLY (eligible, not moved) | primary | stats aggregate; authz helper shares the dal |
| `v1/overlay.py` | 5: GET /<int:community_id>/browser-sources (get_browser_sources); POST /<int:community_id>/browser-sources/regenerate (regenerate_browser_sources); GET /<int:community_id>/overlay (get_overlay) (+2 more) | WRITE | primary | mutating route |
| `v1/passkey.py` | 1: GET /credentials (list_credentials) | AUTH/SESSION/SECURITY | primary | WebAuthn challenge/credential: security |
| `v1/passkey.py` | 3: POST /register/start (start_registration); POST /register/finish (finish_registration); DELETE /credentials/<int:credential_id> (remove_credential) | WRITE | primary | mutating route |
| `v1/platform.py` | 5: GET /users (list_users); GET /users/<int:user_id> (get_user); GET /communities (list_communities) (+2 more) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/platform.py` | 1: GET /audit-log (get_audit_log) | READ-ONLY (eligible, not moved) | primary | append-only audit listing; authz helper shares the dal |
| `v1/platform.py` | 1: GET /stats (get_stats) | READ-ONLY (eligible, not moved) | primary | platform stats dashboard; authz helper shares the dal |
| `v1/platform.py` | 4: PUT /users/<int:user_id>/role (update_user_role); DELETE /users/<int:user_id> (deactivate_user); PUT /communities/<int:community_id> (update_community) (+1 more) | WRITE | primary | mutating route |
| `v1/platform_config.py` | 2: GET /platform-config (get_platform_configs); GET /settings (get_hub_settings) | AUTH/SESSION/SECURITY | primary | platform/OAuth client config: security-relevant |
| `v1/platform_config.py` | 1: POST /platform-config/<platform>/test (test_platform_connection) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/platform_config.py` | 2: PUT /platform-config/<platform> (update_platform_config); PUT /settings (update_hub_settings) | WRITE | primary | mutating route |
| `v1/profile.py` | 1: GET /profile (get_my_profile) | AUTH/SESSION/SECURITY | primary | caller's own profile: read-after-write after edit |
| `v1/profile.py` | 4: PUT /profile (update_my_profile); POST /profile/avatar (upload_avatar); DELETE /profile/avatar (delete_avatar) (+1 more) | WRITE | primary | mutating route |
| `v1/provider_connections.py` | 6: GET /connection-providers (list_connection_providers); GET ? (list_provider_connections); POST ? (create_provider_connection) (+3 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/public.py` | 3: GET /stats (get_stats); GET /signup-settings (get_signup_settings); GET /signup-settings (get_signup_settings_alias) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/public.py` | 6: GET /communities/spotlighted (get_spotlighted_communities); GET /communities (list_communities); GET /communities/<int:community_id> (get_community) (+3 more) | READ-ONLY | replica (moved) | anonymous public page: lag tolerated |
| `v1/public_music_queue.py` | 1: GET /communities/<int:community_id>/music-station/queue (get_public_queue) | READ-ONLY (eligible, not moved) | primary | public queue; reads Valkey playback + DB, review pending |
| `v1/sso.py` | 11: GET /options (sso_options); GET /<public_id>/start (sso_start); GET /<public_id>/login (sso_login) (+8 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/stream.py` | 2: GET /<int:community_id>/streams/featured (get_featured_streams); GET /<int:community_id>/streams/<entity_id> (get_stream_details) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/stream.py` | 1: GET /<int:community_id>/streams (get_live_streams) | WRITE | primary | mutating route |
| `v1/streaming.py` | 3: GET /<int:community_id>/streams (get_stream_config); GET /<int:community_id>/streams/destinations (get_destinations); GET /<int:community_id>/streams/status (get_stream_status) | AUTH/SESSION/SECURITY | primary | in-handler tenant/membership authz check |
| `v1/streaming.py` | 5: POST /<int:community_id>/streams (create_stream_config); POST /<int:community_id>/streams/key/regenerate (regenerate_stream_key); POST /<int:community_id>/streams/destinations (add_destination) (+2 more) | WRITE | primary | mutating route |
| `v1/superadmin.py` | 4: GET /dashboard (get_dashboard); GET /communities (list_communities); GET /communities/<int:community_id> (get_community) (+1 more) | AUTH/SESSION/SECURITY | primary | global admin: authz + read-after-write |
| `v1/superadmin.py` | 7: POST /communities (create_community); PUT /communities/<int:community_id> (update_community); DELETE /communities/<int:community_id> (delete_community) (+4 more) | WRITE | primary | mutating route |
| `v1/support.py` | 6: GET /<int:community_id>/support/categories (list_categories); GET /<int:community_id>/support/tickets (list_tickets); GET /<int:community_id>/support/tickets/<int:ticket_id> (get_ticket) (+3 more) | READ-AFTER-WRITE / UNREVIEWED | primary | authenticated console/dashboard GET: its UI edits then re-fetches the same rows |
| `v1/support.py` | 9: POST /<int:community_id>/support/categories (create_category); PUT /<int:community_id>/support/categories/<int:category_id> (update_category); DELETE /<int:community_id>/support/categories/<int:category_id> (delete_category) (+6 more) | WRITE | primary | mutating route |
| `v1/tenant.py` | 4: GET /<tenant_slug> (get_tenant); GET /<tenant_slug>/settings (get_tenant_settings); GET /<tenant_slug>/communities (get_tenant_communities) (+1 more) | AUTH/SESSION/SECURITY | primary | tenant/admin membership: authz |
| `v1/tenant.py` | 4: PUT /<tenant_slug> (update_tenant); PUT /<tenant_slug>/settings (update_tenant_settings); POST /<tenant_slug>/admins (add_tenant_admin) (+1 more) | WRITE | primary | mutating route |
| `v1/tenant_discord_install.py` | 1: POST /tenants/discord/install/authorize (start_install) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/tenant_discord_install.py` | 2: GET /discord/install/callback (install_callback); GET /tenants/discord/install/status (install_status) | WRITE | primary | mutating route |
| `v1/tenant_kms.py` | 4: GET /<tenant_slug>/kms (get_kms_status); PUT /<tenant_slug>/kms (put_kms_config); POST /<tenant_slug>/kms/activate (activate_kms) (+1 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/tenant_reputation.py` | 1: GET /<tenant_slug>/reputation/config (get_tenant_reputation_config) | READ-AFTER-WRITE / UNREVIEWED | primary | unreviewed GET: kept primary until lag review |
| `v1/tenant_reputation.py` | 1: PUT /<tenant_slug>/reputation/config (put_tenant_reputation_config) | WRITE | primary | mutating route |
| `v1/tenant_twitch_install.py` | 2: POST /authorize (authorize); GET /callback (callback) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/token_billing.py` | 3: GET / (list_token_products); GET /<int:community_id>/tokens/balances (get_balances); GET /<int:community_id>/tokens/transactions (get_transactions) | AUTH/SESSION/SECURITY | primary | metered balance/ledger: money, freshness |
| `v1/token_billing.py` | 2: POST /<int:community_id>/tokens/credit (credit_tokens); POST /<int:community_id>/tokens/debit (debit_tokens) | WRITE | primary | mutating route |
| `v1/user_management.py` | 4: GET / (list_users); GET /<int:user_id> (get_user); GET /<int:user_id>/sessions (list_user_sessions) (+1 more) | AUTH/SESSION/SECURITY | primary | user/role admin: authz + read-after-write |
| `v1/user_management.py` | 10: POST / (create_user); PUT /<int:user_id> (update_user); DELETE /<int:user_id> (delete_user) (+7 more) | WRITE | primary | mutating route |
| `v1/workflow.py` | 12: GET /<community_id>/workflows (list_workflows); GET /<community_id>/workflows/<workflow_id> (get_workflow); PUT /<community_id>/workflows/<workflow_id> (update_workflow) (+9 more) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/workflow.py` | 3: POST /<community_id>/workflows (create_workflow); POST /<community_id>/workflows/<workflow_id>/publish (publish_workflow); POST /<community_id>/workflows/<workflow_id>/execute (execute_workflow) | WRITE | primary | mutating route |
| `v1/workstream_usage.py` | 1: GET / (list_usage) | WRITE | primary | mutating route |
| `v1/youtube_connections.py` | 1: POST ? (discover_live_chat) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
| `v1/youtube_connections.py` | 3: PUT /tenants/youtube/app (put_youtube_app); POST ? (authorize_youtube); GET /provider-connections/youtube/callback (youtube_callback) | WRITE | primary | mutating route |
| `v2/platform.py` | 2: GET /status (platform_status); POST /echo (platform_echo) | NO-DB | n/a | no DAL access in the handler (proxy / cache / flag / static) |
