"""RFC 6238 TOTP and one-time-code primitives for account MFA (pure: no I/O, no clock reads).

Google Authenticator / any RFC 6238 app: HMAC-SHA1, 6 digits, 30-second period, 160-bit secret
(RFC 4226 section 4 recommends >= 160 bits). Implemented on the standard library's `hmac` rather
than adding a dependency -- the algorithm is ~10 lines and the stdlib primitives are the ones a
library would call anyway. Every comparison here is constant time (`hmac.compare_digest`).

Also holds the recovery-code and email-code generators/hashers so all one-time-secret handling
for MFA lives in one reviewable module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from typing import Final
from urllib.parse import quote, urlencode

DIGITS: Final = 6
PERIOD_SECONDS: Final = 30
#: Accept the previous and next step too (clock drift between phone and server).
DRIFT_STEPS: Final = 1
SECRET_BYTES: Final = 20
ISSUER: Final = "Waddles"

RECOVERY_CODE_COUNT: Final = 10
#: Crockford-style alphabet without look-alikes (0/O, 1/I/L): 10 chars x 5 bits = 50 bits.
_RECOVERY_ALPHABET: Final = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_RECOVERY_LENGTH: Final = 10


def new_secret() -> str:
    """A fresh random TOTP secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    """Decode a base32 secret, tolerating missing padding."""
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    return base64.b32decode(padded, casefold=True)


def code_at_step(secret_b32: str, step: int) -> str:
    """The RFC 4226 HOTP value for counter `step` (TOTP = HOTP over the time step)."""
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**DIGITS)).zfill(DIGITS)


def step_for(epoch_seconds: float) -> int:
    """The TOTP time step containing `epoch_seconds`."""
    return int(epoch_seconds // PERIOD_SECONDS)


def matching_step(secret_b32: str, code: str, *, now: float, after_step: int | None) -> int | None:
    """The step within +/-1 of `now` whose code equals `code`, or None.

    Steps at or before `after_step` (the last one already accepted) are never matched, which is
    what makes a code single-use even inside its own validity window. All candidate steps are
    compared (no early exit) so timing does not reveal which window matched.
    """
    candidate = normalize_digits(code)
    if candidate is None:
        return None
    current = step_for(now)
    found: int | None = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        ok = hmac.compare_digest(code_at_step(secret_b32, step), candidate)
        fresh = after_step is None or step > after_step
        if ok and fresh and found is None:
            found = step
    return found


def normalize_digits(code: str) -> str | None:
    """Strip spaces/dashes; return the code if it is exactly `DIGITS` ASCII digits, else None."""
    cleaned = code.replace(" ", "").replace("-", "")
    if len(cleaned) != DIGITS or not cleaned.isascii() or not cleaned.isdigit():
        return None
    return cleaned


def provisioning_uri(secret_b32: str, *, account_label: str) -> str:
    """The `otpauth://totp/...` URI an authenticator app scans (rendered as a QR code client-side).

    The URI carries the secret, so it is only ever returned to the enrolling user's own browser
    and is never logged.
    """
    label = quote(f"{ISSUER}:{account_label}", safe="")
    params = urlencode(
        {
            "secret": secret_b32,
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{params}"


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """`count` random recovery codes, formatted `XXXXX-XXXXX` for readability."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    """Upper-case and drop separators/whitespace so `abcde-fghjk` == `ABCDEFGHJK`."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    """LEGACY unkeyed SHA-256 of the normalized code -- never used for storage any more.

    Rows written this way (dev only, pre alembic `0146`) are deleted by that migration; kept
    so the normalization contract stays pinned by its test.
    """
    return hashlib.sha256(normalize_recovery_code(code).encode("ascii", "ignore")).hexdigest()


#: Domain-separation label for the recovery-code pepper derived from the envelope data key.
RECOVERY_PEPPER_INFO: Final = b"waddles-mfa-recovery-pepper/v1"


def recovery_pepper(data_key: bytes) -> bytes:
    """Derive the recovery-code pepper from an envelope data subkey (domain separated)."""
    if len(data_key) < 16:
        raise ValueError("recovery pepper needs at least 128 bits of key material")
    return hmac.new(data_key, RECOVERY_PEPPER_INFO, hashlib.sha256).digest()


def hmac_recovery_code(pepper: bytes, code: str) -> str:
    """HMAC-SHA256 hex of the normalized code under the server-side pepper.

    A leaked table alone is not testable offline: without the pepper (held by the envelope /
    KMS service, never in the database) the 50-bit codes cannot be brute forced.
    """
    if len(pepper) < 16:
        raise ValueError("recovery pepper too short")
    message = normalize_recovery_code(code).encode("ascii", "ignore")
    return hmac.new(pepper, message, hashlib.sha256).hexdigest()


def new_email_code() -> str:
    """A uniformly random 6-digit email one-time code."""
    return str(secrets.randbelow(10**DIGITS)).zfill(DIGITS)


def hash_email_code(code: str, salt_hex: str) -> str:
    """Salted SHA-256 hex of a 6-digit code (the 10-minute TTL + 5 attempts bound its value)."""
    return hashlib.sha256(bytes.fromhex(salt_hex) + code.encode("ascii")).hexdigest()


def hash_token(token: str) -> str:
    """SHA-256 hex of an opaque bearer value (MFA-pending token) for at-rest storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digests_equal(left: str, right: str) -> bool:
    """Constant-time equality for two hex digests."""
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
