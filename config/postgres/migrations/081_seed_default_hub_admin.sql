-- Migration 081: First-run admin bootstrap + default hub_settings/cookie
-- policy, migrated out of admin/hub_module/backend/src/index.js's
-- initializeDatabase(). That function is now skipped in every deployment
-- (docker-compose.yml/Helm set SKIP_DB_INIT=true) because it unconditionally
-- ran CREATE TABLE/INDEX/ALTER against tables it does not own -- most
-- notably `CREATE INDEX ... ON hub_chat_messages`, which 500s with
-- "must be owner of table hub_chat_messages" (hub_admin only owns
-- hub_users/hub_settings, granted by config/postgres/init.sql; every other
-- app table is owned by the migration-runner role that runs this file).
-- These SQL migrations already own the schema (as documented in
-- hub_api/PORTING.md's "the house pattern") -- so the one thing
-- initializeDatabase() did that SQL migrations don't yet cover, the
-- one-time first-run admin-user + default-settings seed, moves here instead
-- of being silently lost when SKIP_DB_INIT=true.
--
-- SECURITY (CWE-798, OWASP A07): this migration NEVER plants a default or
-- known credential. It creates an initial global admin (system-tenant admin) ONLY when both
-- waddlebot.initial_admin_email / waddlebot.initial_admin_password custom
-- GUCs are populated (from the INITIAL_ADMIN_EMAIL / INITIAL_ADMIN_PASSWORD
-- process env vars -- see alembic/versions/0001_baseline_from_sql_migrations.py
-- and config/postgres/migrations/run-migrations.sh, which both set these
-- GUCs via `SELECT set_config(...)` before this file runs, in whichever
-- execution path applies) AND only if no global admin exists yet. If the env
-- vars are unset, NO admin account is created -- boot continues normally.
--
-- Uses pgcrypto's crypt(..., gen_salt('bf', 12)) to produce a real bcrypt
-- hash ($2a$/$2b$) -- interoperable with Node's `bcrypt` package
-- (admin/hub_module/backend/src/utils/adminBootstrap.js calls
-- bcrypt.compare() against whatever is in password_hash; bcrypt's hash
-- format isn't implementation-specific).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Global identity is derived from tenancy (alembic 0096): the initial admin becomes a member + tenant
-- admin of the reserved `system` tenant. This file stays valid on BOTH sides of 0096 -- it never
-- names hub_users.is_super_admin directly (the column is dropped by 0096); a pre-0096 database that
-- still carries it is detected through information_schema and consulted dynamically.
CREATE TABLE IF NOT EXISTS tenant_members (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, user_id)
);

DO $$
DECLARE
    v_admin_email TEXT := NULLIF(current_setting('waddlebot.initial_admin_email', true), '');
    v_admin_password TEXT := NULLIF(current_setting('waddlebot.initial_admin_password', true), '');
    v_admin_id INTEGER;
    v_system_id INTEGER;
    v_legacy_admin_exists BOOLEAN := FALSE;
BEGIN
    SELECT id INTO v_system_id FROM tenants WHERE slug = 'system';
    IF v_system_id IS NULL THEN
        INSERT INTO tenants (slug, display_name, is_global) VALUES ('system', 'System', FALSE)
        RETURNING id INTO v_system_id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'hub_users'
          AND column_name = 'is_super_admin'
    ) THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM hub_users WHERE is_super_admin = true)'
            INTO v_legacy_admin_exists;
    END IF;

    IF v_admin_email IS NULL OR v_admin_password IS NULL THEN
        RAISE NOTICE 'INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD not set - skipping initial admin bootstrap. No default admin account is created.';
    ELSIF v_legacy_admin_exists
          OR EXISTS (SELECT 1 FROM tenant_admins WHERE tenant_id = v_system_id) THEN
        RAISE NOTICE 'A global admin already exists - skipping initial admin bootstrap.';
    ELSE
        INSERT INTO hub_users (email, username, password_hash, is_active, email_verified)
        VALUES (
            v_admin_email,
            v_admin_email,
            crypt(v_admin_password, gen_salt('bf', 12)),
            true,
            true
        )
        ON CONFLICT (email) DO NOTHING
        RETURNING id INTO v_admin_id;

        IF v_admin_id IS NULL THEN
            RAISE WARNING 'Initial admin bootstrap: INITIAL_ADMIN_EMAIL is already in use by an existing account - not escalating it to global admin.';
        ELSE
            INSERT INTO tenant_members (tenant_id, user_id) VALUES (v_system_id, v_admin_id)
            ON CONFLICT (tenant_id, user_id) DO NOTHING;
            INSERT INTO tenant_admins (tenant_id, user_id, role) VALUES (v_system_id, v_admin_id, 'tenant-owner')
            ON CONFLICT (tenant_id, user_id) DO NOTHING;
            RAISE NOTICE 'Initial global admin created with ID: %', v_admin_id;
        END IF;
    END IF;
END
$$;

INSERT INTO hub_settings (setting_key, setting_value, updated_at) VALUES
    ('signup_enabled', 'true', NOW()),
    ('email_configured', 'false', NOW()),
    ('signup_allowed_domains', '', NOW()),
    ('require_email_verification', 'false', NOW()),
    ('storage_type', 'local', NOW())
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO cookie_policy_versions (version, content, changes_summary, is_active, effective_date)
SELECT
    '1.0.0',
    E'# Cookie Policy\n\n## What Are Cookies\nCookies are small text files stored on your device when you visit our website.\n\n## Types of Cookies We Use\n\n### Necessary Cookies (Required)\nThese cookies are essential for the website to function properly. They enable basic features like page navigation and access to secure areas.\n\n### Functional Cookies (Optional)\nThese cookies enable enhanced functionality and personalization, such as remembering your preferences and settings.\n\n### Analytics Cookies (Optional)\nThese cookies help us understand how visitors interact with our website by collecting and reporting information anonymously.\n\n### Marketing Cookies (Optional)\nThese cookies are used to track visitors across websites to display relevant advertisements.\n\n## Your Choices\nYou can manage your cookie preferences at any time through the cookie settings panel. Note that disabling certain cookies may affect website functionality.\n\n## Contact Us\nIf you have questions about our cookie policy, please contact us at privacy@waddlebot.io.',
    'Initial cookie policy',
    true,
    NOW()
WHERE NOT EXISTS (SELECT 1 FROM cookie_policy_versions);

COMMIT;
