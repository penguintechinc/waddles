"""JWT verification hardening shared by every flask_core verifier (RFC 8725).

Phase 0 of the H-2 asymmetric-JWT migration plus the MED-5 permissive-claims
finding (2026-10-09 security audit). Nothing here changes what a *valid*
platform token looks like -- it closes the ways an *invalid* one could slip
through, and makes algorithm usage observable ahead of the ES256 cutover:

* **One algorithm per verifier** (RFC 8725 section 3.1). The JOSE header ``alg`` is
  checked against the verifier's own allow-list *before* any cryptography
  runs, so an attacker-chosen ``alg`` (``none``, an HMAC variant presented to
  an asymmetric verifier, an RSA/EC algorithm presented to the HS256 verifier)
  is rejected on its own named reason instead of relying on the JWT library's
  behaviour alone.
* **``alg: none`` is a hard reject**, in any letter case.
* **Key-material header parameters are refused** -- ``jku``, ``jwk``, ``x5u``,
  ``x5c`` (RFC 7515 section 4.1) tell a naive verifier where to *fetch or find* the
  verification key, i.e. they let the token choose its own trust anchor.
  Nothing in this platform ever mints them, so their presence is hostile
  by construction. ``crit`` is refused as well: no critical extension is
  understood, and RFC 7515 section 4.1.11 says to reject what you do not understand.
* **``kid`` hygiene.** A ``kid`` will select a key in the JWKS phase; its
  charset and length are pinned now so a hostile value can never reach a
  key-lookup path, a log line or a metric label.
* **A per-algorithm verification metric** (``waddles_jwt_verifications_total``
  and ``waddles_jwt_verification_seconds``) so algorithm usage and the
  rejection mix are visible before any minting change.

Logging is PII-free by construction: only members of the closed vocabularies
below (``REASON_*``, ``VERIFIER_*``, algorithm labels) are ever rendered. The
token, its claims and the JWT library's exception text are never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

import jwt
from jwt import exceptions as jwt_exceptions
from opentelemetry import metrics

logger = logging.getLogger(__name__)

#: Verifier identities -- the ``verifier`` metric label (closed set).
VERIFIER_PLATFORM_HS256: Final = "platform_hs256"
VERIFIER_SERVICE_EDDSA: Final = "service_eddsa"
VERIFIER_OIDC_ID_TOKEN: Final = "oidc_id_token"  # noqa: S105 - metric label, not a secret

#: Verification outcome / rejection reasons -- the ``outcome`` metric label and the only
#: free-form-looking text ever logged about a rejection (closed set).
OUTCOME_OK: Final = "ok"
REASON_MALFORMED: Final = "malformed"
REASON_ALG_NONE: Final = "alg_none"
REASON_ALG_MISMATCH: Final = "alg_mismatch"
REASON_FORBIDDEN_HEADER: Final = "forbidden_header"
REASON_BAD_KID: Final = "bad_kid"
REASON_UNKNOWN_KID: Final = "unknown_kid"
REASON_NO_KEY: Final = "no_key"
REASON_BAD_SIGNATURE: Final = "bad_signature"
REASON_EXPIRED: Final = "expired"
REASON_IMMATURE: Final = "immature"
REASON_BAD_ISSUER: Final = "bad_issuer"
REASON_BAD_AUDIENCE: Final = "bad_audience"
REASON_MISSING_CLAIM: Final = "missing_claim"
REASON_INVALID_CLAIM: Final = "invalid_claim"
REASON_SCOPE_DENIED: Final = "scope_denied"
REASON_INVALID: Final = "invalid"

#: JOSE header parameters that let a token pick its own verification key (see module doc).
FORBIDDEN_HEADER_PARAMS: Final[frozenset[str]] = frozenset({"jku", "jwk", "x5u", "x5c", "crit"})

#: ``kid`` charset/length. Covers the platform ``hs256-v1`` style and the Helm
#: ``keyId`` (POSIX env-var-safe) used for service keys.
_KID_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-]{0,63}")

#: A real JOSE header is ~50 bytes; anything this large is hostile or broken (a pasted
#: ``x5c`` chain, a decompression-style payload) and is refused before JSON parsing.
_MAX_HEADER_SEGMENT_CHARS: Final = 16 * 1024

#: Algorithms that may appear as a metric label verbatim; anything else collapses to
#: ``other`` so an attacker-chosen ``alg`` can never mint unbounded label values.
_KNOWN_ALGS: Final[frozenset[str]] = frozenset(
    {
        "hs256", "hs384", "hs512",
        "rs256", "rs384", "rs512",
        "ps256", "ps384", "ps512",
        "es256", "es384", "es512", "es256k",
        "eddsa",
    }
)  # fmt: skip
ALG_LABEL_NONE: Final = "none"
ALG_LABEL_ABSENT: Final = "absent"
ALG_LABEL_OTHER: Final = "other"
_ALG_LABELS: Final[frozenset[str]] = _KNOWN_ALGS | {
    ALG_LABEL_NONE,
    ALG_LABEL_ABSENT,
    ALG_LABEL_OTHER,
}

#: Rejections that mean "somebody is probing" or "something is mis-issuing" (the
#: signature already verified for the claim-level ones) -> ERROR; the rest is the
#: ordinary noise of expired / garbage tokens -> WARNING.
_ALARMING_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_ALG_NONE,
        REASON_ALG_MISMATCH,
        REASON_FORBIDDEN_HEADER,
        REASON_BAD_KID,
        REASON_BAD_SIGNATURE,
        REASON_BAD_ISSUER,
        REASON_BAD_AUDIENCE,
        REASON_MISSING_CLAIM,
        REASON_INVALID_CLAIM,
    }
)


class JwtRejection(Exception):
    """A token failed verification; ``reason`` is a ``REASON_*`` constant, never token data.

    The exception text *is* the reason, so even a handler that stringifies it
    cannot leak claims, header values or key material.
    """

    def __init__(self, reason: str, *, alg: str | None = None) -> None:
        """Carry the closed-vocabulary ``reason`` and the (label-safe) header ``alg``."""
        super().__init__(reason)
        self.reason = reason
        self.alg = alg_label(alg)


@dataclass(slots=True, frozen=True)
class JoseHeader:
    """The vetted subset of a JOSE header a verifier is allowed to act on."""

    alg: str
    kid: str | None


def alg_label(alg: object) -> str:
    """Collapse an arbitrary header ``alg`` to a bounded, log- and metric-safe label."""
    if alg is None:
        return ALG_LABEL_ABSENT
    if not isinstance(alg, str):
        return ALG_LABEL_OTHER
    lowered = alg.lower()
    return lowered if lowered in _ALG_LABELS else ALG_LABEL_OTHER


def is_valid_kid(kid: object) -> bool:
    """Return True if ``kid`` is a string within the pinned charset and length."""
    return isinstance(kid, str) and _KID_RE.fullmatch(kid) is not None


def _decode_header(token: object) -> Mapping[str, Any]:
    """Decode the first compact-JWS segment ourselves, as a JSON object, or raise MALFORMED.

    Deliberately NOT ``jwt.get_unverified_header``: PyJWT raises its own ``DecodeError`` for
    some of the very headers we want to name precisely (``crit``, a non-string ``kid``), which
    would collapse a hostile-header attack into an anonymous "malformed" in the metric.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        raise JwtRejection(REASON_MALFORMED)
    segment = token.split(".", 1)[0]
    if not segment or len(segment) > _MAX_HEADER_SEGMENT_CHARS:
        raise JwtRejection(REASON_MALFORMED)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, TypeError, RecursionError) as exc:  # b64/UTF-8/JSON errors, deep nesting
        raise JwtRejection(REASON_MALFORMED) from exc
    if not isinstance(decoded, dict):
        raise JwtRejection(REASON_MALFORMED)
    return decoded


def inspect_header(
    token: object,
    *,
    allowed_algs: Collection[str],
    validate_kid: bool = False,
    require_kid: bool = False,
) -> JoseHeader:
    """Vet a token's unverified JOSE header and return the safe subset.

    Order matters: ``alg: none`` is named first, then key-material parameters,
    then the algorithm allow-list, then ``kid`` -- the first failure wins so the
    metric names the most dangerous property of the token. Raises ``JwtRejection``;
    never returns for a header that a verifier should not act on.

    Args:
        token: The compact-serialised JWT (anything non-``str`` is malformed).
        allowed_algs: The verifier's algorithm allow-list (one entry for a
            platform verifier). The header ``alg`` must match case-sensitively.
        validate_kid: Reject a present ``kid`` outside the pinned charset.
        require_kid: Reject a token with no ``kid`` at all.
    """
    header = _decode_header(token)

    raw_alg = header.get("alg")
    label = alg_label(raw_alg)
    if isinstance(raw_alg, str) and raw_alg.lower() == "none":
        raise JwtRejection(REASON_ALG_NONE, alg=ALG_LABEL_NONE)
    if FORBIDDEN_HEADER_PARAMS.intersection(header):
        raise JwtRejection(REASON_FORBIDDEN_HEADER, alg=label)
    if not isinstance(raw_alg, str) or raw_alg not in allowed_algs:
        raise JwtRejection(REASON_ALG_MISMATCH, alg=label)

    kid = header.get("kid")
    if kid is None:
        if require_kid:
            raise JwtRejection(REASON_BAD_KID, alg=label)
    elif (validate_kid or require_kid) and not is_valid_kid(kid):
        raise JwtRejection(REASON_BAD_KID, alg=label)
    return JoseHeader(alg=raw_alg, kid=kid if isinstance(kid, str) else None)


def classify_decode_error(exc: jwt.PyJWTError) -> str:
    """Map a PyJWT failure to a closed ``REASON_*`` (most specific class first)."""
    if isinstance(exc, jwt.ExpiredSignatureError):
        return REASON_EXPIRED
    if isinstance(exc, jwt.ImmatureSignatureError):
        return REASON_IMMATURE
    if isinstance(exc, jwt.InvalidSignatureError):
        return REASON_BAD_SIGNATURE
    if isinstance(exc, jwt.InvalidIssuerError):
        return REASON_BAD_ISSUER
    if isinstance(exc, jwt.InvalidAudienceError):
        return REASON_BAD_AUDIENCE
    if isinstance(exc, jwt.MissingRequiredClaimError):
        return REASON_MISSING_CLAIM
    if isinstance(
        exc,
        (
            jwt_exceptions.InvalidIssuedAtError,
            jwt_exceptions.InvalidSubjectError,
            jwt_exceptions.InvalidJTIError,
        ),
    ):
        return REASON_INVALID_CLAIM
    if isinstance(exc, jwt.InvalidAlgorithmError):
        return REASON_ALG_MISMATCH
    if isinstance(exc, jwt.DecodeError):
        return REASON_MALFORMED
    return REASON_INVALID


@dataclass(slots=True)
class _Instruments:
    """The verification counter + latency histogram, bound to one meter provider."""

    verifications: Any
    duration: Any


def _build_instruments(meter_provider: Any = None) -> _Instruments:
    meter = metrics.get_meter("waddles.flask_core.jwt", meter_provider=meter_provider)
    return _Instruments(
        verifications=meter.create_counter(
            "waddles_jwt_verifications_total",
            unit="{verification}",
            description=(
                "JWT verifications by verifier, header algorithm label and outcome. Watch "
                "alg=hs256 drain and alg=es256 rise across the asymmetric cutover; any "
                "alg=none/other or outcome=alg_*/forbidden_header is a probe."
            ),
        ),
        duration=meter.create_histogram(
            "waddles_jwt_verification_seconds",
            unit="s",
            description="Wall time verifying one JWT, by verifier and header algorithm label.",
        ),
    )


_instruments: _Instruments = _build_instruments()


def use_meter_provider(meter_provider: Any = None) -> None:
    """Rebind the verification instruments to ``meter_provider`` (tests, embedding apps)."""
    global _instruments
    _instruments = _build_instruments(meter_provider)


def record_verification(*, verifier: str, alg: str | None, outcome: str, started: float) -> None:
    """Emit the per-algorithm verification counter and latency histogram.

    ``started`` is a ``time.perf_counter()`` reading taken before verification.
    A telemetry failure is logged and swallowed deliberately: a dead exporter
    must never turn into an authentication failure (or success).
    """
    label = alg_label(alg)
    try:
        _instruments.verifications.add(1, {"verifier": verifier, "alg": label, "outcome": outcome})
        _instruments.duration.record(
            max(time.perf_counter() - started, 0.0), {"verifier": verifier, "alg": label}
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never affect the auth verdict
        logger.warning("JWT verification metric emit failed (%s)", type(exc).__name__)


def log_rejection(*, verifier: str, reason: str, alg: str | None) -> None:
    """Log one rejected token: closed-vocabulary fields only, never token data."""
    label = alg_label(alg)
    if reason == REASON_NO_KEY:
        level = logging.CRITICAL
    elif reason in _ALARMING_REASONS:
        level = logging.ERROR
    else:
        level = logging.WARNING
    logger.log(
        level,
        "JWT rejected: verifier=%s reason=%s alg=%s",
        verifier,
        reason,
        label,
        extra={
            "event_type": "AUTH",
            "action": "verify_jwt",
            "result": "FAILURE",
            "verifier": verifier,
            "reason": reason,
            "alg": label,
        },
    )
