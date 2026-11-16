"""
Fail-Closed Secret Loading
===========================

Security audit finding C1 (A02:2021 Cryptographic Failures / CWE-798
Hardcoded Credentials): ~47 services across this monorepo read
``SECRET_KEY`` (then the symmetric key that signed and verified every JWT; platform JWTs are
ES256 now and ``SECRET_KEY`` only keys sessions / refresh-token MACs) via
``os.getenv("SECRET_KEY", "change-me-in-production")``. Any deploy that
forgets to set ``SECRET_KEY`` silently boots with a public, hardcoded
signing key -- anyone can forge a token, including a ``global:admin``
scope bundle or an arbitrary ``tenant`` claim (security.md Tenant
Isolation is a hard boundary only if the signature actually proves
authenticity).

:func:`require_secret_key` is the one place this is fixed: unset or
still-a-known-placeholder in a production-like environment refuses to
return a value at all -- the caller (almost always a module-level
``Config`` class body, evaluated at process import) propagates the
raised error and the service never finishes starting, rather than
serving traffic signed with a public key.

Two things keep this from also fail-closing every test suite and the
local dev stack that already assert on the literal placeholder string:

- A live ``pytest`` process (detected via ``sys.modules``, the standard
  no-conftest-changes-required technique) is never treated as
  production, regardless of ``RELEASE_MODE``/``ENVIRONMENT`` -- ~50
  test suites across the repo keep working unmodified. A test that
  wants to exercise the fail-closed path itself does so explicitly via
  the ``require`` parameter (see :func:`require_secret_key`), the same
  override shape ``workload_identity.IdentityProvider.connect(require=...)``
  already uses for its own fail-safe posture check.
- Outside pytest, production posture is resolved via
  ``workload_identity.is_production()`` -- the same "House fail-safe
  posture check" every other identity/security gate in this library
  uses, so a misconfigured deploy (no ``RELEASE_MODE``/``ENVIRONMENT``
  set at all) still fails closed by default rather than assuming dev.
"""

from __future__ import annotations

import os
import sys

from .workload_identity import is_production

#: Every literal placeholder value that has shipped as a `SECRET_KEY` /
#: `MODULE_SECRET_KEY` / legacy JWT-secret fallback somewhere
#: in this repo (grep audit, C1) -- centralized here so a new module can't
#: reintroduce yet another spelling of "insecure default" that this check
#: doesn't know about.
KNOWN_PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me-in-production",
        "jwt-secret-key-change-in-prod",
        "jwt-secret-change-in-production",
        "development-secret-key",
        "your-secret-key-change-in-production",
        "",
    }
)

#: The historical fallback value -- kept as the function's own default so
#: every existing `os.getenv("SECRET_KEY", "change-me-in-production")` call
#: site becomes `require_secret_key()` with identical dev/test behavior.
_DEFAULT_PLACEHOLDER = "change-me-in-production"


class InsecureSecretError(RuntimeError):
    """Raised when a required secret is unset or a known placeholder in production.

    Raised during module import (a `Config` class body) or app-factory
    startup, so the process exits before binding a port -- a crash-looping
    container is the intended fail-closed behavior, not a 200 OK serving
    traffic signed with a publicly known key.
    """


def _running_under_pytest() -> bool:
    """True in any pytest worker process, without requiring a conftest change.

    Deliberately narrower than `workload_identity.is_production()`'s own
    dev-environment allowlist -- this only ever widens what counts as
    "not production" for test collection, never for a real deploy.
    """
    return "pytest" in sys.modules


def require_secret_key(
    env_var: str = "SECRET_KEY",
    *,
    default: str = _DEFAULT_PLACEHOLDER,
    require: bool | None = None,
) -> str:
    """
    Read ``env_var``, failing closed if it is unset/placeholder in production.

    Args:
        env_var: Name of the environment variable to read (``SECRET_KEY``
            for the shared flask_core JWT signing key; some modules use
            their own name, e.g. ``MODULE_SECRET_KEY``).
        default: Value returned when ``env_var`` is unset outside
            production -- kept as the historical placeholder so dev/test
            behavior is unchanged by this function existing.
        require: Forces production posture on/off. ``None`` (the default)
            resolves it from the environment: a live pytest process is
            never production; otherwise ``workload_identity.is_production()``
            decides. Tests exercising the fail-closed path pass
            ``require=True`` explicitly rather than relying on
            environment-variable gymnastics.

    Returns:
        The environment value (or ``default`` outside production).

    Raises:
        InsecureSecretError: ``require`` (or its resolution) is ``True``
            and the value is empty or one of :data:`KNOWN_PLACEHOLDER_SECRETS`.
    """
    value = os.getenv(env_var, default)
    prod = require if require is not None else (not _running_under_pytest() and is_production())
    if prod and value in KNOWN_PLACEHOLDER_SECRETS:
        raise InsecureSecretError(
            f"{env_var} is unset or still an insecure default value in a "
            f"production-like environment -- refusing to start. Set {env_var} "
            "to a unique, randomly generated secret (security.md Secrets & "
            "Credentials; A02:2021 / CWE-798)."
        )
    return value
