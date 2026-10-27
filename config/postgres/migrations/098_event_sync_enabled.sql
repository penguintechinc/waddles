-- Migration 098: Event-Discord-sync -- guild_tenant_pairings.event_sync_enabled +
-- calendar_event_discord_syncs (dual-system mirror of alembic/versions/0039_event_sync_enabled.py)
--
-- This repo runs schema changes through TWO systems depending on which table group
-- owns them: hub-api's M2b control-plane tables (0020+) go through Alembic
-- (alembic/versions/), legacy per-module tables (calendar_events and friends) go
-- through this numbered config/postgres/migrations/*.sql sequence. guild_tenant_pairings
-- is Alembic-owned (migration 0034); calendar_events is legacy. This file carries the
-- SAME idempotent DDL as 0039_event_sync_enabled.py so either migration path, run first,
-- leaves the other a safe no-op -- see that file's own docstring for the full rationale
-- and the migration-number collision note (098 picked over 097, already claimed by two
-- in-flight PRs at write time).
-- Timestamp: 2026-10-05
--
-- Fresh-DB replay: this legacy file runs inside the 000->100 baseline, which on a
-- brand-new database executes BEFORE any alembic revision, so guild_tenant_pairings
-- (alembic 0034) does not exist yet. The whole body is therefore guarded: it is a
-- no-op until the Alembic-owned table exists, and 0039_event_sync_enabled.py applies
-- the identical DDL at the right point in the chain. On an already-migrated DB this
-- file is recorded as applied and never re-run.

DO $mig$
BEGIN
  IF to_regclass('public.guild_tenant_pairings') IS NULL THEN
    RAISE NOTICE '098: guild_tenant_pairings not created yet (alembic 0034); deferring to alembic 0039';
    RETURN;
  END IF;

  ALTER TABLE guild_tenant_pairings
      ADD COLUMN IF NOT EXISTS event_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE;

  COMMENT ON COLUMN guild_tenant_pairings.event_sync_enabled IS
      'Opt-in for the Discord event-sync push engine, independent of sync_enabled '
      '(role-sync) on the same pairing row.';

  CREATE TABLE IF NOT EXISTS calendar_event_discord_syncs (
      id BIGSERIAL PRIMARY KEY,
      event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
      pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
      discord_guild_id VARCHAR(255) NOT NULL,
      discord_event_id VARCHAR(255),
      sync_status VARCHAR(20) NOT NULL DEFAULT 'pending'
          CHECK (sync_status IN ('pending', 'synced', 'sync_error')),
      sync_error TEXT,
      last_sync_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE (event_id, pairing_id)
  );

  COMMENT ON TABLE calendar_event_discord_syncs IS
      'Per-guild Discord scheduled-event sync state for one calendar_events row -- the '
      'multi-guild fan-out state calendar_events own single discord_event_id/sync_status/'
      'sync_error columns cannot represent.';

  CREATE INDEX IF NOT EXISTS idx_calendar_event_discord_syncs_event
      ON calendar_event_discord_syncs (event_id);

  CREATE INDEX IF NOT EXISTS idx_calendar_event_discord_syncs_pending
      ON calendar_event_discord_syncs (sync_status)
      WHERE sync_status IN ('pending', 'sync_error');

END
$mig$;
