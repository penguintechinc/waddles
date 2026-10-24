"""Connection service: the orchestration layer between the HTTP API, adapters, vault and store.

One class, :class:`ProviderConnectionService`, owns every flow so the blueprint stays a thin
translation layer and the rules live in exactly one place:

* **create / update / delete** (community-scoped, called after the blueprint has authenticated the
  caller, resolved their tenant from the JWT and checked they administer the community). The
  service still passes ``tenant_id`` to every store call -- isolation never depends on the caller
  having remembered a check.
* **list_for_dataplane / resolve** (operator plane): how ``svc-ingest`` and friends learn which
  connections to run and obtain a *fresh* access token for one. Only here is a token decrypted for
  a caller, and only the access token -- the refresh token and the OAuth app secret never leave
  hub-api (docs/CONNECTION_MODEL.local.md "Data-plane poll").

Hard rules enforced in this file:

* A provider is contacted only after shape validation, and never while a database transaction is
  open -- except the single, deliberate one in :meth:`_fresh_token`, which holds the row lock so a
  single-use refresh token is never spent twice.
* Secrets are sealed before they reach the store and opened at the last moment; no log line, span
  attribute, metric label or error message ever carries one.
* A provider rejecting a refresh token marks the link ``expired`` (reason ``refresh_rejected``) and
  keeps the row so the admin can reconnect; a *transient* provider failure changes nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from services.platform_integrations_crypto import platform_connection_aad
from services.provider_connections import store
from services.provider_connections.adapter import (
    AdapterRegistry,
    AdapterSpec,
    Credentials,
    ProviderAdapter,
    validate_credentials_shape,
    validate_metadata,
    validate_token_shape,
)
from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    ConnectionNotUsable,
    InvalidConnectionInput,
    ProviderConnectionError,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    UnknownProvider,
    VaultCorrupt,
)
from services.provider_connections.kinds import ConnectionKind, SecretScheme, UnknownSecretScheme
from services.provider_connections.static_secret import (
    StaticSecretAdapter,
    reject_control_characters,
    validate_secret_document,
)
from services.provider_connections.store import Binding, ResolveState, SealedSecrets
from services.provider_connections.telemetry import (
    dataplane_list_size,
    log_failure,
    operation_span,
    token_refresh_counter,
)
from services.provider_connections.vault import (
    SEAL_AAD,
    SEAL_LEGACY,
    TokenVault,
    reseal_if_legacy,
)

logger = logging.getLogger(__name__)

#: Refresh a token this long before it expires so a caller never holds one that dies in flight.
DEFAULT_REFRESH_SKEW: Final = timedelta(seconds=120)
#: Hard ceiling on one data-plane list page.
MAX_PAGE_SIZE: Final = 1000


@dataclass(slots=True, frozen=True)
class ConnectionView:
    """One connection as a community admin sees it. Carries no secret, only ``has_refresh_token``.

    ``spec`` is ``None`` only for a stored link whose provider has no registered adapter any more
    (listed so it can still be seen and deleted, never hidden).
    """

    binding: Binding
    spec: AdapterSpec | None
    public_metadata: dict[str, Any]
    capabilities: list[str]
    usable: bool


@dataclass(slots=True, frozen=True)
class DataPlaneBinding:
    """One (community, connection) pair the data plane should be running. No token."""

    grant_id: int
    connection_id: int
    community_id: int
    tenant_id: int
    provider: str
    resource_type: str
    resource_id: str
    capabilities: list[str]
    expires_at: datetime | None
    updated_at: datetime | None
    metadata: dict[str, Any]


@dataclass(slots=True, frozen=True)
class DataPlanePage:
    """One page of :class:`DataPlaneBinding`; ``next_after`` is ``None`` on the last page."""

    items: list[DataPlaneBinding]
    next_after: int | None


@dataclass(slots=True, frozen=True)
class ResolvedCredentials:
    """A fresh access token for the data plane. ``access_token`` is excluded from ``repr``."""

    connection_id: int
    community_id: int
    provider: str
    access_token: str = field(repr=False)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderConnectionService:
    """Create, change, list and resolve provider connections for communities."""

    def __init__(
        self,
        db: Any,
        registry: AdapterRegistry,
        vault: TokenVault,
        *,
        clock: Callable[[], datetime] | None = None,
        refresh_skew: timedelta = DEFAULT_REFRESH_SKEW,
    ) -> None:
        """Bind the penguin-dal handle, adapter registry and vault (all injectable for tests)."""
        self._db = db
        self._registry = registry
        self._vault = vault
        self._clock = clock or (lambda: datetime.now(UTC))
        self._refresh_skew = refresh_skew

    # -- descriptors -----------------------------------------------------------------------

    def providers(self) -> list[AdapterSpec]:
        """Every registered provider's spec; the API renders these into install descriptors."""
        return self._registry.specs()

    def provider_kind(self, provider: str) -> ConnectionKind:
        """The :class:`ConnectionKind` a provider creates (:class:`UnknownProvider` if unknown)."""
        return self._registry.get(provider).spec.kind

    # -- admin: create ---------------------------------------------------------------------

    async def create(
        self,
        *,
        tenant_id: int,
        community_id: int,
        actor_user_id: int | None,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        metadata: Mapping[str, Any],
        enabled: bool,
    ) -> ConnectionView:
        """Verify the credential with the provider, resolve the resource, then store it sealed."""
        adapter = self._registry.get(provider)
        spec = adapter.spec
        if spec.kind is not ConnectionKind.PLATFORM_OAUTH:
            raise InvalidConnectionInput("this provider takes a secret document, not credentials")
        with operation_span(provider, "create"):
            clean_metadata = validate_metadata(spec, metadata)
            credentials = adapter.normalize_credentials(
                validate_credentials_shape(spec, access_token, refresh_token)
            )
            validate_token_shape("access_token", credentials.access_token)
            if credentials.refresh_token and not spec.refreshable:
                raise InvalidConnectionInput("this provider does not use a refresh token")
            owner = await store.community_tenant_id(self._db, community_id)
            if owner != tenant_id:
                # Same answer for "no such community" and "someone else's": no existence oracle,
                # and no provider call is spent on a community the caller cannot touch.
                raise ConnectionNotFound("community not found")

            account = await adapter.validate_credentials(credentials)
            resource = await adapter.resolve_resource(credentials, account, clean_metadata)
            facts = self._compose_facts(adapter, account.scopes, resource.facts)
            facts.update(self._metadata_inputs(spec, clean_metadata))
            now = self._clock()
            # The row's immutable identity is minted first: it is part of the tokens' AAD (M4).
            public_id = uuid4()
            aad = platform_connection_aad(public_id=public_id, platform=spec.platform)
            binding = await store.insert_connection(
                self._db,
                tenant_id=tenant_id,
                community_id=community_id,
                platform=spec.platform,
                resource_type=spec.resource_type,
                resource_id=resource.resource_id,
                public_id=public_id,
                secrets=SealedSecrets(
                    access_token=self._vault.seal(credentials.access_token, aad=aad),
                    refresh_token=(
                        self._vault.seal(credentials.refresh_token, aad=aad)
                        if credentials.refresh_token
                        else None
                    ),
                    seal_version=SEAL_AAD,
                ),
                expires_at=account.expires_at,
                resource_metadata=facts,
                installed_by_user_id=actor_user_id,
                enabled=enabled,
                now=now,
            )
            if account.expires_at is not None and not credentials.refresh_token:
                logger.info(
                    "provider_connections.created_without_refresh provider=%s connection_id=%d "
                    "-- it stops working when the access token expires",
                    provider,
                    binding.connection.id,
                )
            logger.info(
                "provider_connections.created provider=%s connection_id=%d community_id=%d "
                "enabled=%s",
                provider,
                binding.connection.id,
                community_id,
                enabled,
            )
            return self._view(binding)

    async def create_static(
        self,
        *,
        tenant_id: int,
        community_id: int,
        actor_user_id: int | None,
        provider: str,
        secret_scheme: int,
        secret_document: Mapping[str, Any],
        metadata: Mapping[str, Any],
        enabled: bool,
    ) -> ConnectionView:
        """Create an external-http / aws-lambda / gcp-function connection from a secret document.

        Everything is validated structurally (no outbound call -- host policy is the dispatcher's
        send-time job), the JSON document is sealed with the row-bound AAD, and the row is stored
        with ``connection_kind`` set, ``resource_id = public_id`` and only non-secret target facts
        in ``resource_metadata``. The secret is never echoed, logged or returned.
        """
        adapter = self._registry.get(provider)
        spec = adapter.spec
        if spec.kind is ConnectionKind.PLATFORM_OAUTH or not isinstance(
            adapter, StaticSecretAdapter
        ):
            raise InvalidConnectionInput("this provider takes OAuth credentials, not a secret")
        with operation_span(provider, "create_static"):
            try:
                scheme = SecretScheme.from_code(secret_scheme)
            except UnknownSecretScheme:
                raise InvalidConnectionInput("unknown secret scheme") from None
            if scheme not in spec.secret_schemes:
                raise InvalidConnectionInput("secret scheme is not supported by this provider")
            document = validate_secret_document(scheme, secret_document)
            reject_control_characters(metadata)
            facts = adapter.validate_target(validate_metadata(spec, metadata))
            owner = await store.community_tenant_id(self._db, community_id)
            if owner != tenant_id:
                raise ConnectionNotFound("community not found")
            facts = {
                **facts,
                "secret_scheme": int(scheme),
                "scopes": ["dispatch"],
                "capabilities": ["dispatch"],
            }
            public_id = uuid4()
            aad = platform_connection_aad(public_id=public_id, platform=spec.platform)
            sealed = self._vault.seal(
                json.dumps(document, sort_keys=True, separators=(",", ":")), aad=aad
            )
            binding = await store.insert_connection(
                self._db,
                tenant_id=tenant_id,
                community_id=community_id,
                platform=spec.platform,
                resource_type=spec.resource_type,
                resource_id=str(public_id),
                public_id=public_id,
                secrets=SealedSecrets(
                    access_token=None,
                    refresh_token=None,
                    seal_version=SEAL_AAD,
                    sealed_secret=sealed,
                ),
                expires_at=None,
                resource_metadata=facts,
                installed_by_user_id=actor_user_id,
                enabled=enabled,
                now=self._clock(),
                connection_kind=int(spec.kind),
            )
            logger.info(
                "provider_connections.created_static provider=%s connection_id=%d "
                "community_id=%d scheme=%d enabled=%s",
                provider,
                binding.connection.id,
                community_id,
                int(scheme),
                enabled,
            )
            return self._view(binding)

    # -- admin: read -----------------------------------------------------------------------

    async def list(self, *, tenant_id: int, community_id: int) -> list[ConnectionView]:
        """This community's connections (tenant-scoped). No secrets are ever loaded."""
        bindings = await store.list_bindings(
            self._db, tenant_id=tenant_id, community_id=community_id
        )
        logger.debug(
            "provider_connections.listed community_id=%d count=%d", community_id, len(bindings)
        )
        return [self._view(binding) for binding in bindings]

    async def get(self, *, tenant_id: int, community_id: int, connection_id: int) -> ConnectionView:
        """One connection, or :class:`ConnectionNotFound` if it is not visible to the caller."""
        binding = await store.get_binding(
            self._db, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
        if binding is None:
            raise ConnectionNotFound("connection not found")
        return self._view(binding)

    # -- admin: update ---------------------------------------------------------------------

    async def update(
        self,
        *,
        tenant_id: int,
        community_id: int,
        actor_user_id: int | None,
        connection_id: int,
        enabled: bool | None,
        metadata: Mapping[str, Any] | None,
        access_token: str | None,
        refresh_token: str | None,
    ) -> ConnectionView:
        """Toggle ``enabled``, re-target the resource, and/or rotate the credential.

        ``enabled`` is this community's own switch and always allowed. Changing the metadata or
        credential edits the *shared link*, so it is refused while another community uses it.
        """
        if enabled is None and metadata is None and access_token is None:
            raise InvalidConnectionInput("nothing to update")
        if refresh_token is not None and access_token is None:
            raise InvalidConnectionInput(
                "refresh_token can only be replaced together with access_token"
            )
        binding = await store.get_binding(
            self._db, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
        if binding is None:
            raise ConnectionNotFound("connection not found")
        adapter = self._registry.get(binding.connection.platform)
        spec = adapter.spec
        if spec.kind is not ConnectionKind.PLATFORM_OAUTH and (
            metadata is not None or access_token is not None
        ):
            raise InvalidConnectionInput(
                "a static-secret connection cannot be edited; delete it and create a new one"
            )
        with operation_span(spec.provider, "update"):
            changes_link = metadata is not None or access_token is not None
            if changes_link and binding.shared_with > 0:
                raise ConnectionConflict(
                    "this connection is shared with other communities and cannot be changed here"
                )
            if changes_link:
                binding = await self._update_link(
                    adapter,
                    binding,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    metadata=metadata,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )
            if enabled is not None and enabled != binding.grant.enabled:
                updated = await store.set_enabled(
                    self._db,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    connection_id=connection_id,
                    enabled=enabled,
                    now=self._clock(),
                )
                if updated is None:
                    raise ConnectionNotFound("connection not found")
                binding = updated
            logger.info(
                "provider_connections.updated provider=%s connection_id=%d community_id=%d "
                "link_changed=%s enabled=%s",
                spec.provider,
                connection_id,
                community_id,
                changes_link,
                binding.grant.enabled,
            )
            return self._view(binding)

    async def _update_link(
        self,
        adapter: ProviderAdapter,
        binding: Binding,
        *,
        tenant_id: int,
        community_id: int,
        metadata: Mapping[str, Any] | None,
        access_token: str | None,
        refresh_token: str | None,
    ) -> Binding:
        """Re-verify against the provider, then rewrite the (unshared) link in one transaction."""
        spec = adapter.spec
        stored = binding.connection.resource_metadata
        if metadata is not None:
            clean_metadata = validate_metadata(spec, metadata)
        else:
            clean_metadata = {
                item.name: str(stored[item.name])
                for item in spec.metadata_fields
                if item.name in stored
            }
        values: dict[str, Any] = {}
        if access_token is not None:
            credentials = adapter.normalize_credentials(
                validate_credentials_shape(spec, access_token, refresh_token)
            )
            validate_token_shape("access_token", credentials.access_token)
            if credentials.refresh_token and not spec.refreshable:
                raise InvalidConnectionInput("this provider does not use a refresh token")
            account = await adapter.validate_credentials(credentials)
            aad = self._row_aad(binding.connection)
            values["access_token"] = self._vault.seal(credentials.access_token, aad=aad)
            values["refresh_token"] = (
                self._vault.seal(credentials.refresh_token, aad=aad)
                if credentials.refresh_token
                else None
            )
            values["seal_version"] = SEAL_AAD
            values["expires_at"] = account.expires_at
            values["status"] = store.STATUS_ACTIVE
            values["status_reason"] = None
        else:
            # Metadata-only change: verify with the connection's own (refreshed if stale) token.
            plaintext, _ = await self._fresh_token(
                adapter, binding.connection.id, community_id=community_id
            )
            credentials = Credentials(access_token=plaintext)
            account = await adapter.validate_credentials(credentials)
        resource = await adapter.resolve_resource(credentials, account, clean_metadata)
        if metadata is None and resource.resource_id != binding.connection.resource_id:
            raise ConnectionConflict("those credentials authorize a different resource")
        facts = self._compose_facts(adapter, account.scopes, resource.facts)
        facts.update(self._metadata_inputs(spec, clean_metadata))
        values["resource_id"] = resource.resource_id
        values["resource_metadata"] = facts
        return await store.update_link(
            self._db,
            tenant_id=tenant_id,
            community_id=community_id,
            connection_id=binding.connection.id,
            values=values,
            now=self._clock(),
        )

    # -- admin: delete ---------------------------------------------------------------------

    async def delete(self, *, tenant_id: int, community_id: int, connection_id: int) -> bool:
        """Disconnect this community; ``True`` when the shared link (and its tokens) was purged."""
        binding = await store.get_binding(
            self._db, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
        if binding is None:
            raise ConnectionNotFound("connection not found")
        provider = binding.connection.platform
        with operation_span(provider, "delete"):
            outcome = await store.delete_for_community(
                self._db,
                tenant_id=tenant_id,
                community_id=community_id,
                connection_id=connection_id,
            )
            if outcome is None:
                raise ConnectionNotFound("connection not found")
            logger.info(
                "provider_connections.deleted provider=%s connection_id=%d community_id=%d "
                "purged=%s",
                provider,
                connection_id,
                community_id,
                outcome.purged_connection,
            )
            return outcome.purged_connection

    # -- data plane ------------------------------------------------------------------------

    async def list_for_dataplane(
        self, *, provider: str, capability: str | None, after: int, limit: int
    ) -> DataPlanePage:
        """Usable (approved + enabled + active) bindings for ``provider``, across all tenants.

        ``capability`` filters on what the stored scopes actually grant; the cursor is the last
        *scanned* grant id so a filtered page can be short without ending the scan.
        """
        adapter = self._registry.get(provider)
        if capability is not None and capability not in adapter.spec.capabilities:
            raise InvalidConnectionInput("unknown capability for this provider")
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        with operation_span(provider, "dataplane_list"):
            rows = await store.list_dataplane(
                self._db, platform=adapter.spec.platform, after_grant_id=after, limit=limit
            )
            items: list[DataPlaneBinding] = []
            for row in rows:
                capabilities = _capabilities_of(row.connection.resource_metadata)
                if capability is not None and capability not in capabilities:
                    continue
                items.append(self._dataplane_item(adapter.spec, row))
            dataplane_list_size.record(len(items), {"provider": provider})
            next_after = rows[-1].grant.id if len(rows) == limit else None
            logger.debug(
                "provider_connections.dataplane_listed provider=%s returned=%d scanned=%d",
                provider,
                len(items),
                len(rows),
            )
            return DataPlanePage(items=items, next_after=next_after)

    async def resolve(self, *, community_id: int, connection_id: int) -> ResolvedCredentials:
        """Hand the data plane a fresh access token for one usable binding (refresh if stale)."""
        state = await store.read_resolve_state(
            self._db, community_id=community_id, connection_id=connection_id
        )
        if state is None:
            raise ConnectionNotFound("connection not found")
        provider = state.connection.platform
        adapter = self._registry.get(provider)
        if state.connection.connection_kind != int(ConnectionKind.PLATFORM_OAUTH):
            # Fail loud: a static row has no OAuth token to hand out, and must never fall through
            # to the token-opening path (its access_token is NULL by construction).
            raise ConnectionNotUsable(
                "a static-secret connection has no OAuth token to resolve", reason="static_secret"
            )
        with operation_span(provider, "resolve"):
            self._assert_usable(state)
            plaintext, connection = await self._fresh_token(
                adapter, connection_id, community_id=community_id, state=state
            )
            allowed = adapter.spec.runtime_metadata_keys
            return ResolvedCredentials(
                connection_id=connection_id,
                community_id=community_id,
                provider=provider,
                access_token=plaintext,
                expires_at=connection.expires_at,
                metadata={k: v for k, v in connection.resource_metadata.items() if k in allowed},
            )

    # -- internals -------------------------------------------------------------------------

    def _needs_refresh(self, expires_at: datetime | None) -> bool:
        return expires_at is not None and expires_at <= self._clock() + self._refresh_skew

    @staticmethod
    def _assert_usable(state: ResolveState) -> None:
        """Raise :class:`ConnectionNotUsable` with a precise fixed reason, else return."""
        grant, connection = state.grant, state.connection
        if grant.status != store.GRANT_APPROVED:
            reason = "pending_approval" if grant.status == "pending" else "revoked_grant"
            raise ConnectionNotUsable("this community's access is not approved", reason=reason)
        if not grant.enabled:
            raise ConnectionNotUsable(
                "this connection is disabled for the community", reason="disabled"
            )
        if connection.status != store.STATUS_ACTIVE:
            raise ConnectionNotUsable(
                "the connection needs to be reconnected",
                reason=connection.status_reason or f"connection_{connection.status}",
            )

    async def _fresh_token(
        self,
        adapter: ProviderAdapter,
        connection_id: int,
        *,
        community_id: int,
        state: ResolveState | None = None,
    ) -> tuple[str, store.ConnectionRow]:
        """Return a plaintext access token that is not about to expire, refreshing under a lock.

        Fast path (no lock): the stored token is still good -> decrypt and return. Otherwise take
        the row lock, re-read (another worker may have rotated it while we queued), and only then
        spend the refresh token. A rejected refresh marks the link expired (kept, so the admin can
        reconnect); a transient provider failure rolls everything back and changes nothing.
        """
        provider = adapter.spec.provider
        if state is None:
            state = await store.read_resolve_state(
                self._db, community_id=community_id, connection_id=connection_id
            )
            if state is None:
                raise ConnectionNotFound("connection not found")
        if not self._needs_refresh(state.connection.expires_at):
            access = self._open(state.secrets.access_token, state)
            await self._reseal_if_legacy(state)
            return access, state.connection

        failure: ConnectionNotUsable | None = None
        async with store.lock_for_refresh(
            self._db, community_id=community_id, connection_id=connection_id
        ) as locked:
            if locked is None:
                raise ConnectionNotFound("connection not found")
            current = locked.state
            if not self._needs_refresh(current.connection.expires_at):
                token_refresh_counter.add(1, {"provider": provider, "outcome": "already_rotated"})
                return self._open(current.secrets.access_token, current), current.connection
            self._assert_usable_connection(current)
            now = self._clock()
            if current.secrets.refresh_token is None or not adapter.spec.refreshable:
                await locked.mark_unusable(status="expired", reason="token_expired", now=now)
                token_refresh_counter.add(1, {"provider": provider, "outcome": "no_refresh_token"})
                failure = ConnectionNotUsable(
                    "the access token expired and there is no refresh token", reason="token_expired"
                )
            else:
                try:
                    refreshed = await adapter.refresh(
                        self._open(current.secrets.refresh_token, current),
                        current.connection.resource_metadata,
                    )
                except RefreshRejected:
                    await locked.mark_unusable(status="expired", reason="refresh_rejected", now=now)
                    token_refresh_counter.add(1, {"provider": provider, "outcome": "rejected"})
                    logger.warning(
                        "provider_connections.refresh_rejected provider=%s connection_id=%d "
                        "-- marked expired, admin must reconnect",
                        provider,
                        connection_id,
                    )
                    failure = ConnectionNotUsable(
                        "the provider rejected the refresh token; reconnect required",
                        reason="refresh_rejected",
                    )
                except RefreshNotConfigured as exc:
                    token_refresh_counter.add(
                        1, {"provider": provider, "outcome": "not_configured"}
                    )
                    log_failure(
                        logger,
                        "provider_connections.refresh_not_configured",
                        exc,
                        level=logging.WARNING,
                        provider=provider,
                        connection_id=connection_id,
                    )
                    raise ConnectionNotUsable(exc.message, reason="refresh_not_configured") from exc
                except ProviderUnavailable as exc:
                    token_refresh_counter.add(1, {"provider": provider, "outcome": "unavailable"})
                    log_failure(
                        logger,
                        "provider_connections.refresh_unavailable",
                        exc,
                        level=logging.WARNING,
                        provider=provider,
                        connection_id=connection_id,
                    )
                    raise
                else:
                    aad = self._row_aad(current.connection)
                    sealed_access = self._vault.seal(refreshed.access_token, aad=aad)
                    if refreshed.refresh_token:
                        sealed_refresh: str | None = self._vault.seal(
                            refreshed.refresh_token, aad=aad
                        )
                    elif current.secrets.seal_version == SEAL_LEGACY:
                        # Keeping the old refresh token, but the row is being stamped AAD-bound:
                        # it must be re-sealed too or it would fail authentication next read.
                        sealed_refresh = self._vault.seal(
                            self._open(current.secrets.refresh_token, current), aad=aad
                        )
                    else:
                        sealed_refresh = current.secrets.refresh_token
                    await locked.save_tokens(
                        secrets=SealedSecrets(sealed_access, sealed_refresh, seal_version=SEAL_AAD),
                        expires_at=refreshed.expires_at,
                        now=now,
                    )
                    token_refresh_counter.add(1, {"provider": provider, "outcome": "refreshed"})
                    logger.info(
                        "provider_connections.refreshed provider=%s connection_id=%d",
                        provider,
                        connection_id,
                    )
                    updated = replace(
                        current.connection, expires_at=refreshed.expires_at, updated_at=now
                    )
                    return refreshed.access_token, updated
        if failure is None:  # pragma: no cover - every non-returning path above sets it
            raise ProviderConnectionError("token refresh finished without a result")
        raise failure

    @staticmethod
    def _row_aad(connection: store.ConnectionRow) -> bytes:
        """The AAD binding this row's sealed tokens to it: immutable ``public_id`` + platform."""
        return platform_connection_aad(public_id=connection.public_id, platform=connection.platform)

    def _open(self, ciphertext: str | None, state: ResolveState) -> str:
        """Open one of this row's ciphertexts through the versioned vault."""
        if ciphertext is None:
            raise VaultCorrupt("a stored connection secret is missing")
        return self._vault.open(
            ciphertext,
            aad=self._row_aad(state.connection),
            seal_version=state.secrets.seal_version,
        )

    async def _reseal_if_legacy(self, state: ResolveState) -> None:
        """Lazy M4 upgrade on resolve: re-seal a legacy (v0) row AAD-bound, best effort.

        The row just opened successfully, so a failure here is operational (lock/connection), not
        tamper: it is logged by type only and must never fail the resolve that triggered it.
        ``scripts/reseal_connections.py`` reports anything left behind.
        """
        if state.secrets.seal_version != SEAL_LEGACY:
            return
        try:
            await store.reseal_legacy(
                self._db, connection_id=state.connection.id, rewrap=self._rewrap_legacy
            )
        except Exception as exc:  # noqa: BLE001 - opportunistic upgrade, see docstring
            log_failure(
                logger,
                "provider_connections.lazy_reseal_failed",
                exc,
                level=logging.WARNING,
                connection_id=state.connection.id,
            )

    def _rewrap_legacy(
        self, public_id: UUID, platform: str, current: SealedSecrets
    ) -> SealedSecrets | None:
        """Re-seal a genuinely legacy pair under the row's AAD; ``None`` if it is not legacy.

        A pair that already opens under the row's AAD while the column says legacy is a suspected
        downgrade (the column was flipped): it is logged (ids only) and left alone, never
        re-blessed.
        """
        if current.access_token is None:  # pragma: no cover - the 0064 CHECK forbids it
            return None  # a legacy (v0) row always carries an OAuth token
        aad = platform_connection_aad(public_id=public_id, platform=platform)
        access = reseal_if_legacy(current.access_token, aad=aad)
        refresh = (
            reseal_if_legacy(current.refresh_token, aad=aad) if current.refresh_token else None
        )
        if access is None or (current.refresh_token and refresh is None):
            logger.warning(
                "provider_connections.suspected_seal_downgrade platform=%s public_id=%s "
                "-- ciphertext is AAD-bound but seal_version says legacy; not re-sealed",
                platform,
                public_id,
            )
            return None
        return SealedSecrets(access, refresh, seal_version=SEAL_AAD)

    @staticmethod
    def _assert_usable_connection(state: ResolveState) -> None:
        """Re-check, under the row lock, that the link is still active."""
        if state.connection.status != store.STATUS_ACTIVE:
            raise ConnectionNotUsable(
                "the connection needs to be reconnected",
                reason=state.connection.status_reason or f"connection_{state.connection.status}",
            )

    @staticmethod
    def _compose_facts(
        adapter: ProviderAdapter, scopes: tuple[str, ...], adapter_facts: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Adapter facts + the framework-owned ``scopes`` / ``capabilities`` (JSON-safe)."""
        facts: dict[str, Any] = dict(adapter_facts)
        facts["scopes"] = list(scopes)
        facts["capabilities"] = sorted(adapter.granted_capabilities(scopes))
        return facts

    @staticmethod
    def _metadata_inputs(spec: AdapterSpec, clean: Mapping[str, str]) -> dict[str, str]:
        """Persist the admin's normalised inputs verbatim so a later update can replay them."""
        return {item.name: clean[item.name] for item in spec.metadata_fields if item.name in clean}

    def _view(self, binding: Binding) -> ConnectionView:
        try:
            spec: AdapterSpec | None = self._registry.get(binding.connection.platform).spec
        except UnknownProvider:
            spec = None
            logger.warning(
                "provider_connections.unregistered_provider provider=%s connection_id=%d",
                binding.connection.platform,
                binding.connection.id,
            )
        meta = binding.connection.resource_metadata
        public = {k: v for k, v in meta.items() if k in spec.public_metadata_keys} if spec else {}
        usable = (
            spec is not None
            and binding.grant.status == store.GRANT_APPROVED
            and binding.grant.enabled
            and binding.connection.status == store.STATUS_ACTIVE
        )
        return ConnectionView(
            binding=binding,
            spec=spec,
            public_metadata=public,
            capabilities=_capabilities_of(meta) if spec else [],
            usable=usable,
        )

    @staticmethod
    def _dataplane_item(spec: AdapterSpec, row: Binding) -> DataPlaneBinding:
        meta = row.connection.resource_metadata
        return DataPlaneBinding(
            grant_id=row.grant.id,
            connection_id=row.connection.id,
            community_id=row.grant.community_id,
            tenant_id=row.connection.tenant_id,
            provider=spec.provider,
            resource_type=row.connection.resource_type,
            resource_id=row.connection.resource_id,
            capabilities=_capabilities_of(meta),
            expires_at=row.connection.expires_at,
            updated_at=row.connection.updated_at,
            metadata={k: v for k, v in meta.items() if k in spec.runtime_metadata_keys},
        )


def _capabilities_of(metadata: Mapping[str, Any]) -> list[str]:
    value = metadata.get("capabilities")
    return [str(item) for item in value] if isinstance(value, list) else []


__all__ = [
    "DEFAULT_REFRESH_SKEW",
    "MAX_PAGE_SIZE",
    "ConnectionView",
    "DataPlaneBinding",
    "DataPlanePage",
    "ProviderConnectionError",
    "ProviderConnectionService",
    "ResolvedCredentials",
]
