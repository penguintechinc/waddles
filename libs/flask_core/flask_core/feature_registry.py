"""
Feature registry
==================

Module-singleton index of validated
:class:`~flask_core.feature_contract.FeatureContract` instances -- the
Feature-side counterpart to :mod:`flask_core.app_registry`. Same shape,
deliberately: ``register`` (duplicate id = error) / ``get`` / ``for_module``
/ ``all_contracts``, one process-wide singleton plus a class for test
isolation.

:func:`entitled_features` is the one addition beyond the App registry's
shape: it is what a per-tenant MCP tool listing calls (design doc
``Interaction surfaces`` -- "Tools derive from Feature contracts" / "The
tool list is per tenant") to get exactly the Feature contracts a tenant may
see -- never more, per that section's warning that listing an ungated tool
leaks the product surface. The entitlement check is injectable via the
``check`` parameter, same injectable-adapter shape as
``EntitlementClient.flag_gate``/``license_gate``, so no live PostHog/
license-server connection is needed to exercise it in tests.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from .feature_contract import FeatureContract

FeatureCheck = Callable[..., Awaitable[bool]]


class FeatureRegistryError(Exception):
    """
    Raised by :class:`FeatureRegistry` for a bad *registration*, as distinct
    from :class:`~flask_core.feature_contract.FeatureContractError`'s bad
    *contract shape*. ``reason`` is a stable machine-checkable code.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


REASON_DUPLICATE_ID = "duplicate_id"
REASON_DUPLICATE_FLAG = "duplicate_flag"
REASON_NOT_FOUND = "not_found"


class FeatureRegistry:
    """
    Loads and indexes Feature contracts. One instance per process in normal
    operation (see :func:`get_registry`); tests construct additional
    instances freely for isolation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, FeatureContract] = {}
        self._by_flag: Dict[str, FeatureContract] = {}
        self._by_module: Dict[str, List[str]] = {}

    def register(self, contract: FeatureContract) -> FeatureContract:
        """Index an already-validated contract. Raises :class:`FeatureRegistryError` on conflict."""
        with self._lock:
            if contract.id in self._by_id:
                raise FeatureRegistryError(
                    REASON_DUPLICATE_ID, f"feature id {contract.id!r} already registered"
                )
            if contract.flag in self._by_flag:
                # Two contracts sharing one flag key would make the flag's
                # `min_tier` (what `EntitlementClient.required_tier` enforces)
                # ambiguous -- reject rather than let either silently win.
                raise FeatureRegistryError(
                    REASON_DUPLICATE_FLAG,
                    f"flag {contract.flag!r} already registered by feature "
                    f"{self._by_flag[contract.flag].id!r}",
                )
            self._by_id[contract.id] = contract
            self._by_flag[contract.flag] = contract
            self._by_module.setdefault(contract.module, []).append(contract.id)
            return contract

    def get(self, feature_id: str) -> FeatureContract:
        """Look up a registered Feature by id. Raises :class:`FeatureRegistryError` if unknown."""
        try:
            return self._by_id[feature_id]
        except KeyError:
            raise FeatureRegistryError(
                REASON_NOT_FOUND, f"no Feature registered with id {feature_id!r}"
            ) from None

    def by_flag(self, flag: str) -> Optional[FeatureContract]:
        """Look up a registered Feature by its PostHog ``flag`` key; ``None`` if unregistered.

        This is the seam ``EntitlementClient.required_tier`` uses to turn a
        contract's ``min_tier`` into runtime enforcement -- entitlement is
        keyed on ``flag``, not ``id``. Returns ``None`` (never raises) because
        most flag keys evaluated at runtime are not Feature contracts at all.
        """
        return self._by_flag.get(flag)

    def for_module(self, module: str) -> Tuple[FeatureContract, ...]:
        """All Features registered against ``module``, registration order."""
        return tuple(self._by_id[fid] for fid in self._by_module.get(module, []))

    def all_contracts(self) -> Tuple[FeatureContract, ...]:
        """Every registered Feature contract, registration order."""
        return tuple(self._by_id.values())

    def clear(self) -> None:
        """Drop all registrations. Test-only -- production registries are load-once at startup."""
        with self._lock:
            self._by_id.clear()
            self._by_flag.clear()
            self._by_module.clear()


_registry = FeatureRegistry()


def get_registry() -> FeatureRegistry:
    """The process-wide singleton registry."""
    return _registry


def register(contract: FeatureContract) -> FeatureContract:
    """Register an already-validated contract against the singleton registry."""
    return _registry.register(contract)


def get(feature_id: str) -> FeatureContract:
    """Look up a registered Feature by id in the singleton registry."""
    return _registry.get(feature_id)


def for_module(module: str) -> Tuple[FeatureContract, ...]:
    """All Features registered against ``module`` in the singleton registry."""
    return _registry.for_module(module)


def all_contracts() -> Tuple[FeatureContract, ...]:
    """Every Feature contract registered in the singleton registry.

    Named ``all_contracts`` rather than ``all`` at module scope so this
    function never shadows the ``all()`` builtin for anything that does
    ``from flask_core.feature_registry import *`` or reads this module
    alongside stdlib usage; :meth:`FeatureRegistry.all_contracts` carries
    the same name for symmetry.
    """
    return _registry.all_contracts()


async def entitled_features(
    *,
    tenant: str,
    community: Optional[int] = None,
    contracts: Optional[Tuple[FeatureContract, ...]] = None,
    check: Optional[FeatureCheck] = None,
) -> List[FeatureContract]:
    """
    Return every Feature contract entitled for ``tenant`` (optionally
    narrowed to ``community``) -- the source list for a per-tenant MCP tool
    listing.

    ``contracts`` defaults to the singleton registry's full set; pass an
    explicit tuple to scope to one Module's contracts (e.g.
    ``entitled_features(tenant=t, contracts=for_module("bot"))``).
    ``check`` defaults to :func:`flask_core.feature_flags.feature_enabled`;
    tests inject a fake so no live PostHog/license-server connection is
    needed. Each contract's flag is evaluated concurrently
    (``asyncio.gather``) since every gate is an independent network
    round-trip.
    """
    if check is None:
        # Local import: avoids pulling entitlement.py's posthog/penguin_licensing
        # dependency chain into every caller of this module, most of which
        # never call entitled_features with the real gate.
        from .feature_flags import feature_enabled

        check = feature_enabled

    pool = contracts if contracts is not None else _registry.all_contracts()
    if not pool:
        return []

    results = await asyncio.gather(
        *(check(c.flag, tenant=tenant, community=community, default=False) for c in pool)
    )
    return [contract for contract, enabled in zip(pool, results) if enabled]
