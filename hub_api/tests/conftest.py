"""Shared fixtures for hub-api's own tests.

`tenant_db` mirrors `libs/flask_core/tests/test_tenancy.py` and
`test_mcp_routes.py`'s own fixture exactly (in-memory pydal `tenants`
table, `migrate=True`) -- ephemeral, test-only schema. Production never
migrates `tenants` (see `app.py::_bind_reference_tables` -- schema owned
by `config/postgres/migrations/058_tenants_and_claims.sql`); tests need a
real row to resolve against, so they get their own throwaway table
instead of relaxing that production invariant.

`auth_db` (M1 Core Identity/Auth group) extends the same pattern with
`services.schema.bind_auth_tables()` -- every future port group that
needs its own tables should add its own `<group>_db` fixture here the
same way, rather than editing `tenant_db`/`auth_db` in place (keeps this
shared file append-only across the parallel port wave).

`overlay_db` (M7 Streaming/overlay+calls group) is this port's own
additive fixture, mirroring `auth_db`'s file-backed-sqlite/`migrate=True`
shape but also calling `services.schema.bind_overlay_tables()` for
`community_overlay_tokens`/`overlay_access_log` -- new tables `auth_db`
doesn't bind. Named `overlay_db` (not `streaming_db`) to avoid colliding
with the M7 Streaming (music/stream/streaming) group's own `streaming_db`
fixture below, which binds an unrelated table set via a same-named-but-
different `bind_streaming_tables()`/`bind_overlay_tables()` split (see
`services/schema.py`'s own docstring on that rename). `_seed_community`/
`_seed_membership` are this group's own seeding helpers for
`communities`/`community_members` (`services.community_access`'s authz
checks).

`ai_routing_db` (premium-AI model-routing group, greenfield) is another
additive fixture in the exact same shape as `overlay_db` -- `bind_auth_
tables()` (for `communities`/`community_members`, which `services.
community_access`'s admin/member checks query) plus `services.schema.
bind_ai_routing_tables()` (for `ai_model_config`/`ai_byok_keys`; the
`ai_token_balances`/`ai_token_transactions` tables it also defines are
unused dead schema now -- see that function's own docstring) plus
`bind_token_billing_tables()` (migration 076's real
`token_products`/`community_token_balances`/`token_transactions`, which
`services/token_ledger.py` delegates to as of the #234 reconciliation).
Seeds the `token_products` catalog rows that delegation needs before any
credit/debit can succeed (`"ai_routing_call"` -- migration 078's
production seed -- plus `"transcoding"`, exercised by `test_token_ledger.
py::TestDebitTokens::test_separate_consumable_types_have_independent_
balances`). Reuses `seed_community`/`seed_membership` unchanged -- they
take any `AsyncDAL`-like fixture with a `.dal` attribute, not just
`overlay_db` specifically.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.auth import create_jwt_token
from flask_core.database import AsyncDAL
from penguin_dal import AsyncDB
from pydal import DAL, Field
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
)

from services.schema import (
    bind_admin_tables,
    bind_ai_routing_tables,
    bind_app_bundle_tables,
    bind_auth_tables,
    bind_community_authz_tables,
    bind_github_sync_tables,
    bind_lifecycle_tables,
    bind_music_tables,
    bind_overlay_tables,
    bind_platform_tables,
    bind_privacy_tables,
    bind_streaming_tables,
    bind_superadmin_tenant_fields,
    bind_tenant_tables,
    bind_token_billing_tables,
)

#: Matches flask_core.tenancy/authz's own os.getenv("SECRET_KEY", ...) fallback.
SECRET_KEY = "change-me-in-production"

TENANT_SLUG = "acme-corp"

#: Second tenant slug -- `lifecycle_db`'s cross-tenant IDOR fixture data.
OTHER_TENANT_SLUG = "other-corp"


@pytest.fixture
def tenant_db() -> Any:
    """In-memory pydal DB with one active tenant -- `tenants.slug`/`is_active` only."""
    dal = DAL("sqlite:memory")
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("is_active", "boolean", default=True),
    )
    dal.tenants.insert(slug=TENANT_SLUG, is_active=True)
    dal.commit()
    yield dal
    dal.close()


@pytest.fixture
def auth_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` with `tenants` + every M1 auth/identity/passkey/profile table.

    Services in this group call `async_dal.select_async`/`insert_async`/etc,
    which run on `AsyncDAL`'s own `ThreadPoolExecutor` (a different OS
    thread per call, see `flask_core.database.AsyncDAL`) -- `sqlite:memory`
    (as `tenant_db` above uses) is connection-scoped, so a worker thread
    opening its own connection sees a BLANK database, not the one the main
    thread just seeded (`sqlite3.OperationalError: no such table`, caught
    the hard way -- see `hub_api/PORTING.md`'s async_dal testing gotcha). A
    `tmp_path`-backed sqlite FILE is shared across connections/threads like
    real Postgres would be, so it exercises the exact code path
    `select_async`/`insert_async`/etc actually run in production.
    """
    # pool_size=1 -- sqlite file DBs serialize writes across connections;
    # AsyncDAL's own ThreadPoolExecutor otherwise opens up to `pool_size`
    # concurrent connections, which raced into "database is locked" against
    # a plain (non-WAL) sqlite file during this suite's own test-writing.
    # Real deployments target Postgres (see this fixture's own docstring),
    # where connection pooling has no such lock-contention behavior.
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'auth_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    # `lazy_tables=True` (AsyncDAL's own default) defers each table's
    # actual `CREATE TABLE` until first ORM access -- left lazy, the FIRST
    # access could happen on an `async_dal.*_async()` worker thread mid-
    # request, racing its own `CREATE TABLE` against the main thread's
    # still-open sqlite file handle ("database is locked"). Touching every
    # table once here, still on the main thread, forces that DDL to run
    # before any worker thread exists -- a test-only concern (production
    # Postgres has no such lazy-CREATE-races-a-thread failure mode).
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


#: Second tenant slug -- exists only in `overlay_db`, for cross-tenant IDOR tests
#: (`services.community_access._require_community_in_tenant`).
OTHER_TENANT_SLUG = "other-corp"


@pytest.fixture
def overlay_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` with `tenants` + auth tables + M7's own overlay/calls tables.

    Same connection-visibility rationale as `auth_db` (see its own
    docstring) -- a fresh file, not shared with `auth_db`, so tests in
    this group don't collide with M1's own fixture instance.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'overlay_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_overlay_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def ai_routing_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` with `tenants` + auth tables + the premium-AI routing group's own.

    Same connection-visibility rationale as `auth_db`/`overlay_db` above
    -- a fresh file, not shared with any other group's fixture.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'ai_routing_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_ai_routing_tables(dal, migrate=True)
    bind_token_billing_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)
    dal.token_products.insert(
        key="ai_routing_call",
        name="AI Routing Call (Premium)",
        unit="token",
        price_cents=0,
        tokens_granted=1,
        active=True,
    )
    dal.token_products.insert(
        key="transcoding",
        name="Transcoding",
        unit="minute",
        price_cents=0,
        tokens_granted=1,
        active=True,
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def streaming_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the M7 Streaming group (music/stream/streaming).

    Additive fixture (`hub_api/PORTING.md`: "add your own bind_<group>_
    tables() call to auth_db (or a new fixture) ... additive only, never
    edit the M1 group's own fixture logic") -- extends `auth_db`'s exact
    pattern with `services.schema.bind_streaming_tables()` on top of
    `bind_auth_tables()`, since `services.community_authz` needs both M1's
    `community_members`/`tenant_admins`/`communities` tables AND M7's own
    `community_roles`/`coordination`/`community_servers`/music tables in
    the same DAL instance.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'streaming_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_streaming_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    # See auth_db's own docstring -- forces lazy-table DDL to run on the
    # main thread before any AsyncDAL executor thread exists.
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def music_station_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the Music Station queue feature.

    Additive fixture, same rationale as `streaming_db` above -- extends
    `bind_auth_tables()` with BOTH `bind_streaming_tables()` (needed for
    the `coordination`/`community_servers` category-restriction check,
    `services.community_music_queue_service._is_live_music_category()`)
    AND this feature's own `bind_music_tables()`
    (`music_tracks`/`music_station_queue`/`music_policy`/
    `music_moderation_log`).
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'music_station_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_streaming_tables(dal, migrate=True)
    bind_music_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def automation_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the M-automation port group (workflow + github_sync).

    Same file-backed-sqlite/`pool_size=1`/lazy-table-touch rationale as
    `auth_db` above -- see that fixture's own docstring for the full
    gotcha writeup. Extends `bind_auth_tables()` with
    `bind_community_authz_tables()` (`community_roles`, for
    `services.community_authz.require_community_admin()`) and
    `bind_github_sync_tables()` (`github_repo_connections` and friends).
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'automation_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_github_sync_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


def seed_community(
    overlay_db: Any, *, tenant_slug: str = TENANT_SLUG, name: str = "acme-community"
) -> int:
    """Insert one `communities` row scoped to `tenant_slug`; returns its id."""
    dal = overlay_db.dal
    tenant = dal(dal.tenants.slug == tenant_slug).select().first()
    community_id: int = dal.communities.insert(
        name=name, display_name=name, tenant_id=tenant.id, is_active=True
    )
    dal.commit()
    return community_id


def seed_membership(
    overlay_db: Any, *, community_id: int, user_id: int, role: str = "community-owner"
) -> None:
    """Insert one active `community_members` row for `user_id` in `community_id`."""
    dal = overlay_db.dal
    dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role=role, is_active=True
    )
    dal.commit()


def seed_super_admin(overlay_db: Any, *, user_id: int, username: str = "root") -> None:
    """Insert one `hub_users` row with `is_super_admin=True`, at `hub_users.id == user_id`.

    Needed by any test asserting `services.community_authz.is_super_admin()`'s
    DB-backed bypass (`community_access.require_community_admin`/
    `require_community_member`'s super-admin path) -- `make_super_admin_token`
    only mints a JWT `roles=["super_admin"]` claim, which is no longer what
    that bypass checks (security.md: `roles` is audit/display only). Callers
    must insert this BEFORE minting the matching token so `hub_users`' own
    autoincrement lands on the same id `user_id` expects (fixtures' `hub_users`
    table starts empty, so the first insert lands on id 1).
    """
    dal = overlay_db.dal
    inserted_id: int = dal.hub_users.insert(username=username, is_super_admin=True)
    dal.commit()
    assert inserted_id == user_id, (
        f"hub_users autoincrement landed on {inserted_id}, expected {user_id} -- "
        "insert this before any other hub_users row in the same fixture"
    )


@pytest.fixture
def privacy_db(tmp_path: Any) -> Any:
    """`auth_db`'s tables PLUS the Privacy/Compliance group's own.

    See `services.schema.bind_privacy_tables`.

    Same file-backed-sqlite/`pool_size=1`/eager-table-touch rationale as
    `auth_db` -- see that fixture's own docstring for the full gotcha
    writeup. A separate fixture (not a mutation of `auth_db`) per `hub_api/
    PORTING.md`'s Test pattern: additive-only, never edit another group's
    fixture in place.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'privacy_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_privacy_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def platform_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the M3 Platform-admin/Public group.

    Same `tmp_path`-backed-sqlite-file / `pool_size=1` / touch-every-table
    rationale as `auth_db` above (see its own docstring) -- additive, new
    fixture rather than editing `auth_db` in place (`hub_api/PORTING.md`'s
    Test pattern: "add your own `bind_<group>_tables()` call ... never
    edit the M1 group's own fixture logic"). `bind_platform_tables()`
    calls `bind_auth_tables()` itself, so this fixture alone covers every
    table this group's blueprints query.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'platform_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_platform_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def admin_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the M3 Platform-admin group (admin/superadmin blueprints).

    Extends `auth_db`'s exact pattern (file-backed sqlite -- see that
    fixture's own docstring for the connection-scoping/`pool_size=1`
    rationale) with `bind_admin_tables()` + `bind_superadmin_tenant_fields()`,
    additive per `hub_api/PORTING.md`'s test-pattern guidance rather than
    editing `auth_db` in place.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'admin_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_admin_tables(dal, migrate=True)
    bind_superadmin_tenant_fields(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    # See auth_db's own comment above -- forces lazy CREATE TABLE DDL to
    # run on the main thread before any async_dal worker thread exists.
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def tenant_admin_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` with every table the M2 Core Tenant group queries.

    Additive, independent of `auth_db` (own `tmp_path` sqlite file, own
    `AsyncDAL`) -- per `hub_api/PORTING.md`'s Test pattern section, a
    group needing tables `bind_auth_tables()` doesn't cover gets its own
    fixture rather than editing `auth_db` in place. Defines a narrow
    `tenants` table first (mirrors `auth_db`'s own bootstrap step -- in
    production this is `app.py::_bind_reference_tables`, off-limits to
    this port task), then calls the REAL `bind_auth_tables`/
    `bind_tenant_tables` -- the same functions `blueprints/v1/tenant.py`'s
    `_dal()` calls in production -- so this fixture exercises the actual
    binding code path (including `bind_tenant_tables`'s `redefine=True`
    extension of `tenants`/`communities`) rather than a hand-duplicated
    field list that could drift from it.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'tenant_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_tenant_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    # See auth_db's own comment above -- forces lazy CREATE TABLE DDL to
    # run on the main thread before any async_dal worker thread exists.
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def lifecycle_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the App Bundle 3-tier lifecycle group.

    Same file-backed-sqlite / `pool_size=1` / eager-table-touch rationale as
    `auth_db` above (see its own docstring). Extends `bind_auth_tables()`
    (`hub_users`/`communities`/`community_members`/`tenant_admins` --
    needed by `services.community_authz`) with `bind_community_authz_tables()`
    (`community_roles`) and this group's own `bind_lifecycle_tables()`
    (`app_catalog`/`app_tenant_availability`/`app_activations`).

    Seeds TWO tenants (`TENANT_SLUG` + `OTHER_TENANT_SLUG`) and one
    community per tenant, plus an `admin` `community_roles` row and a
    `community_members` row granting user `"1"` that role in the FIRST
    tenant's community only -- the fixture every per-community-IDOR test
    needs to prove a caller admin of one community cannot act on the
    other tenant's community. IDs are NOT returned (matches every other
    `*_db` fixture's `yield async_dal`-only shape, e.g. `automation_db`'s
    own `COMMUNITY_ID = 1` precedent in `test_community_authz.py`) --
    insertion order into a fresh sqlite file is deterministic (1-based
    autoincrement), so tests reference `LIFECYCLE_TENANT_ID = 1`,
    `LIFECYCLE_COMMUNITY_ID = 1`, `LIFECYCLE_OTHER_TENANT_ID = 2`,
    `LIFECYCLE_OTHER_COMMUNITY_ID = 2` (module-level constants below) the
    same way.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'lifecycle_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_lifecycle_tables(dal, migrate=True)

    tenant_id = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    other_tenant_id = dal.tenants.insert(
        slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True
    )
    community_id = dal.communities.insert(
        name="acme-community", display_name="Acme Community", tenant_id=tenant_id, is_active=True
    )
    dal.communities.insert(
        name="other-community",
        display_name="Other Community",
        tenant_id=other_tenant_id,
        is_active=True,
    )
    role_id = dal.community_roles.insert(
        community_id=community_id,
        name="admin",
        base_claims={"scopes": ["community:manage_members"]},
    )
    dal.community_members.insert(
        community_id=community_id,
        user_id="1",
        role="admin",
        community_role_id=role_id,
        is_active=True,
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def distribution_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the Distribution API (`blueprints/v1/distribution.py`).

    Same file-backed-sqlite/`pool_size=1`/lazy-table-touch rationale as
    `auth_db` above (see its own docstring). `bind_app_bundle_tables()`
    depends on `tenants`/`communities` already being bound -- `bind_auth_tables()`
    provides `communities`; `tenants` is defined narrowly here the same way
    every other fixture in this file bootstraps it (production's own
    `app.py::_bind_reference_tables` equivalent).
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'distribution_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_app_bundle_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


#: `lifecycle_db`'s deterministic seeded ids (fresh sqlite file, 1-based
#: autoincrement, insertion order == fixture body order above).
LIFECYCLE_TENANT_ID = 1
LIFECYCLE_OTHER_TENANT_ID = 2
LIFECYCLE_COMMUNITY_ID = 1
LIFECYCLE_OTHER_COMMUNITY_ID = 2


def make_token(*, scope: str = "", tenant: str = TENANT_SLUG, user_id: str = "u1") -> str:
    """Mint a JWT via the real `flask_core.auth.create_jwt_token` -- no hand-rolled JWTs.

    `user_id` defaults to the non-numeric `"u1"` (existing platform tests'
    expectation); Community-module tests exercising `auth_required` +
    `int(get_current_user(request)["user_id"])` pass a numeric string
    (e.g. `user_id="1"`) instead.
    """
    return create_jwt_token(
        user_id=user_id,
        username="alice",
        email="alice@example.com",
        roles=["viewer"],
        secret_key=SECRET_KEY,
        tenant=tenant,
        scope=scope,
    )


def make_user_token(*, user_id: int, scope: str = "", tenant: str = TENANT_SLUG) -> str:
    """Mint a JWT whose `sub` is a real `hub_users.id` -- for `services.current_user` callers."""
    return create_jwt_token(
        user_id=str(user_id),
        username="alice",
        email="alice@example.com",
        roles=["viewer"],
        secret_key=SECRET_KEY,
        tenant=tenant,
        scope=scope,
    )


def make_super_admin_token(*, user_id: int, scope: str = "", tenant: str = TENANT_SLUG) -> str:
    """Mint a JWT with `roles=["super_admin"]` -- `sub` matches a `seed_super_admin()` row.

    `roles=["super_admin"]` itself is no longer what
    `services.community_access.require_community_admin`/
    `require_community_member` check (that bypass is now
    `services.community_authz.is_super_admin()`, DB-backed) -- callers must
    also `seed_super_admin(db, user_id=...)` with the same `user_id` for the
    bypass to actually trigger. Kept minting the `roles` claim anyway since
    `services.community_authz`'s OTHER bypass (`require_community_admin`'s
    platform-admin check, `_jwt_roles`) still reads it.
    """
    return create_jwt_token(
        user_id=str(user_id),
        username="root",
        email="root@example.com",
        roles=["super_admin"],
        secret_key=SECRET_KEY,
        tenant=tenant,
        scope=scope,
    )


def make_user_token_with_roles(
    *, user_id: int, roles: list[str], scope: str = "", tenant: str = TENANT_SLUG
) -> str:
    """Like `make_user_token`, but with a caller-supplied `roles` claim.

    Additive helper (new function, not an edit to `make_user_token`) --
    needed by `services.community_authz`'s super-admin/platform-admin
    bypass tests (`roles=["super_admin"]`), which the fixed
    `roles=["viewer"]` in `make_user_token` can't express.
    """
    return create_jwt_token(
        user_id=str(user_id),
        username="alice",
        email="alice@example.com",
        roles=roles,
        secret_key=SECRET_KEY,
        tenant=tenant,
        scope=scope,
    )


@pytest.fixture
def auth_headers():
    """Factory fixture: `auth_headers(scope="platform:read")` -> Authorization header dict."""

    def _make(*, scope: str = "", tenant: str = TENANT_SLUG, user_id: str = "u1") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {make_token(scope=scope, tenant=tenant, user_id=user_id)}"
        }

    return _make


#: Matches `services/community_common.py::is_valid_service_key`'s `SERVICE_API_KEY` lookup.
SERVICE_API_KEY = "test-service-key"


@pytest.fixture
def community_db(tenant_db: Any, monkeypatch: Any) -> Any:
    """`tenant_db` + every Community-module (M6) table (`migrate=True`) + one seeded community.

    `ensure_community_tables(dal, migrate=True)` reuses the exact same
    field definitions production binds with `migrate=False` (see
    `community_common.py`'s docstring) -- one schema definition, not a
    second one drifting in test fixtures. Also sets `SERVICE_API_KEY` for
    the internal (X-Service-Key) endpoint tests.
    """
    from services.community_common import ensure_community_tables
    from services.community_relay import _ensure_relay_tables

    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)

    dal = tenant_db
    ensure_community_tables(dal, migrate=True)
    _ensure_relay_tables(dal, migrate=True)

    # `hub_user_identities` is a Core/Identity table (000_create_base_schema.sql),
    # not owned by the Community-module port -- `_find_hub_user_id` (community_
    # activity.py) queries it via raw `executesql`, which needs no pydal
    # `define_table` in production (the physical table already exists via
    # Core's own migration) but does need one here so sqlite:memory has a
    # real backing table to query against.
    if "hub_user_identities" not in dal.tables:
        dal.define_table(
            "hub_user_identities",
            dal.Field("hub_user_id", "integer"),
            dal.Field("platform", "string"),
            dal.Field("platform_user_id", "string"),
            migrate=True,
        )

    tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
    community_id = dal.communities.insert(name="test-community", tenant_id=tenant_row.id)
    dal.commit()
    return dal, community_id


@pytest.fixture
def service_key_headers() -> dict[str, str]:
    """`X-Service-Key` header matching `community_db`'s `SERVICE_API_KEY`."""
    return {"X-Service-Key": SERVICE_API_KEY}


#: Matches `flask_core.auth.DEFAULT_TENANT_SLUG` -- the always-visible global catalog tenant.
GLOBAL_TENANT_SLUG = "global"
#: A second, non-global tenant distinct from `TENANT_SLUG` -- proves the marketplace-catalog
#: port's tenant-scoping never leaks a THIRD tenant's rows to `TENANT_SLUG`'s caller.
OTHER_TENANT_SLUG = "other-corp"


@pytest.fixture
def marketplace_catalog_db(tmp_path: Any) -> Any:
    """File-backed pydal DB for the marketplace-catalog port group, `migrate=True`.

    Sync `dal` only, no `async_dal` -- this group never calls `*_async()`
    (see `services/marketplace_catalog_service.py`'s module docstring), so
    `hub_api/PORTING.md` Gotcha #2 (the executor-thread `sqlite:memory`
    isolation gotcha) does not apply on its own terms. A `tmp_path`-backed
    sqlite FILE is used anyway rather than `sqlite:memory` -- empirically,
    running this fixture back-to-back across ~40 tests in the same process
    as `tests/test_app_factory.py`'s own `sqlite:memory` `create_app()`
    DAL caused `test_healthz_returns_200` to fail (`pydal.DAL`'s
    thread-local connection-URI bookkeeping does not cleanly support many
    short-lived `DAL("sqlite:memory")` instances sharing one process/
    thread -- confirmed by bisecting which test file combination
    reproduced it). A uniquely-named file per test sidesteps the shared-
    URI collision entirely, matching `auth_db`'s own file-backed choice
    (for an unrelated reason -- see that fixture's docstring).

    Seeds three tenants (global, `TENANT_SLUG`, `OTHER_TENANT_SLUG`) and one
    `marketplace_catalog` row per visibility case a caller can hit: a core row
    (`tenant_id=None`, always visible), a global-tenant marketplace row (always
    visible), `TENANT_SLUG`'s own private marketplace row (visible only to
    `TENANT_SLUG`'s own caller or an anonymous global-only caller's absence
    thereof), and `OTHER_TENANT_SLUG`'s private marketplace row (must NEVER be
    visible to a `TENANT_SLUG` caller -- the cross-tenant-leak regression this
    group's port fixes over Node's unscoped original).

    Also seeds `hub_modules`/`hub_module_reviews`/`hub_module_installations`/
    `communities` for `blueprints/v1/marketplace_modules.py`'s tests. Yields
    `(dal, ids)` -- `ids` a dict of every id a test needs (`community_id`,
    `published_module_id`, `unpublished_module_id`, `core_module_id`),
    matching `community_db`'s own `(dal, community_id)` tuple-return
    convention rather than attaching ad hoc attributes to the `DAL` object.
    """
    from services.schema import bind_marketplace_catalog_tables

    dal = DAL(f"sqlite://{tmp_path / 'marketplace_catalog_test.db'}")
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("is_active", "boolean", default=True),
    )
    global_id = dal.tenants.insert(slug=GLOBAL_TENANT_SLUG, display_name="Global", is_active=True)
    tenant_id = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    other_id = dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)
    dal.commit()

    bind_marketplace_catalog_tables(dal, migrate=True)

    now = "2026-01-01 00:00:00"
    dal.marketplace_catalog.insert(
        source="core",
        source_id=1,
        name="core-widget",
        display_name="Core Widget",
        description="A core module",
        category="utility",
        is_core=True,
        pricing_type="free",
        price_cents=0,
        pricing_model="flat",
        version="1.0.0",
        author="PenguinTech",
        avg_rating=4.5,
        review_count=2,
        install_count=10,
        created_at=now,
        updated_at=now,
        tenant_id=None,
    )
    dal.marketplace_catalog.insert(
        source="marketplace",
        source_id=1,
        name="global-vendor-widget",
        display_name="Global Vendor Widget",
        description="A globally-approved vendor module",
        category="utility",
        is_core=False,
        pricing_type="free",
        price_cents=0,
        pricing_model="flat",
        version="1.0.0",
        author="Vendor A",
        avg_rating=4.0,
        review_count=1,
        install_count=5,
        created_at=now,
        updated_at=now,
        tenant_id=global_id,
    )
    dal.marketplace_catalog.insert(
        source="marketplace",
        source_id=2,
        name="acme-private-widget",
        display_name="Acme Private Widget",
        description="Acme Corp's own private vendor module",
        category="utility",
        is_core=False,
        pricing_type="paid",
        price_cents=500,
        pricing_model="flat",
        version="1.0.0",
        author="Vendor B",
        avg_rating=5.0,
        review_count=1,
        install_count=1,
        created_at=now,
        updated_at=now,
        tenant_id=tenant_id,
    )
    dal.marketplace_catalog.insert(
        source="marketplace",
        source_id=3,
        name="other-private-widget",
        display_name="Other Corp Private Widget",
        description="Other Corp's own private vendor module -- must never leak",
        category="utility",
        is_core=False,
        pricing_type="paid",
        price_cents=999,
        pricing_model="flat",
        version="1.0.0",
        author="Vendor C",
        avg_rating=3.0,
        review_count=1,
        install_count=1,
        created_at=now,
        updated_at=now,
        tenant_id=other_id,
    )

    # `hub_modules`/`hub_module_reviews`/`hub_module_installations`/`communities`
    # -- for `blueprints/v1/marketplace_modules.py` (moduleController.js port).
    # `communities` is a fresh, minimal binding local to this fixture (only
    # `name`/`display_name`/`logo_url`), never the shared `bind_auth_tables`/
    # `ensure_community_tables` definitions -- this fixture owns its own DAL
    # instance, so there is no double-`define_table` collision to guard against.
    dal.define_table(
        "communities",
        Field("name"),
        Field("display_name"),
        Field("logo_url"),
    )
    community_id = dal.communities.insert(
        name="test-community",
        display_name="Test Community",
        logo_url="https://example.com/logo.png",
    )

    published_id = dal.hub_modules.insert(
        name="published-module",
        display_name="Published Module",
        description="A published core module",
        version="1.0.0",
        author="PenguinTech",
        category="utility",
        is_published=True,
        is_core=False,
        is_featured=True,
        config_schema={"type": "object"},
        created_at=now,
        updated_at=now,
    )
    unpublished_id = dal.hub_modules.insert(
        name="unpublished-module",
        display_name="Unpublished Module",
        version="0.1.0",
        is_published=False,
        is_core=False,
        is_featured=False,
        created_at=now,
        updated_at=now,
    )
    core_module_id = dal.hub_modules.insert(
        name="core-module",
        display_name="Core Module",
        version="2.0.0",
        is_published=True,
        is_core=True,
        is_featured=False,
        created_at=now,
        updated_at=now,
    )
    dal.hub_module_reviews.insert(
        module_id=published_id, community_id=community_id, user_id=1, rating=5, created_at=now
    )
    dal.hub_module_installations.insert(
        community_id=community_id,
        module_id=published_id,
        is_enabled=True,
        installed_at=now,
        updated_at=now,
    )
    dal.commit()

    yield (
        dal,
        {
            "community_id": community_id,
            "published_module_id": published_id,
            "unpublished_module_id": unpublished_id,
            "core_module_id": core_module_id,
        },
    )
    dal.close()


@pytest.fixture
def marketplace_billing_db(tenant_db: Any) -> Any:
    """`tenant_db` + every M4 Marketplace Billing table (`migrate=True`) + one seeded community.

    Sync `dal` only (this group never calls `AsyncDAL.*_async()` -- see
    `services/marketplace_billing_service.py`'s module docstring), so
    `tenant_db`'s plain `sqlite:memory` DAL is safe to reuse directly here,
    unlike `auth_db`'s file-backed sqlite workaround for `AsyncDAL`'s
    cross-thread connection scoping.
    """
    from services.schema import bind_marketplace_billing_tables

    dal = tenant_db
    bind_marketplace_billing_tables(dal, migrate=True)

    tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
    community_id = dal.communities.insert(name="test-community", tenant_id=tenant_row.id)
    dal.marketplace_settings.insert(
        setting_key="community_premium_base_price_cents", setting_value="500"
    )
    dal.marketplace_settings.insert(
        setting_key="community_premium_base_seat_limit", setting_value="50"
    )
    dal.marketplace_settings.insert(
        setting_key="community_premium_overage_price_cents", setting_value="10"
    )
    dal.commit()
    return dal, tenant_row.id, community_id


@pytest.fixture
def user_auth_headers():
    """Factory fixture: `user_auth_headers(user_id=42)` -> Authorization header dict.

    Unlike `auth_headers` (fixed `sub="u1"`, for `blueprints/v2/platform.py`-
    style scope tests), this mints a token whose `sub` is a real
    `hub_users.id` -- required by every M1 self-service route, which
    resolves the caller via `services.current_user.get_current_user_id`.
    """

    def _make(*, user_id: int, scope: str = "", tenant: str = TENANT_SLUG) -> dict[str, str]:
        token = make_user_token(user_id=user_id, scope=scope, tenant=tenant)
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def support_token_db(tenant_db: Any) -> Any:
    """`tenant_db` + Community-module tables + support/access-token tables + one seeded community.

    Mirrors `community_db`'s shape (`ensure_community_tables(dal,
    migrate=True)`), additionally binding `services.schema.
    bind_support_token_tables(dal, migrate=True)` for the support-ticket/
    PAT/CAT port group -- same one-schema-definition-not-two rationale as
    every other `<group>_db` fixture in this file. Also seeds two
    `hub_users` rows (reporter + admin) and one `permission_scopes` catalog
    entry, both needed by every PAT/CAT/ticket test in this group.
    """
    from services.community_common import ensure_community_tables
    from services.schema import bind_support_token_tables

    dal = tenant_db
    ensure_community_tables(dal, migrate=True)
    bind_support_token_tables(dal, migrate=True)

    tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
    community_id = dal.communities.insert(name="test-community", tenant_id=tenant_row.id)
    dal.hub_users.insert(
        username="reporter", display_name="Reporter One", email="reporter@example.com"
    )
    dal.hub_users.insert(username="admin", display_name="Admin One", email="admin@example.com")
    dal.permission_scopes.insert(
        scope_key="chat:read",
        display_name="Read chat",
        description="Read chat messages",
        category="chat",
    )
    dal.commit()
    return dal, community_id


@pytest.fixture
def marketplace_db(tenant_db: Any, monkeypatch: Any) -> Any:
    """`tenant_db` + every Marketplace-vendor-port table (`migrate=True`).

    `bind_marketplace_vendor_tables(dal, migrate=True)` reuses production's
    exact field definitions (see `services/schema.py`'s own docstring) --
    additive-only fixture per `hub_api/PORTING.md`'s test-pattern
    guidance, never edits `tenant_db`/`auth_db`/`community_db` in place.
    Also sets `SERVICE_API_KEY` for the `internal_bp` (X-Service-Key)
    endpoint tests, matching `community_db`'s own precedent.
    """
    from services.schema import bind_marketplace_vendor_tables

    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)
    monkeypatch.setenv(
        "MARKETPLACE_ENCRYPTION_KEY",
        "0" * 64,  # 64 hex chars = 32 bytes, test-only key
    )

    dal = tenant_db
    bind_marketplace_vendor_tables(dal, migrate=True)
    return dal


@pytest.fixture
def token_billing_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` for the metered token billing group (migration 076).

    Same file-backed-sqlite/`pool_size=1`/eager-table-touch rationale as
    `auth_db` above (see its own docstring). `bind_auth_tables()` supplies
    every table `services.community_authz.authorize_community()` needs
    (`communities`, `community_members`, `community_roles`,
    `tenant_admins`); `bind_token_billing_tables()` adds this group's own
    three (`token_products`, `community_token_balances`,
    `token_transactions`). Two tenants seeded (`TENANT_SLUG`/
    `OTHER_TENANT_SLUG`, same pair `overlay_db` seeds) for the
    cross-tenant balance-IDOR regression test.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'token_billing_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_token_billing_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)
    dal.commit()
    # See auth_db's own comment above -- forces lazy CREATE TABLE DDL to
    # run on the main thread before any async_dal worker thread exists.
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


def seed_token_product(
    token_billing_db: Any,
    *,
    key: str = "ai_routing_call",
    name: str = "AI Routing Call",
    unit: str = "call",
    price_cents: int = 100,
    tokens_granted: int = 10,
    active: bool = True,
) -> int:
    """Insert one `token_products` row; returns its id."""
    dal = token_billing_db.dal
    product_id: int = dal.token_products.insert(
        key=key,
        name=name,
        unit=unit,
        price_cents=price_cents,
        tokens_granted=tokens_granted,
        active=active,
    )
    dal.commit()
    return product_id


def seed_token_balance(
    token_billing_db: Any, *, community_id: int, product_id: int, balance: int
) -> None:
    """Insert one `community_token_balances` row for a `(community_id, product_id)` pair."""
    dal = token_billing_db.dal
    dal.community_token_balances.insert(
        community_id=community_id, product_id=product_id, balance=balance
    )
    dal.commit()


# ---------------------------------------------------------------------------
# M2b workstreams/usage-metering group (spec Sec5.11/Sec5.12, D30/D31)
# ---------------------------------------------------------------------------
#
# R52 (coordinator ruling): "we aren't using pydal anymore for new
# tables... instead we are running penguin-dal." `ingest_sources`,
# `workstreams`, and `workstream_usage_hourly` (migrations 0020-0021) are
# this slice's own new tables, queried through `install_dal:
# penguin_dal.AsyncDB` (`services/bundle_install_dal.py`), never a new
# pydal binder. `bundle_install_db` supplies the one pydal table
# (`tenants`) these fixtures' tests need to insert against; Alembic owns
# the real DDL in production, `_create_bundle_install_tables()`
# reproduces the same three tables as sqlite-compatible SQLAlchemy Core
# schema for the unit-test fixture (no Alembic run in a unit test).

BUNDLE_TENANT_ID = 1
BUNDLE_COMMUNITY_ID = 1


@pytest.fixture
def bundle_install_db(tmp_path: Any) -> Any:
    """File-backed pydal `AsyncDAL` with a narrow `tenants` table.

    Mirrors `distribution_db`'s own narrow-tenants shape above -- the
    M2b workstreams/usage-metering slice's tests only ever need a real
    tenant row to insert `ingest_sources`/`workstreams`/
    `workstream_usage_hourly` rows against, never `communities`/
    `app_catalog`/etc, so this fixture stays intentionally smaller than
    `distribution_db`.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'bundle_install_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("is_active", "boolean", default=True),
    )
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    yield async_dal
    dal.close()


def _create_bundle_install_tables(conn: Any) -> None:
    """Synchronous SQLAlchemy Core DDL for the M2b workstreams/usage-metering tables.

    Run via `conn.run_sync()` inside `install_dal`'s `engine.begin()`
    block below. Alembic owns this DDL in real Postgres (migrations
    0020-0021); a unit test has no Alembic run, so this reproduces the
    same column set as a sqlite-compatible SQLAlchemy Core schema --
    plain generic types, no native UUID/JSONB.

    `workstream_usage_hourly.workstream_id` is a `String(36)` column
    here, not an integer FK -- the real Postgres column is `UUID`, while
    `workstreams.id` in this sqlite fixture stays an autoincrement
    integer (sqlite has no native UUID type). Every service function
    treats `workstream_id` as an opaque string on both backends, so
    `String(36)` holds either representation without a type mismatch.
    """
    metadata = MetaData()
    Table(
        "ingest_sources",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("label", String(255), nullable=False),
        Column("secret_ciphertext", LargeBinary),
        Column("secret_iv", LargeBinary),
        Column("mapping", JSON),
        Column("enabled", Boolean, server_default="1"),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    )
    Table(
        "workstreams",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("ingest_source_id", BigInteger),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("created_at", DateTime),
        Column("disabled_at", DateTime),
    )
    Table(
        "workstream_usage_hourly",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("workstream_id", String(36), nullable=False),
        Column("stage", String(20), nullable=False),
        Column("app_id", String(255)),
        Column("hour", DateTime, nullable=False),
        Column("events", BigInteger, server_default="0"),
        Column("invocations", BigInteger, server_default="0"),
        Column("host_calls", BigInteger, server_default="0"),
        Column("actions_delivered", BigInteger, server_default="0"),
        Column("fuel_ms", BigInteger, server_default="0"),
        Column("outbound_bytes", BigInteger, server_default="0"),
        Column("media_minutes", Float),
        Column("recorded_at", DateTime),
    )
    metadata.create_all(conn)


@pytest.fixture
async def install_dal(bundle_install_db: Any) -> Any:
    """`penguin_dal.AsyncDB` for every M2b workstreams/usage-metering test.

    Points at the exact same sqlite file as `bundle_install_db`
    (`bundle_install_db.uri`) so both fixtures see each other's rows in
    the same test -- `bundle_install_db` (pydal) seeds `tenants`; this
    fixture creates the three new M2b tables directly via
    `_create_bundle_install_tables()` and then calls `await install_dal.
    reflect()`, which discovers both.
    """
    install_dal = AsyncDB(bundle_install_db.uri, pool_size=1, echo=False)
    async with install_dal.engine.begin() as conn:
        await conn.run_sync(_create_bundle_install_tables)
    await install_dal.reflect()
    yield install_dal
    await install_dal.close()
