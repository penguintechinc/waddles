"""First-run import (security review H1): adopt a value already in use, or refuse it and generate.

Only ever called on a pristine store, and only for a registry type that names an `ImportSource`;
the env var names come from the registry allowlist, never from the caller or the environment, so
nothing attacker-controlled chooses what is read. Every candidate is validated before it can become
version 1: not empty, not a placeholder or chart default, parses as the type's key type with the
exact length, and carries real entropy. A rejected value is NEVER imported (the bootstrap generates
a fresh one instead) and is reported by outcome only -- the value is never logged, labelled or
returned in an error.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from services.platform_secrets.registry import SecretSpec
from services.provider_connections.vault import (
    PlatformKeyMaterial,
    PlatformSecretInputError,
    platform_key_material_from_private,
)

#: Whole-value denylist (lower-cased, stripped): placeholders and the chart / docs defaults.
_DENY_EXACT: Final = frozenset(
    {
        "",
        "replace_me",
        "replace-me",
        "changeme",
        "change-me",
        "change_me",
        "none",
        "null",
        "undefined",
        "todo",
        "xxx",
        "password",
        "secret",
        "default",
        "dummy",
        "test",
        "jwt-secret-change-in-production",
    }
)
#: Substrings that mark a value as a placeholder wherever they appear (lower-cased).
_DENY_SUBSTRINGS: Final = (
    "replace_me",
    "replace-me",
    "changeme",
    "change-me",
    "change_me",
    "change-in-production",
    "placeholder",
    "your-key-here",
    "example",
)
#: A key needs at least this many distinct byte values (a repeated / sequential seed has few).
_MIN_DISTINCT_BYTES: Final = 8
_B64_RE: Final = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_KID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

OUTCOME_OK: Final = "ok"
OUTCOME_ABSENT: Final = "absent"
OUTCOME_PLACEHOLDER: Final = "rejected_placeholder"
logger = logging.getLogger(__name__)

OUTCOME_INVALID: Final = "rejected_invalid"


@dataclass(slots=True, frozen=True)
class ImportDecision:
    """What the importer concluded for one type.

    `outcome` is `ok` (use `material`), `absent` (nothing to import) or `rejected_*` (do not
    import; generate). `source` is the allowlisted env var NAME the value came from (a fixed
    registry string, safe to log and to record), never its value.
    """

    outcome: str
    source: str
    material: PlatformKeyMaterial | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        """True only when a validated value is ready to be sealed as version 1."""
        return self.outcome == OUTCOME_OK and self.material is not None


def _is_placeholder(text: str) -> bool:
    """True for an empty value, a known default, or anything containing a placeholder marker."""
    lowered = text.strip().lower()
    return lowered in _DENY_EXACT or any(marker in lowered for marker in _DENY_SUBSTRINGS)


def _weak_bytes(raw: bytes) -> bool:
    """True for a key with too few distinct bytes or a strictly sequential run (not random)."""
    if len(set(raw)) < _MIN_DISTINCT_BYTES:
        return True
    return all((raw[i + 1] - raw[i]) % 256 == 1 for i in range(len(raw) - 1))


def _decode_b64(text: str) -> bytes | None:
    """Strict base64 (std or urlsafe, padding optional) -> bytes, or None when malformed."""
    stripped = text.strip()
    if not _B64_RE.fullmatch(stripped):
        return None
    padded = stripped.rstrip("=")
    padded += "=" * (-len(padded) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(padded)
        except (binascii.Error, ValueError):
            continue
    return None


def import_candidate(spec: SecretSpec, environ: Mapping[str, str]) -> ImportDecision:
    """Read and validate the one allowlisted env source of `spec`; never raises on a bad value.

    Reads ONLY `spec.import_source.private_env` (and its `kid_env`) from `environ`. A type with no
    import source, or an unset / empty variable, is `absent`. Placeholders and chart defaults are
    `rejected_placeholder`; undecodable, wrong-length, weak or unparsable keys are
    `rejected_invalid`. A supplied key id that is not a plain id is dropped (a fresh one is derived
    from the public key) rather than trusted.
    """
    source = spec.import_source
    if source is None or not spec.is_asymmetric:
        return ImportDecision(OUTCOME_ABSENT, "")
    raw_value = environ.get(source.private_env)
    if raw_value is None or not raw_value.strip():
        return ImportDecision(OUTCOME_ABSENT, source.private_env)
    if _is_placeholder(raw_value):
        return ImportDecision(OUTCOME_PLACEHOLDER, source.private_env)
    decoded = _decode_b64(raw_value)
    if decoded is None or _weak_bytes(decoded):
        return ImportDecision(OUTCOME_INVALID, source.private_env)
    kid: str | None = None
    if source.kid_env is not None:
        supplied = (environ.get(source.kid_env) or "").strip()
        if supplied and not _is_placeholder(supplied) and _KID_RE.fullmatch(supplied):
            kid = supplied
    try:
        material = platform_key_material_from_private(int(spec.algorithm), decoded, kid=kid)
    except PlatformSecretInputError as exc:
        # Refused loudly: the recorded outcome is `rejected_invalid` and the caller generates a
        # fresh key. Log the env var NAME and exception type only -- never the value or its detail.
        logger.warning(
            "platform secret import rejected: key material invalid env=%s error_type=%s",
            source.private_env,
            type(exc).__name__,
        )
        return ImportDecision(OUTCOME_INVALID, source.private_env)
    return ImportDecision(OUTCOME_OK, source.private_env, material)
