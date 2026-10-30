"""SECURITY DEFINER access path to `platform_integrations` for the hub-api role (gh-969).

`waddles_hub_api` is DENIED `platform_integrations` by design (H-3: the table holds operator
bot tokens and per-community OAuth tokens, FORCE RLS, `deny_tables` in
`config/postgres/service-roles.yaml`). hub-api nevertheless needs two narrow things from it:

* **Group A -- metadata** (`pi_list_metadata`, `pi_community_status`): the superadmin
  platform-config list and the community Connections status page. The return types contain
  NO token/secret column at all (only `*_set` presence booleans), so a bug in a metadata caller
  cannot return a raw token. `config_data` is scrubbed of secret-looking keys defence-in-depth.
* **Group B -- community OAuth ciphertext** (`pi_community_secret_get`, `pi_community_upsert`,
  `pi_community_disconnect`, `pi_community_set_sealed`): the per-community OAuth lane
  (`integration_type = 'community_oauth'`, hard-coded in every function body, never a
  parameter). Ciphertext only -- AES-GCM sealing/opening stays in hub-api -- one named
  `(community_id, platform)` row per call, no scan/list variant, every call audited into
  `credential_access_log` (ids/op/outcome only, `db_user = session_user`).

NO function reads or writes `bot` or `user_oauth` rows' secrets; operator bot credentials are
unreachable from hub-api (the superadmin "test connection" check moved to the credential
manager, the sanctioned operator-secret holder).

Every function is `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp`, schema-qualifies
every object, uses no dynamic SQL, has EXECUTE revoked from PUBLIC and granted to
`waddles_hub_api` only. The definer owner is the migration owner; because `platform_integrations`
is FORCE RLS this migration ASSERTS (fail loud) that the owner bypasses RLS (superuser /
BYPASSRLS, or table owner on a non-forced table) and holds the table privileges it needs.

**Idempotent / reach.** `CREATE OR REPLACE`, so re-running is a no-op; this is an Alembic
revision, so it applies to an EXISTING database at `0068` exactly like a fresh replay.
`service-roles.yaml` also lists the callable functions under `waddles_hub_api.functions`, so
`service_roles.py reconcile --strict` (run on every migrate Job) re-grants EXECUTE if it drifts.

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

#: Functions hub-api may EXECUTE (signature -> used by `GRANT`/`REVOKE`/`DROP`).
CALLABLE_SIGNATURES: tuple[str, ...] = (
    "pi_list_metadata(text, text)",
    "pi_community_status(integer, text)",
    "pi_community_secret_get(integer, text)",
    "pi_community_upsert(integer, text, text, text, timestamptz, text[], text, smallint, integer)",
    "pi_community_disconnect(integer, text, integer)",
    "pi_community_set_sealed(integer, text, text, text, text, timestamptz, smallint, boolean)",
)

#: Internal helpers -- never granted to anyone (reachable only from inside the definer bodies).
HELPER_SIGNATURES: tuple[str, ...] = (
    "pi_scrub_config(jsonb)",
    "pi_check_args(integer, text)",
    "pi_audit(integer, text, integer, text, boolean)",
)

_ASSERT_OWNER_SQL = """
DO $assert$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['platform_integrations', 'credential_access_log'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0069: public.% is missing -- cannot define its access functions', t;
        END IF;
        IF NOT (
            EXISTS (SELECT 1 FROM pg_roles
                     WHERE rolname = current_user AND (rolsuper OR rolbypassrls))
            OR EXISTS (SELECT 1 FROM pg_class c
                        WHERE c.oid = ('public.' || t)::regclass
                          AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                          AND NOT c.relforcerowsecurity)
        ) THEN
            RAISE EXCEPTION
                '0069: definer owner % would be subject to RLS on public.% (FORCE RLS) -- the '
                'SECURITY DEFINER functions would silently see zero rows; run migrations as the '
                'DDL owner with BYPASSRLS/superuser', current_user, t;
        END IF;
        IF NOT has_table_privilege(current_user, 'public.' || t, 'SELECT,INSERT,UPDATE') THEN
            RAISE EXCEPTION '0069: % lacks SELECT/INSERT/UPDATE on public.%', current_user, t;
        END IF;
    END LOOP;
END $assert$;
"""

_HELPERS_SQL = """
CREATE OR REPLACE FUNCTION public.pi_scrub_config(p jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p IS NULL THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(p) = 'object' THEN
        RETURN coalesce(
            (SELECT jsonb_object_agg(e.key, public.pi_scrub_config(e.value))
               FROM jsonb_each(p) AS e
              WHERE e.key !~* '(secret|token|passw|credential|private|api[_-]?key|bearer)'),
            '{}'::jsonb);
    ELSIF jsonb_typeof(p) = 'array' THEN
        RETURN coalesce(
            (SELECT jsonb_agg(public.pi_scrub_config(e.value)) FROM jsonb_array_elements(p) AS e),
            '[]'::jsonb);
    END IF;
    RETURN p;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_check_args(p_community_id integer, p_platform text)
RETURNS void LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_community_id IS NULL OR p_community_id <= 0 THEN
        RAISE EXCEPTION 'pi: community_id must be a positive integer' USING ERRCODE = '22023';
    END IF;
    IF p_platform IS NULL
       OR p_platform NOT IN ('youtube', 'spotify', 'twitch', 'discord', 'kick', 'slack') THEN
        RAISE EXCEPTION 'pi: unsupported platform' USING ERRCODE = '22023';
    END IF;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_audit(
    p_integration_id integer, p_platform text, p_community_id integer,
    p_action text, p_success boolean
) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    -- ids / op / outcome only: never a token, never free text. `session_user` is the real
    -- caller (current_user is the definer inside a SECURITY DEFINER body).
    INSERT INTO public.credential_access_log
        (db_user, integration_id, platform, community_id, action, success, detail)
    VALUES (session_user::text, p_integration_id, p_platform, p_community_id,
            p_action, p_success, NULL);
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
           public.pi_scrub_config(pi.config_data), coalesce(pi.is_active, false),
           coalesce(pi.is_encrypted, true), pi.created_at::timestamptz,
           pi.updated_at::timestamptz, pi.created_by_user_id, pi.updated_by_user_id
      FROM public.platform_integrations pi
     WHERE pi.is_active IS TRUE
       AND (p_integration_type IS NULL OR pi.integration_type = p_integration_type)
       AND (p_platform IS NULL OR pi.platform = p_platform)
     ORDER BY pi.platform, pi.integration_type, pi.created_at DESC;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_status(
    p_community_id integer, p_platform text DEFAULT NULL
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
    RETURN QUERY
    SELECT pi.id, pi.platform::text, pi.community_id,
           (pi.access_token IS NOT NULL AND pi.access_token <> ''),
           pi.expires_at::timestamptz, pi.scopes, public.pi_scrub_config(pi.config_data),
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
    p_community_id integer, p_platform text
) RETURNS TABLE (
    id integer, access_token text, refresh_token text, expires_at timestamptz,
    scopes text[], is_encrypted boolean, seal_version smallint
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
    v_id integer;
BEGIN
    PERFORM public.pi_check_args(p_community_id, p_platform);
    SELECT pi.id INTO v_id
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_secret_read',
                            v_id IS NOT NULL);
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
    p_seal_version smallint, p_actor_user_id integer
) RETURNS TABLE (
    id integer, scopes text[], expires_at timestamptz, updated_at timestamptz,
    config_data jsonb
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
    v_id integer;
    v_seal smallint;
BEGIN
    PERFORM public.pi_check_args(p_community_id, p_platform);
    IF coalesce(btrim(p_access), '') = '' THEN
        RAISE EXCEPTION 'pi: access token is required' USING ERRCODE = '22023';
    END IF;
    IF p_seal_version IS NULL OR p_seal_version < 0 THEN
        RAISE EXCEPTION 'pi: invalid seal_version' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pi_community:' || p_community_id::text || ':' || p_platform, 0));
    SELECT pi.id, pi.seal_version INTO v_id, v_seal
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth'
     ORDER BY pi.is_active DESC, pi.id LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        INSERT INTO public.platform_integrations
            (platform, integration_type, community_id, user_id, access_token, refresh_token,
             client_id, client_secret, token_type, expires_at, scopes, config_data, is_active,
             is_encrypted, seal_version, created_at, updated_at, created_by_user_id,
             updated_by_user_id)
        VALUES (p_platform, 'community_oauth', p_community_id, NULL, p_access, p_refresh,
                NULL, NULL, 'Bearer', p_expires_at, coalesce(p_scopes, '{}'),
                jsonb_build_object('account_label', p_account_label), TRUE, TRUE,
                p_seal_version, now(), now(), p_actor_user_id, p_actor_user_id)
        RETURNING platform_integrations.id INTO v_id;
    ELSE
        IF p_seal_version < v_seal THEN
            RAISE EXCEPTION 'pi: seal downgrade refused' USING ERRCODE = '22023';
        END IF;
        UPDATE public.platform_integrations AS pi
           SET access_token = p_access, refresh_token = p_refresh, token_type = 'Bearer',
               expires_at = p_expires_at, scopes = coalesce(p_scopes, '{}'),
               config_data = jsonb_build_object('account_label', p_account_label),
               is_active = TRUE, is_encrypted = TRUE, seal_version = p_seal_version,
               updated_at = now(), updated_by_user_id = p_actor_user_id
         WHERE pi.id = v_id;
    END IF;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_upsert', TRUE);
    RETURN QUERY
    SELECT pi.id, pi.scopes, pi.expires_at::timestamptz, pi.updated_at::timestamptz,
           public.pi_scrub_config(pi.config_data)
      FROM public.platform_integrations pi WHERE pi.id = v_id;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_disconnect(
    p_community_id integer, p_platform text, p_actor_user_id integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id integer;
BEGIN
    PERFORM public.pi_check_args(p_community_id, p_platform);
    SELECT pi.id INTO v_id
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        RETURN FALSE;
    END IF;
    UPDATE public.platform_integrations AS pi
       SET is_active = FALSE, access_token = NULL, refresh_token = NULL, client_id = NULL,
           client_secret = NULL, scopes = '{}', config_data = NULL, updated_at = now(),
           updated_by_user_id = p_actor_user_id
     WHERE pi.id = v_id;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_disconnect', TRUE);
    RETURN TRUE;
END $fn$;

CREATE OR REPLACE FUNCTION public.pi_community_set_sealed(
    p_community_id integer, p_platform text, p_expected_access text, p_access text,
    p_refresh text, p_expires_at timestamptz, p_seal_version smallint,
    p_touch_updated_at boolean
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id integer;
    v_seal smallint;
    v_access text;
BEGIN
    PERFORM public.pi_check_args(p_community_id, p_platform);
    IF coalesce(btrim(p_access), '') = '' OR p_expected_access IS NULL
       OR p_seal_version IS NULL OR p_seal_version < 0 THEN
        RAISE EXCEPTION 'pi: access, expected access and seal_version are required'
            USING ERRCODE = '22023';
    END IF;
    SELECT pi.id, pi.seal_version, pi.access_token INTO v_id, v_seal, v_access
      FROM public.platform_integrations pi
     WHERE pi.community_id = p_community_id AND pi.platform = p_platform
       AND pi.integration_type = 'community_oauth' AND pi.is_active IS TRUE
     ORDER BY pi.id LIMIT 1 FOR UPDATE;
    -- Compare-and-set: a concurrent refresh/reseal changed the ciphertext -> caller re-reads.
    IF v_id IS NULL OR v_access IS DISTINCT FROM p_expected_access THEN
        RETURN FALSE;
    END IF;
    IF p_seal_version < v_seal THEN
        RAISE EXCEPTION 'pi: seal downgrade refused' USING ERRCODE = '22023';
    END IF;
    UPDATE public.platform_integrations AS pi
       SET access_token = p_access,
           refresh_token = coalesce(p_refresh, pi.refresh_token),
           expires_at = coalesce(p_expires_at, pi.expires_at),
           is_encrypted = TRUE, seal_version = p_seal_version,
           updated_at = CASE WHEN p_touch_updated_at THEN now() ELSE pi.updated_at END
     WHERE pi.id = v_id;
    PERFORM public.pi_audit(v_id, p_platform, p_community_id, 'community_set_sealed', TRUE);
    RETURN TRUE;
END $fn$;
"""


def _lockdown_sql() -> str:
    """REVOKE from PUBLIC on every function; GRANT EXECUTE on the callable ones to hub-api."""
    parts = [
        f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC;"
        for sig in (*HELPER_SIGNATURES, *CALLABLE_SIGNATURES)
    ]
    grants = " ".join(
        f"GRANT EXECUTE ON FUNCTION public.{sig} TO {_GRANTEE};"
        for sig in CALLABLE_SIGNATURES
    )
    # nosec B608 -- _GRANTEE and the signatures are module constants, never input
    parts.append(
        f"DO $grant$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_GRANTEE}') THEN "  # nosec B608
        f"{grants} END IF; END $grant$;"
    )
    return "\n".join(parts)


def upgrade() -> None:
    """Assert owner RLS bypass, (re)create the definer functions, lock EXECUTE down."""
    op.execute(_ASSERT_OWNER_SQL)
    op.execute(_HELPERS_SQL)
    op.execute(_GROUP_A_SQL)
    op.execute(_GROUP_B_SQL)
    op.execute(_lockdown_sql())


def downgrade() -> None:
    """Drop every function this revision creates (no data is touched)."""
    for sig in (*CALLABLE_SIGNATURES, *HELPER_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS public.{sig}")
