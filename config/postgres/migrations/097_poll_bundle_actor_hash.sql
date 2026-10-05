-- Migration 097: let the chat-command poll bundle (bundles/python/poll) write to the
-- existing community_polls/poll_votes tables (migration 028) without a resolved
-- hub_users.id.
--
-- Background: migration 089's own FLAG comment already called this out --
-- "[FLAG] User ID resolution: process stage uses placeholder created_by=1 instead of
-- mapping event.actor (username string) to hub_users.id. Proper implementation
-- requires actor->user lookup table or UUID-based actor field." That lookup table does
-- not exist yet (hub_users.username is the dashboard login name, unrelated to a
-- Twitch/Discord chat actor) -- so a chat-created poll genuinely has no FK-valid
-- hub_users.id to supply, and `community_polls.created_by` is NOT NULL today.
--
-- This migration is additive and backward-compatible with the existing REST API
-- (hub_api/blueprints/v1/community_polls.py), which continues to always supply a real
-- `created_by` from its authenticated JWT `sub` claim -- that INSERT path is unaffected.
--
-- poll_votes needs NO schema change: `ip_hash VARCHAR(64)` already exists (migration
-- 028, originally for anonymous form/poll submissions) and is exactly the "identify a
-- voter without a resolved user_id" column the chat bundle needs -- it stores a
-- SHA-256 hash of the chat actor, never the raw username (client.md PII Tokenization /
-- AUTHORING.md SS4).

ALTER TABLE community_polls
    ALTER COLUMN created_by DROP NOT NULL;

ALTER TABLE community_polls
    ADD COLUMN IF NOT EXISTS created_by_hash VARCHAR(64);

COMMENT ON COLUMN community_polls.created_by_hash IS
    'SHA-256 hash of the chat actor that created this poll via bundles/python/poll, '
    'when created_by (a real hub_users.id) could not be resolved. Mutually '
    'exclusive with created_by in practice, never both NULL.';

ALTER TABLE community_polls
    ADD CONSTRAINT community_polls_creator_identity_chk
        CHECK (created_by IS NOT NULL OR created_by_hash IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_community_polls_created_by_hash
    ON community_polls (created_by_hash)
    WHERE created_by_hash IS NOT NULL;
