"""SECURITY DEFINER access path to `platform_integrations` for the hub-api role (gh-969).

`waddles_hub_api` is DENIED `platform_integrations` by design (H-3: the table holds operator
bot tokens and per-community OAuth tokens, FORCE RLS, `deny_tables` in
`config/postgres/service-roles.yaml`). hub-api nevertheless needs two narrow things from it:

* **Group A -- metadata** (`pi_list_metadata`, `pi_community_status`): the superadmin
  platform-config list and the community Connections status page. The return types contain
  NO token/secret column (only `*_set` presence booleans) and `config_data` is returned through
  an explicit ALLOWLIST (`pi_public_config`: only `account_label`, a string) -- never scrubbed
  raw JSON, so a secret under any other key or inside a value can never be returned.
* **Group B -- community OAuth ciphertext** (`pi_community_secret_get`, `pi_community_upsert`,
  `pi_community_disconnect`, `pi_community_set_sealed`): the per-community OAuth lane
  (`integration_type = 'community_oauth'`, hard-coded in every body, never a parameter).
  Ciphertext only -- AES-GCM sealing/opening stays in hub-api -- one named
  `(community_id, platform)` row per call, no scan/list variant.

**Authorization (defence in depth).** The DB cannot know the end user, so hub-api derives
`community_id` from the VALIDATED request scope (tenant middleware + `community_in_tenant`, or a
server-side signed OAuth `state`, or a community-bound machine JWT) -- an invariant tested in
hub-api. In addition every Group B function (and `pi_community_status`) takes the validated
`p_tenant_id` and, when supplied, REJECTS a community that does not belong to that tenant
(`communities.tenant_id`), so a bug that lets a foreign `community_id` through is also caught
here. Internal system-plane callers pass NULL (trusted, community-bound machine identity).
There is deliberately NO caller-supplied "actor" parameter: end-user attribution cannot be
verified by the DB, so it is never persisted into ownership columns.

**Audit.** EVERY Group B call writes one `credential_access_log` row -- success, not found,
stale, invalid arguments, tenant mismatch, seal downgrade -- with `db_user = session_user`,
ids, the operation and a fixed-vocabulary outcome in `detail` (never a token or free text).
Rejections are RETURNED as an `outcome`, not raised, so the audit row commits with them.

NO function reads or writes `bot` or `user_oauth` rows' secrets; operator bot credentials are
unreachable from hub-api (the superadmin "test connection" check lives in the credential
manager).

**Definer owner.** The functions are owned by a dedicated NOLOGIN, NOSUPERUSER, BYPASSRLS role
`waddles_pi_definer` holding exactly the table privileges the bodies need (so a future SQL
injection in a body is never superuser-grade). It is created/refreshed here and asserted
(fail loud). Creating a BYPASSRLS role and changing function owner require the migration role to
be a superuser -- asserted up front.

Every function is `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp`, schema-qualifies
every object, uses no dynamic SQL. ACLs are rebuilt on every run: EXECUTE revoked from PUBLIC
and from every role but the owner, granted by exact SIGNATURE to `waddles_hub_api` only, and any
stray `pi_*` overload is dropped so none can inherit a grant.

**Idempotent / reach.** `CREATE OR REPLACE`, so re-running is a no-op; this is an Alembic
revision, so it applies to an EXISTING database at `0068` exactly like a fresh replay.
`service-roles.yaml` lists the callable signatures under `waddles_hub_api.functions`, so
`service_roles.py reconcile --strict` (every migrate Job) re-grants EXECUTE by signature if it
drifts.

Revision ID: 0069_pi_definer_functions
Revises: 0068_app_catalog_kind
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op

revision = "0069_pi_definer_functions"
down_revision = "0068_app_catalog_kind"
branch_labels = None
depends_on = None

_GRANTEE = "waddles_hub_api"
_OWNER = "waddles_pi_definer"

#: Functions hub-api may EXECUTE, by exact signature (GRANT/REVOKE/DROP/ALTER OWNER).
CALLABLE_SIGNATURES: tuple[str, ...] = (
    "pi_list_metadata(text, text)",
    "pi_community_status(integer, text, integer)",
    "pi_community_secret_get(integer, text, integer)",
    "pi_community_upsert(integer, text, text, text, timestamptz, text[], text, smallint, integer)",
    "pi_community_disconnect(integer, text, integer)",
    "pi_community_set_sealed(integer, text, text, text, text, timestamptz, smallint, boolean, integer)",
)

#: Internal helpers -- never granted to anyone (reachable only from inside the definer bodies).
HELPER_SIGNATURES: tuple[str, ...] = (
    "pi_public_config(jsonb)",
    "pi_arg_reason(integer, text)",
    "pi_tenant_ok(integer, integer)",
    "pi_audit(integer, text, integer, text, boolean, text)",
)

_ROLE_SQL = (
    "-- 0069\n"  # nosec B608 -- only module constants are substituted
    """
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__OWNER__') THEN
        CREATE ROLE __OWNER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS;
    ELSE
        ALTER ROLE __OWNER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS;
    END IF;
END $role$;
GRANT USAGE ON SCHEMA public TO __OWNER__;
GRANT SELECT, INSERT, UPDATE ON public.platform_integrations TO __OWNER__;
GRANT INSERT ON public.credential_access_log TO __OWNER__;
GRANT SELECT ON public.communities TO __OWNER__;
DO $seq$
DECLARE
    s text;
BEGIN
    FOREACH s IN ARRAY ARRAY[
        pg_get_serial_sequence('public.platform_integrations', 'id'),
        pg_get_serial_sequence('public.credential_access_log', 'id')
    ] LOOP
        IF s IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SEQUENCE %s TO __OWNER__', s);
        END IF;
    END LOOP;
END $seq$;
"""
).replace("__OWNER__", _OWNER)

_ASSERT_SQL = (
    "-- 0069\n"  # nosec B608 -- only module constants are substituted
    """
DO $assert$
DECLARE
    t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) THEN
        RAISE EXCEPTION '0069: must run as a superuser DDL owner (creates the BYPASSRLS definer '
            'role and re-owns the functions); current_user is %', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__OWNER__'
                    AND rolbypassrls AND NOT rolsuper AND NOT rolcanlogin AND NOT rolcreaterole
                    AND NOT rolcreatedb AND NOT rolreplication) THEN
        RAISE EXCEPTION '0069: __OWNER__ must be NOLOGIN NOSUPERUSER BYPASSRLS (least privilege) '
            '-- without BYPASSRLS the FORCE-RLS table would silently show the definer zero rows';
    END IF;
    FOREACH t IN ARRAY ARRAY['platform_integrations', 'credential_access_log', 'communities'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0069: public.% is missing -- cannot define its access functions', t;
        END IF;
    END LOOP;
    IF NOT has_table_privilege('__OWNER__', 'public.platform_integrations', 'SELECT,INSERT,UPDATE')
       OR NOT has_table_privilege('__OWNER__', 'public.credential_access_log', 'INSERT')
       OR NOT has_table_privilege('__OWNER__', 'public.communities', 'SELECT') THEN
        RAISE EXCEPTION '0069: __OWNER__ lacks a required table privilege';
    END IF;
END $assert$;
"""
).replace("__OWNER__", _OWNER)

_HELPERS_SQL = """
CREATE OR REPLACE FUNCTION public.pi_public_config(p jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    -- ALLOWLIST, not a scrub: the only key any caller ever receives is `account_label`
    -- (written by community_connections; no other component stores config_data keys we
    -- return). Everything else -- any key, any nested value -- is dropped.
    IF p IS NULL THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(p) = 'object' AND jsonb_typeof(p -> 'account_label') = 'string' THEN
        RETURN jsonb_build_object('account_label', left(p ->> 'account_label', 200));
    END IF;
    RETURN '{}'::jsonb;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_arg_reason(p_community_id integer, p_platform text)
RETURNS text LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_community_id IS NULL OR p_community_id <= 0
       OR p_platform IS NULL
       OR p_platform NOT IN ('youtube', 'spotify', 'twitch', 'discord', 'kick', 'slack') THEN
        RETURN 'invalid_args';
    END IF;
    RETURN NULL;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_tenant_ok(p_community_id integer, p_tenant_id integer)
RETURNS boolean LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    -- NULL tenant = trusted system-plane caller (community-bound machine identity).
    IF p_tenant_id IS NULL THEN
        RETURN TRUE;
    END IF;
    RETURN EXISTS (SELECT 1 FROM public.communities c
                    WHERE c.id = p_community_id AND c.tenant_id = p_tenant_id);
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_audit(
    p_integration_id integer, p_platform text, p_community_id integer,
    p_action text, p_success boolean, p_detail text
) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    -- ids / op / fixed-vocabulary outcome only: never a token, never caller free text.
    -- `session_user` is the real caller (current_user is the definer inside a definer body).
    INSERT INTO public.credential_access_log
        (db_user, integration_id, platform, community_id, action, success, detail)
    VALUES (session_user::text, p_integration_id, p_platform, p_community_id,
            p_action, p_success, p_detail);
END $fn$;
"""

_GROUP_A_SQL = """
CREATE OR REPLACE FUNCTION public.pi_list_metadata(
    p_integration_type text DEFAULT NULL, p_platform text DEFAULT NULL
) RETURNS TABLE (
    id integer, platform text, integration_type text, community_id integer, user_id integer,
    access_token_set boolean, refresh_token_set boolean, client_id text,
    client_secret_set boolean, token_type text, expires_at timestamptz, scopes text[],
    config_data jsonb, is_active boolean, is_encrypted boolean, created_at timestamptz,
    updated_at timestamptz, created_by_user_id integer, updated_by_user_id integer
) LANGUAGE plpgsql SECURITY DEFINER STABLE SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
BEGIN
    IF p_integration_type IS NOT NULL
       AND p_integration_type NOT IN ('bot', 'user_oauth', 'community_oauth') THEN
        RAISE EXCEPTION 'pi: unsupported integration_type' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT pi.id, pi.platform::text, pi.integration_type::text, pi.community_id, pi.user_id,
           (pi.access_token IS NOT NULL AND pi.access_token <> ''),
           (pi.refresh_token IS NOT NULL AND pi.refresh_token <> ''),
           pi.client_id::text,
           (pi.client_secret IS NOT NULL AND pi.client_secret <> ''),
           pi.token_type::text, pi.expires_at::timestamptz, pi.scopes,
           public.pi_public_config(pi.config_data), coalesce(pi.is_active, false),
           coalesce(pi.is_encrypted, true), pi.created_at::timestamptz,
           pi.updated_at::timestamptz, pi.created_by_user_id, pi.updated_by_user_id
      FROM public.platform_integrations pi
     WHERE pi.is_active IS TRUE
       AND (p_integration_type IS NULL OR pi.integration_type = p_integration_type)
       AND (p_platform IS NULL OR pi.platform = p_platform)
     ORDER BY pi.platform, pi.integration_type, pi.created_at DESC;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_status(
    p_community_id integer, p_platform text DEFAULT NULL, p_tenant_id integer DEFAULT NULL
) RETURNS TABLE (
    id integer, platform text, community_id integer, access_token_set boolean,
    expires_at timestamptz, scopes text[], config_data jsonb, is_active boolean,
    created_at timestamptz, updated_at timestamptz
) LANGUAGE plpgsql SECURITY DEFINER STABLE SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
BEGIN
    IF p_community_id IS NULL OR p_community_id <= 0 THEN
        RAISE EXCEPTION 'pi: community_id must be a positive integer' USING ERRCODE = '22023';
    END IF;
    IF NOT public.pi_tenant_ok(p_community_id, p_tenant_id) THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT pi.id, pi.platform::text, pi.community_id,
           (pi.access_token IS NOT NULL AND pi.access_token <> ''),
           pi.expires_at::timestamptz, pi.scopes, public.pi_public_config(pi.config_data),
           coalesce(pi.is_active, false), pi.created_at::timestamptz, pi.updated_at::timestamptz
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id
       AND pi.integration_type = 'community_oauth'
       AND pi.is_active IS TRUE
       AND (p_platform IS NULL OR pi.platform = p_platform)
     ORDER BY pi.platform, pi.id;
END $fn$;
"""

_GROUP_B_SQL = """
CREATE OR REPLACE FUNCTION public.pi_community_secret_get(
    p_community_id integer, p_platform text, p_tenant_id integer DEFAULT NULL
) RETURNS TABLE (
    id integer, access_token text, refresh_token text, expires_at timestamptz,
    scopes text[], is_encrypted boolean, seal_version smallint
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
    v_id integer;
BEGIN
    IF public.pi_arg_reason(p_community_id, p_platform) IS NOT NULL THEN
        -- caller-supplied platform text is never logged (could be arbitrary)
        PERFORM public.pi_audit(NULL, NULL, p_community_id, 'community_secret_read', FALSE,
                                'invalid_args');
        RETURN;
    END IF;
    IF NOT public.pi_tenant_ok(p_community_id, p_tenant_id) THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_secret_read', FALSE,
                                'forbidden_tenant');
        RETURN;
    END IF;
    SELECT pi.id INTO v_id
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_secret_read',
                            v_id IS NOT NULL,
                            CASE WHEN v_id IS NULL THEN 'not_found' ELSE 'ok' END);
    IF v_id IS NULL THEN
        RETURN;
    END IF;
    -- client_id / client_secret / created_by are deliberately NOT returned: refresh uses
    -- the platform's own app credentials, never the row's (gh-969 least privilege).
    RETURN QUERY
    SELECT pi.id, pi.access_token, pi.refresh_token, pi.expires_at::timestamptz, pi.scopes,
           coalesce(pi.is_encrypted, true), pi.seal_version
      FROM public.platform_integrations pi WHERE pi.id = v_id;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_upsert(
    p_community_id integer, p_platform text, p_access text, p_refresh text,
    p_expires_at timestamptz, p_scopes text[], p_account_label text,
    p_seal_version smallint, p_tenant_id integer DEFAULT NULL
) RETURNS TABLE (
    outcome text, id integer, scopes text[], expires_at timestamptz, updated_at timestamptz,
    config_data jsonb
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
    v_id integer;
    v_seal smallint;
BEGIN
    IF public.pi_arg_reason(p_community_id, p_platform) IS NOT NULL
       OR coalesce(btrim(p_access), '') = ''
       OR p_seal_version IS NULL OR p_seal_version < 0 THEN
        PERFORM public.pi_audit(NULL, NULL, p_community_id, 'community_upsert', FALSE,
                                'invalid_args');
        RETURN QUERY SELECT 'invalid_args'::text, NULL::integer, NULL::text[],
                            NULL::timestamptz, NULL::timestamptz, NULL::jsonb;
        RETURN;
    END IF;
    IF NOT public.pi_tenant_ok(p_community_id, p_tenant_id) THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_upsert', FALSE,
                                'forbidden_tenant');
        RETURN QUERY SELECT 'forbidden_tenant'::text, NULL::integer, NULL::text[],
                            NULL::timestamptz, NULL::timestamptz, NULL::jsonb;
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pi_community:' || p_community_id::text || ':' || p_platform, 0));
    SELECT pi.id, pi.seal_version INTO v_id, v_seal
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth'
     ORDER BY pi.is_active DESC, pi.id LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        -- created_by/updated_by stay NULL: end-user attribution is not verifiable by the DB.
        INSERT INTO public.platform_integrations
            (platform, integration_type, community_id, user_id, access_token, refresh_token,
             client_id, client_secret, token_type, expires_at, scopes, config_data, is_active,
             is_encrypted, seal_version, created_at, updated_at)
        VALUES (p_platform, 'community_oauth', p_community_id, NULL, p_access, p_refresh,
                NULL, NULL, 'Bearer', p_expires_at, coalesce(p_scopes, '{}'),
                jsonb_build_object('account_label', p_account_label), TRUE, TRUE,
                p_seal_version, now(), now())
        RETURNING platform_integrations.id INTO v_id;
    ELSE
        IF p_seal_version < v_seal THEN
            PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_upsert', FALSE,
                                    'seal_downgrade');
            RETURN QUERY SELECT 'seal_downgrade'::text, NULL::integer, NULL::text[],
                                NULL::timestamptz, NULL::timestamptz, NULL::jsonb;
            RETURN;
        END IF;
        UPDATE public.platform_integrations AS pi
           SET access_token = p_access, refresh_token = p_refresh, token_type = 'Bearer',
               expires_at = p_expires_at, scopes = coalesce(p_scopes, '{}'),
               config_data = jsonb_build_object('account_label', p_account_label),
               is_active = TRUE, is_encrypted = TRUE, seal_version = p_seal_version,
               updated_at = now()
         WHERE pi.id = v_id;
    END IF;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_upsert', TRUE, 'ok');
    RETURN QUERY
    SELECT 'ok'::text, pi.id, pi.scopes, pi.expires_at::timestamptz, pi.updated_at::timestamptz,
           public.pi_public_config(pi.config_data)
      FROM public.platform_integrations pi WHERE pi.id = v_id;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_disconnect(
    p_community_id integer, p_platform text, p_tenant_id integer DEFAULT NULL
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id integer;
BEGIN
    IF public.pi_arg_reason(p_community_id, p_platform) IS NOT NULL THEN
        PERFORM public.pi_audit(NULL, NULL, p_community_id, 'community_disconnect', FALSE,
                                'invalid_args');
        RETURN 'invalid_args';
    END IF;
    IF NOT public.pi_tenant_ok(p_community_id, p_tenant_id) THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_disconnect', FALSE,
                                'forbidden_tenant');
        RETURN 'forbidden_tenant';
    END IF;
    SELECT pi.id INTO v_id
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_disconnect', FALSE,
                                'not_found');
        RETURN 'not_found';
    END IF;
    UPDATE public.platform_integrations AS pi
       SET is_active = FALSE, access_token = NULL, refresh_token = NULL, client_id = NULL,
           client_secret = NULL, scopes = '{}', config_data = NULL, updated_at = now()
     WHERE pi.id = v_id;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_disconnect', TRUE, 'ok');
    RETURN 'ok';
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_set_sealed(
    p_community_id integer, p_platform text, p_expected_access text, p_access text,
    p_refresh text, p_expires_at timestamptz, p_seal_version smallint,
    p_touch_updated_at boolean, p_tenant_id integer DEFAULT NULL
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id integer;
    v_seal smallint;
    v_access text;
BEGIN
    IF public.pi_arg_reason(p_community_id, p_platform) IS NOT NULL
       OR coalesce(btrim(p_access), '') = '' OR p_expected_access IS NULL
       OR p_seal_version IS NULL OR p_seal_version < 0 OR p_touch_updated_at IS NULL THEN
        PERFORM public.pi_audit(NULL, NULL, p_community_id, 'community_set_sealed', FALSE,
                                'invalid_args');
        RETURN 'invalid_args';
    END IF;
    IF NOT public.pi_tenant_ok(p_community_id, p_tenant_id) THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_set_sealed', FALSE,
                                'forbidden_tenant');
        RETURN 'forbidden_tenant';
    END IF;
    SELECT pi.id, pi.seal_version, pi.access_token INTO v_id, v_seal, v_access
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        PERFORM public.pi_audit(NULL, p_platform, p_community_id, 'community_set_sealed', FALSE,
                                'not_found');
        RETURN 'not_found';
    END IF;
    -- Compare-and-set: a concurrent refresh/reseal changed the ciphertext -> caller re-reads.
    IF v_access IS DISTINCT FROM p_expected_access THEN
        PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_set_sealed', FALSE,
                                'stale');
        RETURN 'stale';
    END IF;
    IF p_seal_version < v_seal THEN
        PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_set_sealed', FALSE,
                                'seal_downgrade');
        RETURN 'seal_downgrade';
    END IF;
    UPDATE public.platform_integrations AS pi
       SET access_token = p_access,
           refresh_token = coalesce(p_refresh, pi.refresh_token),
           expires_at = coalesce(p_expires_at, pi.expires_at),
           is_encrypted = TRUE, seal_version = p_seal_version,
           updated_at = CASE WHEN p_touch_updated_at THEN now() ELSE pi.updated_at END
     WHERE pi.id = v_id;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_set_sealed', TRUE, 'ok');
    RETURN 'ok';
END $fn$;
"""


def _expected_oids_sql() -> str:
    sigs = ", ".join(
        f"to_regprocedure('public.{s}')"
        for s in (*CALLABLE_SIGNATURES, *HELPER_SIGNATURES)
    )
    return f"ARRAY[{sigs}]::oid[]"


def _acl_sql() -> str:
    """Drop stray `pi_*` overloads, re-own, rebuild every ACL, grant by exact signature."""
    owner_stmts = "\n".join(
        f"ALTER FUNCTION public.{sig} OWNER TO {_OWNER};"
        for sig in (*HELPER_SIGNATURES, *CALLABLE_SIGNATURES)
    )
    grants = " ".join(
        f"GRANT EXECUTE ON FUNCTION public.{sig} TO {_GRANTEE};"
        for sig in CALLABLE_SIGNATURES
    )
    template = (
        "-- 0069\n"  # nosec B608 -- only module constants are substituted
        """
DO $drop$
DECLARE
    f record;
BEGIN
    FOR f IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
              WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'pi\\_%'
                AND NOT (p.oid = ANY(__EXPECTED__)) LOOP
        EXECUTE format('DROP FUNCTION %s', f.sig);
    END LOOP;
END $drop$;
__OWNER_STMTS__
DO $acl$
DECLARE
    f record;
    g record;
BEGIN
    FOR f IN SELECT p.oid, p.oid::regprocedure AS sig FROM pg_proc p
              WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'pi\\_%' LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f.sig);
        FOR g IN SELECT DISTINCT r.rolname FROM pg_proc p2,
                        LATERAL aclexplode(p2.proacl) a
                        JOIN pg_roles r ON r.oid = a.grantee
                  WHERE p2.oid = f.oid AND a.grantee <> 0 AND r.rolname <> '__OWNER__' LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', f.sig, g.rolname);
        END LOOP;
    END LOOP;
END $acl$;
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__GRANTEE__') THEN
        __GRANTS__
    END IF;
END $grant$;
"""
    )
    return (
        template.replace("__EXPECTED__", _expected_oids_sql())
        .replace("__OWNER_STMTS__", owner_stmts)
        .replace("__OWNER__", _OWNER)
        .replace("__GRANTEE__", _GRANTEE)
        .replace("__GRANTS__", grants)
    )


def upgrade() -> None:
    """Create/assert the definer role, (re)create the functions, rebuild ACLs by signature."""
    op.execute(_ROLE_SQL_PRE)
    op.execute(_ROLE_SQL)
    op.execute(_ASSERT_SQL)
    op.execute(_HELPERS_SQL)
    op.execute(_GROUP_A_SQL)
    op.execute(_GROUP_B_SQL)
    op.execute(_acl_sql())


#: The grants in `_ROLE_SQL` need the tables to exist; fail with a clear message first.
_ROLE_SQL_PRE = """
DO $pre$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['platform_integrations', 'credential_access_log', 'communities'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0069: public.% is missing -- cannot define its access functions', t;
        END IF;
    END LOOP;
END $pre$;
"""


def downgrade() -> None:
    """Drop every function this revision creates and the definer role (no table data touched)."""
    for sig in (*CALLABLE_SIGNATURES, *HELPER_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS public.{sig}")
    op.execute(
        (
            "-- 0069\n"  # nosec B608 -- only a module constant is substituted
            """
DO $down$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__OWNER__') THEN
        EXECUTE 'DROP OWNED BY __OWNER__';
        EXECUTE 'DROP ROLE __OWNER__';
    END IF;
END $down$;
"""
        ).replace("__OWNER__", _OWNER)
    )
