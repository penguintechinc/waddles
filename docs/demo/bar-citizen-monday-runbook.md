# Bar Citizen Monday Demo: Role Sync + Event Sync Rehearsal Runbook

Target: alpha `waddlebot` community (id 1), Discord guild `474965105759748096`,
Twitch channel `penguinzplays`. Run top to bottom after the deploy lands.

## Honest limits (say these out loud)

- Role sync is a **poll reconcile every minute** (`*/1` CronJob), not an instant gateway push. Expect up to ~1 min plus job start.
- Direction shipped: **Discord role -> Waddles community role**. Reverse (Waddles -> Discord) is **roadmap**.
- Demote is **guarded**: opt-in per pairing, only touches roles sync itself manages, never `community-owner`, aborts on an incomplete member read or when the circuit breaker trips (more than 3 and more than 50% of holders in one pass).
- Event sync is Waddles -> Discord only (Waddles is source of truth). Edits made in Discord are not pulled back.
- Members must already be linked (Discord identity -> hub user -> community member). The seed pre-links one demo member; unlinked guild members are skipped, never auto-enrolled.

## 0. Pre-reqs (all must be true; verify each)

| # | Pre-req | How to set / verify |
|---|---------|---------------------|
| 1 | Event-sync flag ON for the demo tenant | PostHog flag `waddles.calendar.event_discord_sync` (env baseline `FLAG_WADDLES_CALENDAR_EVENT_DISCORD_SYNC=true`) |
| 2 | Role-sync flag ON for the demo tenant | PostHog flag `waddles.bar_citizen.role_sync` |
| 3 | Role-sync CronJob deployed | Helm `pipeline.roleSync.enabled=true` (schedule `*/1 * * * *`); `kubectl get cronjob` shows it |
| 4 | Event-sync backstop CronJob (optional) | Helm `pipeline.eventSync.enabled=true` (`*/5`); the immediate push path does not need it |
| 5 | Demote opt-in | Helm `pipeline.roleSync.demotePairingIds="<pairing id>"` (id printed by the seed script) |
| 6 | **Demote baseline role exists** | Helm default `demoteBaselineRole: "community-member"` is NOT a seeded role (seeded: `member`, `communicator`, `speaker`, `moderator`, `community-admin`, `community-owner`). Set `pipeline.roleSync.demoteBaselineRole: "member"` or demote silently aborts with `role_sync.demote_aborted_baseline_missing` |
| 7 | Bot has MANAGE_EVENTS | Default install permissions include bit 33 (`DEFAULT_BOT_PERMISSIONS`, `hub_api/services/discord_install_service.py:72`). A bot invited before that needs re-invite or a manual grant; check Server Settings > Integrations |
| 8 | GUILD_MEMBERS (Server Members) intent ON | Discord Developer Portal > Bot > Privileged Gateway Intents; required to list members for the reconcile |
| 9 | Demo tenant at Professional tier | Verify via the license/tier endpoint for the tenant |
| 10 | Tenant Discord credentials present | `tenant_platform_credentials` row for platform `discord` (tenant 0 uses cluster creds) |

## 1. Seed (pre-link)

```bash
# DATABASE_URL = hub DB DSN (from the secret; never typed into shell history)
export DEMO_DISCORD_USER_ID=<demo member's Discord user id>     # Discord: right-click > Copy User ID
export DEMO_DISCORD_ROLE_ID=<BC's real role id to sync>         # Server Settings > Roles > Copy Role ID
# optional: DEMO_COMMUNITY_ROLE=moderator (vip|member|community-admin), DEMO_BASELINE_ROLE=member
make seed-bc-demo
```

Expected final log line: `seed_bc_demo.done ... pairing_id=N`. Put `N` in `demotePairingIds` (pre-req 5) and apply. Re-running is safe and also resets the demo member to the baseline role (rehearsal reset). It fails loudly if any table/column, the community, or the baseline role row is missing.

The seed sets: pairing `sync_enabled=true`, `event_sync_enabled=true`, `direction=discord_to_twitch` (the direction that runs the Discord -> Waddles pass); binding `sync_scope=community_role`, Discord role -> `moderator` by default.

## 2. EVENT SYNC (Waddles -> Discord)

Confirm toggle state first:

```bash
curl -s -H "Authorization: Bearer $JWT" \
  https://<hub>/api/v1/communities/1/calendar/event-sync    # expect event_sync_enabled=true, pairings_enabled>=1
# if false:
curl -s -X PUT -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"enabled": true}' https://<hub>/api/v1/communities/1/calendar/event-sync
```

**2a. Web UI**
1. Log in to the hub web UI as a community admin.
2. Go to `/admin/1/calendar/events` (Admin > Calendar > Events).
3. Create an event: title (3+ chars), future start date/time, optional description/end. Save.
4. If it shows pending approval, click Approve.

**2b. API**
```bash
curl -s -X POST -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"title":"BC Demo Night","description":"Rehearsal","event_date":"2026-10-19T20:00:00Z","end_date":"2026-10-19T22:00:00Z"}' \
  https://<hub>/api/v1/admin/1/calendar/events      # expect 201
```

**Verify:** in the Bar Citizen Discord server open the **Events** list (sidebar). The scheduled event appears within seconds (immediate push) or by the next 5-min backstop. DB check:
```sql
SELECT sync_status, last_sync_at FROM calendar_event_discord_syncs ORDER BY id DESC LIMIT 1;  -- synced
```
Edit the title in Waddles and confirm Discord updates; cancel it and confirm Discord cancels.

Triage: `sync_status=sync_error` or hub-api event-sync errors mean check MANAGE_EVENTS (pre-req 7) and the flag (1).

## 3. ROLE SYNC (Discord -> Waddles, ~1 min)

**3a. Promote**
1. In Discord, assign the synced role (`DEMO_DISCORD_ROLE_ID`) to the pre-linked demo member.
2. Wait up to ~1 min (next `*/1` run; `kubectl get jobs -w`).
3. Verify in the web UI (Community > Members) that the member's role is `moderator` (or the configured role), or:
```sql
SELECT role FROM community_members
 WHERE community_id=1 AND platform='discord' AND platform_user_id='<id>';
```

**3b. Demote (guarded)**
1. In Discord, remove the role from the member.
2. Wait ~1 min. Expected: role returns to the baseline (`member`); log `role_sync.demote_pass ... demoted=1`.
3. If it does not demote, check in order: pairing id in `demotePairingIds`; baseline role exists (pre-req 6); the role was granted by sync (manually set roles are `unmanaged_untouched` and left alone); member is not `community-owner`; Server Members intent on (an empty member read aborts demote).

**3c. Reset between rehearsals:** `make seed-bc-demo` (member back to baseline; pairing and binding re-asserted).

## 4. Go/no-go checklist (5 min before)

- [ ] Both flags ON, tenant Professional
- [ ] `make seed-bc-demo` ran clean with BC's real role and user ids
- [ ] `demotePairingIds` + `demoteBaselineRole=member` deployed
- [ ] One full promote + demote cycle rehearsed; one event created and seen in Discord
- [ ] Demo member reset to baseline
