"""Secret-type registry: which platform secrets exist, how each is generated, scoped and imported.

The registry is code, not data: a secret type is an int enum code (the value stored in
`platform_vault.secrets.secret_type`), never a string reference. Adding a type is a reviewed code
change; the importer can only read the env vars a spec names (an explicit allowlist), so what gets
imported on first run cannot be steered by arbitrary environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from services.provider_connections.vault import (
    PLATFORM_ALG_ED25519,
    PLATFORM_ALG_ES256,
    PLATFORM_ALG_PASSWORD,
    PLATFORM_ALG_RANDOM_256,
)

#: Largest `secret_type` / `scope_id` the table's column types hold (SMALLINT / BIGINT).
_SMALLINT_MAX = 32767
_BIGINT_MAX = 2**63 - 1


class SecretType(IntEnum):
    """Secret type codes -- stored as `secrets.secret_type`; never renumber or reuse a code."""

    BUNDLE_SIGNING = 1
    PRECOMPILE_SIGNING = 2
    JWT_SIGNING = 3


class Algorithm(IntEnum):
    """Algorithm codes -- stored as `secrets.algorithm` (same mapping as the migration)."""

    ED25519 = PLATFORM_ALG_ED25519
    ES256 = PLATFORM_ALG_ES256
    RANDOM_256 = PLATFORM_ALG_RANDOM_256
    PASSWORD = PLATFORM_ALG_PASSWORD


class ScopeRule(IntEnum):
    """What `scope_id` a secret type accepts."""

    GLOBAL = 1  # scope_id must be None
    TENANT = 2  # scope_id is a tenant's numeric id (0 = the default tenant)
    COMMUNITY = 3  # scope_id is a community's numeric id


class UnknownSecretTypeError(LookupError):
    """A secret type code that the registry does not define (fixed message, no value)."""


class ScopeRuleError(ValueError):
    """A scope_id that the type's scope rule forbids (fixed message, no value)."""


@dataclass(slots=True, frozen=True)
class ImportSource:
    """The ONLY env vars the first-run import may read for a type (K8s-Secret-backed in the chart).

    `private_env` holds the base64 private key; `kid_env` optionally holds its key id. Both are
    wired from a Secret by `secretKeyRef` (never a ConfigMap or an env literal); the importer reads
    nothing else, so the allowlist is exactly these names.
    """

    private_env: str
    kid_env: str | None = None


@dataclass(slots=True, frozen=True)
class SecretSpec:
    """One registered secret type.

    `name` is the telemetry label (a fixed enum-derived word, never user input). `verify_window_s`
    is how long a verify-only version is honoured after it was demoted (`rotated_at`): JWT keys =
    token TTL + cache TTL, signing keys = until the re-sign job ends (24h cap) -- the cache refuses
    a verify-only key past it, even when no refresh happens.
    """

    secret_type: SecretType
    name: str
    algorithm: Algorithm
    scope_rule: ScopeRule
    verify_window_s: int
    import_source: ImportSource | None = None

    @property
    def is_asymmetric(self) -> bool:
        """True when the type carries a public key (Ed25519 / ES256)."""
        return self.algorithm in (Algorithm.ED25519, Algorithm.ES256)


#: Every registered type. JWT signing is deliberately import-less: the replacement of the shared
#: HS256 secret must be a fresh ES256 key, never a value that has already been distributed.
REGISTRY: dict[SecretType, SecretSpec] = {
    SecretType.BUNDLE_SIGNING: SecretSpec(
        secret_type=SecretType.BUNDLE_SIGNING,
        name="bundle_signing",
        algorithm=Algorithm.ED25519,
        scope_rule=ScopeRule.GLOBAL,
        verify_window_s=24 * 3600,
        import_source=ImportSource("BUNDLE_SIGNING_PRIVATE_KEY", "BUNDLE_SIGNING_KEY_ID"),
    ),
    SecretType.PRECOMPILE_SIGNING: SecretSpec(
        secret_type=SecretType.PRECOMPILE_SIGNING,
        name="precompile_signing",
        algorithm=Algorithm.ED25519,
        scope_rule=ScopeRule.GLOBAL,
        verify_window_s=24 * 3600,
    ),
    SecretType.JWT_SIGNING: SecretSpec(
        secret_type=SecretType.JWT_SIGNING,
        name="jwt_signing",
        algorithm=Algorithm.ES256,
        scope_rule=ScopeRule.GLOBAL,
        verify_window_s=2 * 3600,
    ),
}


def get_spec(secret_type: int) -> SecretSpec:
    """Return the spec for a stored type code, or raise :class:`UnknownSecretTypeError`."""
    try:
        return REGISTRY[SecretType(secret_type)]
    except (ValueError, KeyError):
        raise UnknownSecretTypeError("unknown platform secret type") from None


def validate_scope(spec: SecretSpec, scope_id: int | None) -> None:
    """Enforce the type's scope rule; raises :class:`ScopeRuleError` with a fixed message."""
    if spec.scope_rule is ScopeRule.GLOBAL:
        if scope_id is not None:
            raise ScopeRuleError("this secret type is global and takes no scope_id")
        return
    if (
        isinstance(scope_id, bool)
        or not isinstance(scope_id, int)
        or not 0 <= scope_id <= _BIGINT_MAX
    ):
        raise ScopeRuleError("this secret type requires a non-negative integer scope_id")


def check_registry() -> None:
    """Self-check run by tests: codes unique and in range, names unique, asymmetric-only imports."""
    names = [s.name for s in REGISTRY.values()]
    if len(set(names)) != len(names):
        raise AssertionError("duplicate registry names")
    for secret_type, spec in REGISTRY.items():
        if spec.secret_type is not secret_type or not 1 <= int(secret_type) <= _SMALLINT_MAX:
            raise AssertionError("registry key and code disagree")
        if spec.import_source is not None and not spec.is_asymmetric:
            raise AssertionError("only asymmetric types can be imported")
