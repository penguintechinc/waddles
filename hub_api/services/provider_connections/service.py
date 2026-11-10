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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from flask_core.feature_flags import feature_enabled

from services.platform_integrations_crypto import platform_connection_aad
from services.provider_connections import store
from services.provider_connections.adapter import (
    AdapterRegistry,
    AdapterSpec,
    Credentials,
    MetadataDiscoverer,
    ProviderAdapter,
    validate_credentials_shape,
    validate_metadata,
    validate_token_shape,
)
from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    ConnectionNotUsable,
    FeatureNotEnabled,
    InvalidConnectionInput,
    ProviderConnectionError,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    StaticScopeRequired,
    UnknownProvider,
    VaultCorrupt,
)
from services.provider_connections.kinds import ConnectionKind, SecretScheme, UnknownSecretScheme
from services.provider_connections.static_secret import (
    infer_scheme,
    reject_control_characters,
    validate_secret_document,
    wire_secret_fields,
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
#: Reserved ``resource_metadata`` key stamped by the framework (see :meth:`create`).
FACT_TENANT_ID: Final = "tenant_id"
#: Hard ceiling on one data-plane list page.
MAX_PAGE_SIZE: Final = 1000


#: Two-gate feature (PostHog flag default OFF + Professional tier) for the static-secret kinds.
FEATURE_STATIC_CONNECTIONS: Final = "waddles.integrations.static_connections"


async def _default_static_gate(tenant_slug: str) -> bool:
    """The real entitlement check for static-secret connections (flag + tier)."""
    return bool(await feature_enabled(FEATURE_STATIC_CONNECTIONS, tenant=tenant_slug))


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
    kind: int = int(ConnectionKind.PLATFORM_OAUTH)
    #: Credential-rotation counter (never the secret): a change means "restart this connection".
    secret_version: int = 1


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
    kind: int = int(ConnectionKind.PLATFORM_OAUTH)
    access_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Non-secret target facts of a static kind (``url`` / ``region`` / ``function`` / ``project``).
    endpoint: dict[str, str | None] = field(default_factory=dict)
    #: ``SecretScheme`` code + camelCase wire fields of a static secret; ``None`` for OAuth rows.
    secret_scheme: int | None = None
    secret_fields: dict[str, str | None] | None = field(default=None, repr=False)


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
        static_gate: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        """Bind the penguin-dal handle, adapter registry and vault (all injectable for tests).

        ``static_gate(tenant_slug)`` answers whether the tenant may create static-secret
        connections; the default is the real two-gate feature check (flag + Professional tier).
        """
        self._static_gate = static_gate or _default_static_gate
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
        api_credential: Mapping[str, Any] | None = None,
    ) -> ConnectionView:
        """Verify the credential with the provider, resolve the resource, then store it sealed.

        ``api_credential`` is the optional outbound credential document of a provider that has one
        (Wix ``{api_key, site_id}``); it is structurally validated up front and sealed beside the
        token, never echoed back.
        """
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
            api_document = self._validated_api_credential(spec, api_credential)
            owner = await store.community_tenant_id(self._db, community_id)
            if owner != tenant_id:
                # Same answer for "no such community" and "someone else's": no existence oracle,
                # and no provider call is spent on a community the caller cannot touch.
                raise ConnectionNotFound("community not found")

            account = await adapter.validate_credentials(credentials)
            resource = await adapter.resolve_resource(credentials, account, clean_metadata)
            facts = self._compose_facts(adapter, account.scopes, resource.facts)
            facts.update(self._metadata_inputs(spec, clean_metadata))
            # Framework-owned, never in an adapter allow-list (so never served): lets a refresh
            # find the tenant's own OAuth application (per-tenant bot credentials).
            facts[FACT_TENANT_ID] = tenant_id
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
                    sealed_api_credential=(
                        self._vault.seal(api_document, aad=aad) if api_document else None
                    ),
                ),
                expires_at=account.expires_at,
                resource_metadata=facts,
                installed_by_user_id=actor_user_id,
                enabled=enabled,
                now=now,
                connection_kind=int(ConnectionKind.PLATFORM_OAUTH),
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
        tenant_slug: str,
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
        if spec.kind is ConnectionKind.PLATFORM_OAUTH:
            raise InvalidConnectionInput("this provider takes OAuth credentials, not a secret")
        # Defence in depth: the flag lives here, not only in the HTTP layer, so a CLI / internal
        # caller of this method cannot bypass the entitlement.
        if not await self._static_gate(tenant_slug):
            raise FeatureNotEnabled("Static-secret connections are not enabled for this plan")
        with operation_span(provider, "create_static"):
            try:
                scheme = SecretScheme.from_code(secret_scheme)
            except UnknownSecretScheme:
                raise InvalidConnectionInput("unknown secret scheme") from None
            if scheme not in spec.secret_schemes:
                raise InvalidConnectionInput("secret scheme is not supported by this provider")
            document = validate_secret_document(scheme, secret_document)
            reject_control_characters(metadata)
            clean_metadata = validate_metadata(spec, metadata)
            # One source of truth: the adapter's own structural checks run on the exact canonical
            # document that will be sealed (no outbound call -- see static_secret module docs).
            canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
            credentials = adapter.normalize_credentials(Credentials(access_token=canonical))
            account = await adapter.validate_credentials(credentials)
            resource = await adapter.resolve_resource(credentials, account, clean_metadata)
            owner = await store.community_tenant_id(self._db, community_id)
            if owner != tenant_id:
                raise ConnectionNotFound("community not found")
            facts = self._compose_facts(adapter, account.scopes, resource.facts)
            facts["secret_scheme"] = int(scheme)
            public_id = uuid4()
            aad = platform_connection_aad(public_id=public_id, platform=spec.platform)
            sealed = self._vault.seal(canonical, aad=aad)
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
        api_credential: Mapping[str, Any] | None = None,
    ) -> ConnectionView:
        """Toggle ``enabled``, re-target the resource, and/or rotate the credential.

        ``enabled`` is this community's own switch and always allowed. Changing the metadata or
        credential edits the *shared link*, so it is refused while another community uses it.
        """
        if enabled is None and metadata is None and access_token is None and api_credential is None:
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
            metadata is not None or access_token is not None or api_credential is not None
        ):
            raise InvalidConnectionInput(
                "a static-secret connection cannot be edited; delete it and create a new one"
            )
        with operation_span(spec.provider, "update"):
            changes_link = (
                metadata is not None or access_token is not None or api_credential is not None
            )
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
                    api_credential=api_credential,
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
        api_credential: Mapping[str, Any] | None = None,
    ) -> Binding:
        """Re-verify against the provider, then rewrite the (unshared) link in one transaction."""
        spec = adapter.spec
        api_document = self._validated_api_credential(spec, api_credential)
        if api_document and access_token is None and metadata is None:
            # Outbound-credential-only rotation: nothing provider-facing changes (the credential is
            # never verified against the provider), so seal it and bump the rotation counter.
            return await store.update_link(
                self._db,
                tenant_id=tenant_id,
                community_id=community_id,
                connection_id=binding.connection.id,
                values={
                    "sealed_api_credential": self._vault.seal(
                        api_document, aad=self._row_aad(binding.connection)
                    )
                },
                now=self._clock(),
            )
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
        facts[FACT_TENANT_ID] = tenant_id
        values["resource_id"] = resource.resource_id
        values["resource_metadata"] = facts
        if api_document:
            values["sealed_api_credential"] = self._vault.seal(
                api_document, aad=self._row_aad(binding.connection)
            )
        return await store.update_link(
            self._db,
            tenant_id=tenant_id,
            community_id=community_id,
            connection_id=binding.connection.id,
            values=values,
            now=self._clock(),
        )

    async def discover_metadata(
        self, *, tenant_id: int, community_id: int, connection_id: int
    ) -> ConnectionView:
        """Ask the provider for the binding metadata and store it (admin-triggered, bounded).

        Only for adapters implementing :class:`MetadataDiscoverer`. The connection's own token is
        refreshed if stale, the adapter makes its bounded call(s), and the result is stored through
        the same validate-and-verify path as an admin-typed ``metadata`` update -- so a discovered
        value is held to exactly the rules a pasted one is.
        """
        binding = await store.get_binding(
            self._db, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
        if binding is None:
            raise ConnectionNotFound("connection not found")
        adapter = self._registry.get(binding.connection.platform)
        spec = adapter.spec
        if not isinstance(adapter, MetadataDiscoverer) or spec.kind is not (
            ConnectionKind.PLATFORM_OAUTH
        ):
            raise InvalidConnectionInput("this provider does not support metadata discovery")
        if binding.shared_with > 0:
            raise ConnectionConflict(
                "this connection is shared with other communities and cannot be changed here"
            )
        with operation_span(spec.provider, "discover"):
            plaintext, _ = await self._fresh_token(
                adapter, connection_id, community_id=community_id
            )
            found = await adapter.discover_metadata(
                Credentials(access_token=plaintext), binding.connection.resource_metadata
            )
            stored = binding.connection.resource_metadata
            merged: dict[str, Any] = {
                item.name: str(stored[item.name])
                for item in spec.metadata_fields
                if item.name in stored
            }
            merged.update(found)
            updated = await self._update_link(
                adapter,
                binding,
                tenant_id=tenant_id,
                community_id=community_id,
                metadata=merged,
                access_token=None,
                refresh_token=None,
            )
            logger.info(
                "provider_connections.discovered provider=%s connection_id=%d community_id=%d",
                spec.provider,
                connection_id,
                community_id,
            )
            return self._view(updated)

    async def invalidate_app_connections(
        self, *, tenant_id: int, provider: str, client_id: str
    ) -> int:
        """Expire every connection of this tenant minted under OAuth app ``client_id``.

        Called when a tenant admin REPLACES the tenant's OAuth application: reason ``app_replaced``,
        refresh tokens dropped, rows kept so each admin can reconnect. Returns the count.
        """
        spec = self._registry.get(provider).spec
        count = await store.invalidate_for_app(
            self._db,
            tenant_id=tenant_id,
            platform=spec.platform,
            client_id=client_id,
            reason="app_replaced",
            now=self._clock(),
        )
        logger.info(
            "provider_connections.app_connections_invalidated provider=%s tenant_id=%d count=%d",
            provider,
            tenant_id,
            count,
        )
        return count

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
                if row.community_tenant_id != row.binding.connection.tenant_id:
                    # Tenant isolation: the community's tenant is authoritative; a link stored
                    # under another tenant is never served (fail closed, ids only in the log).
                    logger.error(
                        "provider_connections.dataplane_tenant_mismatch connection_id=%d "
                        "community_id=%d",
                        row.binding.connection.id,
                        row.binding.grant.community_id,
                    )
                    continue
                capabilities = _effective_capabilities(adapter, row.binding.connection)
                if capability is not None and capability not in capabilities:
                    continue
                items.append(self._dataplane_item(adapter.spec, row, capabilities))
            dataplane_list_size.record(len(items), {"provider": provider})
            next_after = rows[-1].binding.grant.id if len(rows) == limit else None
            logger.debug(
                "provider_connections.dataplane_listed provider=%s returned=%d scanned=%d",
                provider,
                len(items),
                len(rows),
            )
            return DataPlanePage(items=items, next_after=next_after)

    async def resolve(
        self, *, community_id: int, connection_id: int, static_allowed: bool = False
    ) -> ResolvedCredentials:
        """Hand the data plane a fresh access token for one usable binding (refresh if stale).

        ``static_allowed`` is the caller's ``connections:resolve:static`` entitlement and defaults
        to fail-closed: a static-secret kind is refused with :class:`StaticScopeRequired` (before
        any usability check, so nothing about the row leaks) unless the caller holds it.
        """
        state = await store.read_resolve_state(
            self._db, community_id=community_id, connection_id=connection_id
        )
        if state is None:
            raise ConnectionNotFound("connection not found")
        provider = state.connection.platform
        adapter = self._registry.get(provider)
        kind = ConnectionKind.from_code(state.connection.connection_kind)
        if kind is not ConnectionKind.PLATFORM_OAUTH:
            if not static_allowed:
                raise StaticScopeRequired("static-secret connections need a broader scope")
            # A static row never falls through to the token-opening path (its access_token is
            # NULL by construction): it has its own branch and its own sealed document.
            with operation_span(provider, "resolve"):
                self._assert_usable(state)
                return self._resolve_static(adapter, kind, state)
        with operation_span(provider, "resolve"):
            self._assert_usable(state)
            # Reject an incomplete binding BEFORE any refresh: never rotate a row we will refuse.
            if (
                self.binding_metadata(
                    provider, state.connection.resource_metadata, connection_id=connection_id
                )
                is None
            ):
                raise ConnectionNotUsable(
                    "the connection's binding is incomplete", reason="metadata_incomplete"
                )
            plaintext, connection = await self._fresh_token(
                adapter, connection_id, community_id=community_id, state=state
            )
            allowed = adapter.spec.runtime_metadata_keys
            api_scheme, api_fields = (
                self._resolve_api_credential(adapter, state) if static_allowed else (None, None)
            )
            return ResolvedCredentials(
                connection_id=connection_id,
                community_id=community_id,
                provider=provider,
                kind=int(kind),
                access_token=plaintext,
                expires_at=connection.expires_at,
                metadata={k: v for k, v in connection.resource_metadata.items() if k in allowed},
                secret_scheme=api_scheme,
                secret_fields=api_fields,
            )

    # -- internals -------------------------------------------------------------------------

    def binding_metadata(
        self, provider: str, stored: Mapping[str, Any], *, connection_id: int
    ) -> dict[str, str] | None:
        """Project stored ``resource_metadata`` to the provider's data-plane metadata contract.

        Flat string->string, allow-listed by the adapter's ``binding_metadata`` (aliases applied
        here, storage untouched). ``None`` = a required key is missing, so the row is not a
        complete binding; the log line names the connection id and the missing *keys* only.
        """
        spec = self._registry.get(provider).spec
        out: dict[str, str] = {}
        missing: list[str] = []
        for key in spec.binding_metadata:
            value = stored.get(key.stored)
            if isinstance(value, int) and not isinstance(value, bool):
                value = str(value)  # e.g. a numeric channel id stored as an int
            if isinstance(value, str) and value:
                out[key.wire] = value
            elif key.required:
                missing.append(key.stored)
        if missing:
            logger.warning(
                "provider_connections.binding_incomplete connection_id=%d missing=%s",
                connection_id,
                ",".join(missing),
            )
            return None
        return out

    def _resolve_static(
        self, adapter: ProviderAdapter, kind: ConnectionKind, state: ResolveState
    ) -> ResolvedCredentials:
        """Open a static row's sealed secret document and shape it for the wire (no refresh).

        Every failure is a fixed-text framework error: the document is opened with the row-bound
        AAD, must parse to an object whose key set is exactly one of the provider's schemes, and a
        row whose own ``expires_at`` has passed is refused like an expired OAuth link.
        """
        connection = state.connection
        if connection.expires_at is not None and connection.expires_at <= self._clock():
            raise ConnectionNotUsable("the connection has expired", reason="connection_expired")
        plaintext = self._open(state.secrets.sealed_secret, state)
        try:
            doc = json.loads(plaintext)
        except ValueError:
            raise VaultCorrupt("a stored connection secret is malformed") from None
        scheme = infer_scheme(doc) if isinstance(doc, dict) else None
        if scheme is None or scheme not in adapter.spec.secret_schemes:
            raise VaultCorrupt("a stored connection secret is malformed")
        meta = connection.resource_metadata
        return ResolvedCredentials(
            connection_id=connection.id,
            community_id=state.grant.community_id,
            provider=connection.platform,
            kind=int(kind),
            access_token=None,
            expires_at=connection.expires_at,
            metadata={},
            endpoint={
                key: (str(meta[key]) if meta.get(key) is not None else None)
                for key in ("url", "region", "function", "project")
            },
            secret_scheme=int(scheme),
            secret_fields=wire_secret_fields(scheme, doc),
        )

    @staticmethod
    def _validated_api_credential(spec: AdapterSpec, raw: Mapping[str, Any] | None) -> str | None:
        """Canonical JSON of a validated outbound credential, or ``None`` when none was supplied.

        A provider with no ``api_credential_scheme`` refuses one outright. Errors name only the
        offending field, never its value.
        """
        if raw is None:
            return None
        if spec.api_credential_scheme is None:
            raise InvalidConnectionInput("this provider does not take an API credential")
        reject_control_characters(raw)
        document = validate_secret_document(spec.api_credential_scheme, raw)
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    def _resolve_api_credential(
        self, adapter: ProviderAdapter, state: ResolveState
    ) -> tuple[int | None, dict[str, str | None] | None]:
        """Open a kind-1 row's sealed outbound credential for a caller holding the static scope.

        ``(None, None)`` when the provider has none or none is stored (the public token still
        resolves). A stored document that fails its own scheme is :class:`VaultCorrupt` -- loud,
        fixed-text, never partial.
        """
        scheme = adapter.spec.api_credential_scheme
        sealed = state.secrets.sealed_api_credential
        if scheme is None or sealed is None:
            return None, None
        plaintext = self._vault.open(
            sealed, aad=self._row_aad(state.connection), seal_version=SEAL_AAD
        )
        try:
            doc = json.loads(plaintext)
        except ValueError:
            raise VaultCorrupt("a stored connection secret is malformed") from None
        if not isinstance(doc, dict) or infer_scheme(doc) is not scheme:
            raise VaultCorrupt("a stored connection secret is malformed")
        return int(scheme), wire_secret_fields(scheme, doc)

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
    def _dataplane_item(
        spec: AdapterSpec, row: store.DataPlaneRow, capabilities: list[str]
    ) -> DataPlaneBinding:
        """Shape one usable row; ``tenant_id`` is the community's own tenant (server-derived)."""
        conn = row.binding.connection
        return DataPlaneBinding(
            grant_id=row.binding.grant.id,
            connection_id=conn.id,
            community_id=row.binding.grant.community_id,
            tenant_id=row.community_tenant_id,
            provider=conn.platform,
            resource_type=conn.resource_type,
            resource_id=conn.resource_id,
            capabilities=capabilities,
            expires_at=conn.expires_at,
            updated_at=conn.updated_at,
            metadata={
                k: v for k, v in conn.resource_metadata.items() if k in spec.runtime_metadata_keys
            },
            kind=conn.connection_kind,
            secret_version=conn.secret_version,
        )


def _effective_capabilities(adapter: ProviderAdapter, conn: store.ConnectionRow) -> list[str]:
    """Capabilities a binding grants *now*: the stored set plus what the adapter derives today.

    A row connected before an adapter learned a capability (Discord gained ``chat_ingest``) still
    carries the old stored list; unioning the adapter's current view of the stored scopes serves it
    without a data migration. Sorted for a stable wire order.
    """
    stored = _capabilities_of(conn.resource_metadata)
    scopes = conn.resource_metadata.get("scopes")
    granted = adapter.granted_capabilities(
        [str(x) for x in scopes] if isinstance(scopes, list) else []
    )
    return sorted({*stored, *granted})


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
