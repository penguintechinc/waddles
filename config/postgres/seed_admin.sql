-- Seed Script: Create or Reset the Admin User
-- Run this after init.sql and migrations to create/reset the admin account.
--
-- SECURITY (CWE-798): this script NEVER contains a hardcoded credential.
-- The admin email and password are read from the ADMIN_EMAIL / ADMIN_PASSWORD
-- process environment variables (never CLI args, so they never land in shell
-- history or `ps`) and hashed here with pgcrypto's bcrypt-compatible crypt().
-- Do not run this file directly with `psql -f` -- use the wrapper:
--
--   ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='<strong-password>' \
--     scripts/seed-admin.sh
--
-- or set the two env vars and invoke psql directly.

\set ON_ERROR_STOP on

\getenv admin_email ADMIN_EMAIL
\getenv admin_password ADMIN_PASSWORD

-- Fail loudly (non-zero exit via ON_ERROR_STOP) rather than silently
-- falling back to any default when required env vars are missing.
\if :{?admin_email}
\else
DO $$ BEGIN RAISE EXCEPTION 'ADMIN_EMAIL env var not set. Run via scripts/seed-admin.sh (see --help).'; END $$;
\endif

\if :{?admin_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'ADMIN_PASSWORD env var not set. Run via scripts/seed-admin.sh (see --help).'; END $$;
\endif

-- =============================================================================
-- Create or Reset Admin User
-- =============================================================================
-- pgcrypto's crypt(password, gen_salt('bf', 12)) produces a real bcrypt hash
-- ($2a$/$2b$), interoperable with Node's `bcrypt` package used to verify it.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO hub_users (
    email,
    username,
    password_hash,
    is_active,
    email_verified,
    created_at,
    updated_at
) VALUES (
    :'admin_email',
    :'admin_email',
    crypt(:'admin_password', gen_salt('bf', 12)),
    true,
    true,
    NOW(),
    NOW()
) ON CONFLICT (email) DO UPDATE SET
    password_hash = EXCLUDED.password_hash,
    is_active = true,
    email_verified = true,
    updated_at = NOW();

-- Global admin = member + tenant admin of the reserved `system` tenant (no is_super_admin flag).
INSERT INTO tenants (slug, display_name, is_global)
    SELECT 'system', 'System', FALSE
    WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE slug = 'system');

INSERT INTO tenant_members (tenant_id, user_id)
    SELECT t.id, u.id FROM tenants t, hub_users u
    WHERE t.slug = 'system' AND u.email = :'admin_email'
ON CONFLICT (tenant_id, user_id) DO NOTHING;

INSERT INTO tenant_admins (tenant_id, user_id, role)
    SELECT t.id, u.id, 'tenant-owner' FROM tenants t, hub_users u
    WHERE t.slug = 'system' AND u.email = :'admin_email'
ON CONFLICT (tenant_id, user_id) DO NOTHING;

-- =============================================================================
-- Create Global Community (if not exists)
-- =============================================================================
INSERT INTO communities (
    name,
    display_name,
    description,
    is_public,
    is_active,
    is_global,
    platform,
    member_count,
    created_at
) VALUES (
    'waddlebot-global',
    'Waddles Global',
    'Global community for cross-community reputation tracking. All users are automatically members.',
    true,
    true,
    true,
    'global',
    1,
    NOW()
) ON CONFLICT (name) DO UPDATE SET
    is_global = true,
    is_active = true;

-- =============================================================================
-- Add Admin to Global Community
-- =============================================================================
INSERT INTO community_members (
    community_id,
    user_id,
    role,
    is_active,
    joined_at
)
SELECT
    c.id,
    u.id,
    'admin',
    true,
    NOW()
FROM hub_users u
CROSS JOIN communities c
WHERE u.email = :'admin_email'
  AND c.name = 'waddlebot-global'
ON CONFLICT (community_id, user_id) DO UPDATE SET
    role = 'admin',
    is_active = true;

-- =============================================================================
-- Update Global Community Member Count
-- =============================================================================
UPDATE communities
SET member_count = (
    SELECT COUNT(*)
    FROM community_members
    WHERE community_id = communities.id AND is_active = true
)
WHERE name = 'waddlebot-global';

-- =============================================================================
-- Verify Admin User Created
-- =============================================================================
-- NOTE: :'var' substitution does not reach inside a $$-quoted DO body, so
-- the check runs as a plain query captured via \gset, and only the failure
-- branch (which needs no variable) uses a DO block to raise a real error.
SELECT COUNT(*) AS verify_admin_count FROM hub_users WHERE email = :'admin_email' \gset

\if :verify_admin_count
\echo 'Admin user verified successfully'
\else
DO $$ BEGIN RAISE EXCEPTION 'Failed to create admin user'; END $$;
\endif

-- Seed Complete
