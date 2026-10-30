"""The ONLY hub-api access path to `platform_integrations` (gh-969).

`waddles_hub_api` is denied the table by design (H-3, `config/postgres/service-roles.yaml`
`deny_tables`); on Postgres every operation here calls one of the narrow SECURITY DEFINER
functions of `alembic/versions/0069_pi_definer_functions.py` -- metadata that can never carry a
secret, plus the per-community OAuth ciphertext lane (`community_oauth` rows only; operator bot
credentials and `user_oauth` rows are unreachable). Sealing/opening stays in hub-api callers;
this layer only moves ciphertext.

SQLite (the dev `DB_TYPE=sqlite` engine) has no roles or privileges, so there the same contract
is implemented directly against the pydal table -- selected by the adapter's engine, never as a
runtime fallback; any other engine raises. Every function is synchronous (call it on the DAL's
executor thread) and commits/rolls back its own transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

COMMUNITY_OAUTH = "community_oauth"
_ENGINES_POSTGRES = frozenset({"postgres", "postgresql"})

#: `config_data` keys never returned by metadata reads (mirrors `pi_scrub_config` in SQL).
_SECRET_KEY_FRAGMENTS = ("secret", "token", "passw", "credential", "private", "bearer")


@dataclass(slots=True, frozen=True)
class MetaRow:
    """One active row's non-secret metadata (`pi_list_metadata`): presence flags, no tokens."""

    id: int
    platform: str
    integration_type: str
    community_id: int | None
    user_id: int | None
    access_token_set: bool
    refresh_token_set: bool
    client_id: str | None
    client_secret_set: bool
    token_type: str | None
    expires_at: Any
    scopes: list[str] = field(default_factory=list)
    config_data: dict[str, Any] | None = None
    is_active: bool = True
    is_encrypted: bool = True
    created_at: Any = None
    updated_at: Any = None
    created_by_user_id: int | None = None
    updated_by_user_id: int | None = None


@dataclass(slots=True, frozen=True)
class StatusRow:
    """A community connection's non-secret state (`pi_community_status` / upsert result)."""

    platform: str
    scopes: list[str]
    expires_at: Any
    updated_at: Any
    config_data: dict[str, Any] | None


@dataclass(slots=True, frozen=True)
class SecretRow:
    """Ciphertext of ONE community_oauth row (`pi_community_secret_get`) -- internal only."""

    access_token: str | None
    refresh_token: str | None
    expires_at: Any
    scopes: list[str]
    is_encrypted: bool
    seal_version: int


class UnsupportedEngineError(RuntimeError):
    """The DAL's adapter is neither Postgres (definer functions) nor SQLite (dev table path)."""


def _engine(dal: Any) -> str:
    return str(dal._adapter.dbengine)


def _is_pg(dal: Any) -> bool:
    engine = _engine(dal)
    if engine in _ENGINES_POSTGRES:
        return True
    if engine == "sqlite":
        return False
    raise UnsupportedEngineError(f"platform_integrations store: unsupported DB engine {engine!r}")


def _scrub(value: Any) -> Any:
    """Drop secret-looking keys recursively (SQLite mirror of `pi_scrub_config`)."""
    if isinstance(value, dict):
        return {
            k: _scrub(v)
            for k, v in value.items()
            if not any(f in str(k).lower() for f in _SECRET_KEY_FRAGMENTS)
            and "api_key" not in str(k).lower()
            and "apikey" not in str(k).lower()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _call(dal: Any, sql: str, args: list[Any]) -> list[dict[str, Any]]:
    """Run one definer-function call; commit on success, roll back and re-raise on error."""
    try:
        rows = dal.executesql(sql, placeholders=args, as_dict=True) or []
        dal.commit()
        return list(rows)
    except Exception:
        dal.rollback()
        raise


def _status(row: dict[str, Any]) -> StatusRow:
    return StatusRow(
        platform=str(row.get("platform") or ""),
        scopes=list(row["scopes"]) if row.get("scopes") else [],
        expires_at=row.get("expires_at"),
        updated_at=row.get("updated_at"),
        config_data=row.get("config_data"),
    )


def _bind(dal: Any) -> Any:
    from services.schema import bind_platform_tables

    bind_platform_tables(dal)
    return dal.platform_integrations


# --------------------------------------------------------------------------- metadata (Group A)


def list_metadata(
    dal: Any, *, integration_type: str | None = None, platform: str | None = None
) -> list[MetaRow]:
    """Active rows' metadata, optionally filtered; NEVER any token/secret value."""
    if _is_pg(dal):
        rows = _call(dal, "SELECT * FROM pi_list_metadata(%s, %s)", [integration_type, platform])
        return [MetaRow(**{**r, "scopes": list(r["scopes"] or [])}) for r in rows]
    t = _bind(dal)
    query = t.is_active == True  # noqa: E712 - pydal idiom
    if integration_type:
        query &= t.integration_type == integration_type
    if platform:
        query &= t.platform == platform
    found = dal(query).select(orderby=t.platform | t.integration_type | ~t.created_at)
    dal.commit()
    return [
        MetaRow(
            id=r.id,
            platform=r.platform,
            integration_type=r.integration_type,
            community_id=r.community_id,
            user_id=r.user_id,
            access_token_set=bool(r.access_token),
            refresh_token_set=bool(r.refresh_token),
            client_id=r.client_id,
            client_secret_set=bool(r.client_secret),
            token_type=r.token_type,
            expires_at=r.expires_at,
            scopes=list(r.scopes) if r.scopes else [],
            config_data=_scrub(r.config_data),
            is_active=bool(r.is_active),
            is_encrypted=bool(r.is_encrypted),
            created_at=r.created_at,
            updated_at=r.updated_at,
            created_by_user_id=r.created_by_user_id,
            updated_by_user_id=r.updated_by_user_id,
        )
        for r in found
    ]


def community_status(dal: Any, community_id: int, platform: str | None = None) -> list[StatusRow]:
    """Active community_oauth rows' non-secret state for one community."""
    if _is_pg(dal):
        rows = _call(dal, "SELECT * FROM pi_community_status(%s, %s)", [community_id, platform])
        return [_status(r) for r in rows]
    t = _bind(dal)
    query = (
        (t.community_id == community_id)
        & (t.integration_type == COMMUNITY_OAUTH)
        & (t.is_active == True)  # noqa: E712 - pydal idiom
    )
    if platform:
        query &= t.platform == platform
    found = dal(query).select(orderby=t.platform | t.id)
    dal.commit()
    return [
        StatusRow(
            platform=r.platform,
            scopes=list(r.scopes) if r.scopes else [],
            expires_at=r.expires_at,
            updated_at=r.updated_at,
            config_data=_scrub(r.config_data),
        )
        for r in found
    ]


# --------------------------------------------------------------- community OAuth lane (Group B)


def get_secret(dal: Any, community_id: int, platform: str) -> SecretRow | None:
    """Ciphertext of the one active `(community_id, platform)` community_oauth row (audited)."""
    if _is_pg(dal):
        rows = _call(dal, "SELECT * FROM pi_community_secret_get(%s, %s)", [community_id, platform])
        if not rows:
            return None
        r = rows[0]
        return SecretRow(
            access_token=r["access_token"],
            refresh_token=r["refresh_token"],
            expires_at=r["expires_at"],
            scopes=list(r["scopes"] or []),
            is_encrypted=bool(r["is_encrypted"]),
            seal_version=int(r["seal_version"] or 0),
        )
    t = _bind(dal)
    row = dal(_active(t, community_id, platform)).select(limitby=(0, 1)).first()
    dal.commit()
    if row is None:
        return None
    return SecretRow(
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        expires_at=row.expires_at,
        scopes=list(row.scopes) if row.scopes else [],
        is_encrypted=bool(row.is_encrypted),
        seal_version=int(row.seal_version or 0),
    )


def upsert(
    dal: Any,
    *,
    community_id: int,
    platform: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: list[str],
    account_label: str | None,
    seal_version: int,
    actor_user_id: int | None,
) -> StatusRow:
    """Insert-or-update the community's sealed OAuth row; refuses a seal downgrade."""
    if _is_pg(dal):
        rows = _call(
            dal,
            "SELECT * FROM pi_community_upsert(%s, %s, %s, %s, %s, %s, %s, %s::smallint, %s)",
            [
                community_id,
                platform,
                access_token,
                refresh_token,
                expires_at,
                scopes,
                account_label,
                seal_version,
                actor_user_id,
            ],
        )
        return _status({**rows[0], "platform": platform})
    t = _bind(dal)
    now = datetime.now(UTC)
    try:
        q = (
            (t.community_id == community_id)
            & (t.platform == platform)
            & (t.integration_type == COMMUNITY_OAUTH)
        )
        existing = dal(q).select(limitby=(0, 1), orderby=~t.is_active | t.id).first()
        config = {"account_label": account_label}
        if existing is None:
            row_id = int(
                t.insert(
                    platform=platform,
                    integration_type=COMMUNITY_OAUTH,
                    community_id=community_id,
                    user_id=None,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    client_id=None,
                    client_secret=None,
                    token_type="Bearer",  # noqa: S106
                    expires_at=expires_at,
                    scopes=scopes,
                    config_data=config,
                    is_active=True,
                    is_encrypted=True,
                    seal_version=seal_version,
                    created_at=now,
                    updated_at=now,
                    created_by_user_id=actor_user_id,
                    updated_by_user_id=actor_user_id,
                )
            )
        else:
            if seal_version < int(existing.seal_version or 0):
                raise ValueError("pi: seal downgrade refused")
            row_id = int(existing.id)
            dal(t.id == row_id).update(
                access_token=access_token,
                refresh_token=refresh_token,
                token_type="Bearer",  # noqa: S106
                expires_at=expires_at,
                scopes=scopes,
                config_data=config,
                is_active=True,
                is_encrypted=True,
                seal_version=seal_version,
                updated_at=now,
                updated_by_user_id=actor_user_id,
            )
        refreshed = dal(t.id == row_id).select().first()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return StatusRow(
        platform=platform,
        scopes=list(refreshed.scopes) if refreshed.scopes else [],
        expires_at=refreshed.expires_at,
        updated_at=refreshed.updated_at,
        config_data=_scrub(refreshed.config_data),
    )


def disconnect(dal: Any, *, community_id: int, platform: str, actor_user_id: int | None) -> bool:
    """Deactivate the active row and null every secret; `False` when nothing was active."""
    if _is_pg(dal):
        rows = _call(
            dal,
            "SELECT pi_community_disconnect(%s, %s, %s) AS ok",
            [community_id, platform, actor_user_id],
        )
        return bool(rows[0]["ok"])
    t = _bind(dal)
    try:
        row = dal(_active(t, community_id, platform)).select(limitby=(0, 1)).first()
        if row is None:
            dal.commit()
            return False
        dal(t.id == row.id).update(
            is_active=False,
            access_token=None,
            refresh_token=None,
            client_id=None,
            client_secret=None,
            scopes=[],
            config_data=None,
            updated_at=datetime.now(UTC),
            updated_by_user_id=actor_user_id,
        )
        dal.commit()
        return True
    except Exception:
        dal.rollback()
        raise


def set_sealed(
    dal: Any,
    *,
    community_id: int,
    platform: str,
    expected_access: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    seal_version: int,
    touch_updated_at: bool,
) -> bool:
    """Compare-and-set re-seal/refresh: `False` if the stored ciphertext changed meanwhile.

    `refresh_token` / `expires_at` of `None` leave the stored value unchanged.
    """
    if _is_pg(dal):
        rows = _call(
            dal,
            "SELECT pi_community_set_sealed(%s, %s, %s, %s, %s, %s, %s::smallint, %s) AS ok",
            [
                community_id,
                platform,
                expected_access,
                access_token,
                refresh_token,
                expires_at,
                seal_version,
                touch_updated_at,
            ],
        )
        return bool(rows[0]["ok"])
    t = _bind(dal)
    try:
        row = dal(_active(t, community_id, platform)).select(limitby=(0, 1)).first()
        if row is None or row.access_token != expected_access:
            dal.commit()
            return False
        if seal_version < int(row.seal_version or 0):
            raise ValueError("pi: seal downgrade refused")
        values: dict[str, Any] = {
            "access_token": access_token,
            "is_encrypted": True,
            "seal_version": seal_version,
        }
        if refresh_token is not None:
            values["refresh_token"] = refresh_token
        if expires_at is not None:
            values["expires_at"] = expires_at
        if touch_updated_at:
            values["updated_at"] = datetime.now(UTC)
        dal(t.id == row.id).update(**values)
        dal.commit()
        return True
    except Exception:
        dal.rollback()
        raise


def _active(t: Any, community_id: int, platform: str) -> Any:
    return (
        (t.community_id == community_id)
        & (t.platform == platform)
        & (t.integration_type == COMMUNITY_OAUTH)
        & (t.is_active == True)  # noqa: E712 - pydal idiom
    )
