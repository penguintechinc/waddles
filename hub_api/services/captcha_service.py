"""Server-side captcha verification for the PUBLIC DSAR form (fails CLOSED).

Providers (env, never hardcoded, never logged):

| `DSAR_CAPTCHA_PROVIDER` | Needs                                          | External call |
|-------------------------|------------------------------------------------|---------------|
| `turnstile`             | `DSAR_CAPTCHA_SITE_KEY`, `DSAR_CAPTCHA_SECRET` | Cloudflare    |
| `recaptcha`             | `DSAR_CAPTCHA_SITE_KEY`, `DSAR_CAPTCHA_SECRET` | Google        |
| `altcha`                | `DSAR_ALTCHA_HMAC_KEY` (>= 32 chars)           | none (local)  |

Fail-closed contract: unset provider defaults to `altcha`; a configured provider missing its key,
or an unknown provider -> logged ERROR + `CaptchaUnavailable` (route returns 503); an outbound
verify that times out, errors, returns non-200 or non-JSON -> `CaptchaUnavailable`; only an explicit
provider `success: true` (altcha: a valid signature + solved proof-of-work + unexpired + unused
challenge) passes. `DSAR_CAPTCHA_TIMEOUT_SECONDS` (default 5) bounds every outbound call.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from services import dsar_telemetry as telemetry
from services.safe_parse import env_float

logger = logging.getLogger(__name__)

PROVIDER_TURNSTILE = "turnstile"
PROVIDER_RECAPTCHA = "recaptcha"
PROVIDER_ALTCHA = "altcha"
PROVIDERS = (PROVIDER_TURNSTILE, PROVIDER_RECAPTCHA, PROVIDER_ALTCHA)
DEFAULT_PROVIDER = PROVIDER_ALTCHA

_VERIFY_URLS = {
    PROVIDER_TURNSTILE: "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    PROVIDER_RECAPTCHA: "https://www.google.com/recaptcha/api/siteverify",
}
_MAX_TOKEN_CHARS = 4096
_ALTCHA_MAX_NUMBER = 50_000
_ALTCHA_TTL_SECONDS = 600
_ALTCHA_KEY_MIN = 32
_ALTCHA_REPLAY_SLACK = 60
_ALTCHA_SALT_RE = re.compile(r"^[0-9a-f]{24}\?expires=([0-9]{1,12})&$")
DEFAULT_ACTION = "dsar_request"
ALTCHA_CHALLENGE_PATH = "/api/v1/public/privacy-requests/altcha-challenge"

#: Test seam: an `httpx` transport (e.g. `MockTransport`) swapped in for the real socket.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

ReplayGuard = Callable[[str, int], Awaitable[bool]]


class CaptchaUnavailable(Exception):
    """Captcha cannot be evaluated (unconfigured / provider outage) -> fail closed (503)."""


class CaptchaRejected(Exception):
    """The captcha token is missing, malformed, wrong or replayed -> 400."""


def provider() -> str | None:
    """The effective provider name, or `None` when it is configured but unusable (logged LOUDLY).

    An unset `DSAR_CAPTCHA_PROVIDER` defaults to self-hosted `altcha` (its HMAC key is an internal,
    chart-generated secret). Third-party providers are opt-in and need operator-supplied keys. A
    configured provider with a missing/short key or an unknown name is never silently bypassed: it
    logs an ERROR naming the missing setting (never a value) and the caller answers 503.
    """
    name = os.environ.get("DSAR_CAPTCHA_PROVIDER", "").strip().lower() or DEFAULT_PROVIDER
    if name in (PROVIDER_TURNSTILE, PROVIDER_RECAPTCHA):
        missing = [
            k for k in ("DSAR_CAPTCHA_SECRET", "DSAR_CAPTCHA_SITE_KEY") if not os.environ.get(k)
        ]
        if not missing:
            return name
        logger.error(
            "dsar.captcha_misconfigured",
            extra={"provider": name, "missing": ",".join(missing)},
        )
        return None
    if name == PROVIDER_ALTCHA:
        if len(os.environ.get("DSAR_ALTCHA_HMAC_KEY", "")) >= _ALTCHA_KEY_MIN:
            return name
        logger.error(
            "dsar.captcha_misconfigured",
            extra={"provider": name, "missing": "DSAR_ALTCHA_HMAC_KEY(>=32 chars)"},
        )
        return None
    logger.error("dsar.captcha_unknown_provider", extra={"provider": name[:32]})
    return None


def public_config() -> dict[str, str | None]:
    """What the browser may know: provider, public site key, altcha URL (raises if off)."""
    name = provider()
    if name is None:
        raise CaptchaUnavailable(
            "captcha provider configured but its key/secret is missing or invalid"
        )
    if name == PROVIDER_ALTCHA:
        return {"provider": name, "siteKey": None, "challengeUrl": ALTCHA_CHALLENGE_PATH}
    return {"provider": name, "siteKey": os.environ["DSAR_CAPTCHA_SITE_KEY"], "challengeUrl": None}


def _timeout() -> float:
    return max(0.5, min(30.0, env_float("DSAR_CAPTCHA_TIMEOUT_SECONDS", 5.0)))


def _altcha_key() -> bytes:
    return os.environ["DSAR_ALTCHA_HMAC_KEY"].encode("utf-8")


def _altcha_sign(salt: str, challenge: str) -> str:
    """Keyed BLAKE2b MAC over the salt (incl. its expiry) AND the challenge.

    Server-issued and server-verified; the widget treats the signature as opaque, so this need not
    be HMAC (and it avoids a new `hmac` importer, see `tests/test_seal_callers_guard.py`). Length
    prefixing makes the (salt, challenge) encoding unambiguous.
    """
    message = f"{len(salt)}:{salt}|{challenge}".encode()
    return hashlib.blake2b(
        message, key=_altcha_key()[:64], digest_size=32, person=b"dsar-altcha-v2"
    ).hexdigest()


def create_altcha_challenge() -> dict[str, Any]:
    """Issue a signed altcha proof-of-work challenge (self-hosted; no external call)."""
    if provider() != PROVIDER_ALTCHA:
        raise CaptchaUnavailable("altcha is not the configured provider")
    number = secrets.randbelow(_ALTCHA_MAX_NUMBER) + 1
    salt = f"{secrets.token_hex(12)}?expires={int(time.time()) + _ALTCHA_TTL_SECONDS}&"
    challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    signature = _altcha_sign(salt, challenge)
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "salt": salt,
        "signature": signature,
        "maxnumber": _ALTCHA_MAX_NUMBER,
    }


async def _verify_altcha(token: str, replay_guard: ReplayGuard) -> None:
    """Validate an altcha payload (base64 JSON): signature, proof-of-work, expiry, single use."""
    try:
        payload = json.loads(base64.b64decode(token, validate=True))
        algorithm, challenge, salt = payload["algorithm"], payload["challenge"], payload["salt"]
        signature, number = payload["signature"], int(payload["number"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise CaptchaRejected("malformed altcha payload") from exc
    if algorithm != "SHA-256" or not all(isinstance(v, str) for v in (challenge, salt, signature)):
        raise CaptchaRejected("unsupported altcha payload")
    match = _ALTCHA_SALT_RE.fullmatch(salt)
    if match is None:
        raise CaptchaRejected("malformed altcha salt")
    if not secrets.compare_digest(_altcha_sign(salt, challenge), signature):
        raise CaptchaRejected("bad altcha signature")
    if not secrets.compare_digest(
        hashlib.sha256(f"{salt}{number}".encode()).hexdigest(), challenge
    ):
        raise CaptchaRejected("altcha proof-of-work not solved")
    now = time.time()
    expires = int(match.group(1))
    if expires < now:
        raise CaptchaRejected("altcha challenge expired")
    if expires > now + _ALTCHA_TTL_SECONDS + _ALTCHA_REPLAY_SLACK:
        raise CaptchaRejected("altcha expiry out of range")  # signed, but never issued by us
    remembered_for = int(expires - now) + _ALTCHA_REPLAY_SLACK
    if not await replay_guard(challenge, remembered_for):
        raise CaptchaRejected("altcha challenge already used")


async def _verify_remote(name: str, token: str, remote_ip: str | None) -> None:
    """POST the token to Turnstile/reCAPTCHA; anything but an explicit success fails closed."""
    form = {"secret": os.environ["DSAR_CAPTCHA_SECRET"], "response": token}
    if remote_ip:
        form["remoteip"] = remote_ip
    timeout = _timeout()
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=_TRANSPORT
        ) as client:
            response = await asyncio.wait_for(
                client.post(_VERIFY_URLS[name], data=form), timeout=timeout + 1
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "outage"})
        logger.error(
            "dsar.captcha_provider_unreachable", extra={"provider": name, "err": type(exc).__name__}
        )
        raise CaptchaUnavailable("captcha provider unreachable") from exc
    finally:
        telemetry.captcha_duration.record(time.monotonic() - started, {"provider": name})
    if response.status_code != 200:
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "provider_status"})
        logger.error(
            "dsar.captcha_provider_status", extra={"provider": name, "status": response.status_code}
        )
        raise CaptchaUnavailable("captcha provider error")
    try:
        body = response.json()
    except ValueError as exc:
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "provider_body"})
        raise CaptchaUnavailable("captcha provider returned invalid JSON") from exc
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CaptchaRejected("captcha not passed")
    _check_binding(name, body)


def _expected_hostnames() -> set[str]:
    """Accepted provider hostnames (env, else the verify-URL host); empty -> fail closed."""
    raw = os.environ.get("DSAR_CAPTCHA_HOSTNAMES", "")
    names = {h.strip().lower() for h in raw.split(",") if h.strip()}
    if not names:
        host = urlparse(os.environ.get("DSAR_VERIFY_URL_BASE", "")).hostname
        if host:
            names.add(host.lower())
    return names


def _check_binding(name: str, body: dict[str, Any]) -> None:
    """Bind a provider success to OUR site, action and (reCAPTCHA v3) score; fail closed."""
    allowed = _expected_hostnames()
    if not allowed:
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "no_hostname_config"})
        logger.error("dsar.captcha_hostnames_unconfigured")
        raise CaptchaUnavailable("captcha hostname allowlist not configured")
    expected_action = (
        os.environ.get("DSAR_CAPTCHA_ACTION", DEFAULT_ACTION).strip() or DEFAULT_ACTION
    )
    v3 = name == PROVIDER_RECAPTCHA and (
        os.environ.get("DSAR_RECAPTCHA_VERSION", "v3").strip().lower() != "v2"
    )
    hostname = body.get("hostname")
    if hostname is None and name == PROVIDER_TURNSTILE:
        pass  # Turnstile: verify when returned
    elif not isinstance(hostname, str) or hostname.lower() not in allowed:
        raise CaptchaRejected("captcha hostname mismatch")
    action = body.get("action")
    if v3 or (name == PROVIDER_TURNSTILE and action is not None):
        if action != expected_action:
            raise CaptchaRejected("captcha action mismatch")
    if v3:
        try:
            min_score = float(os.environ.get("DSAR_CAPTCHA_MIN_SCORE", "0.5"))
        except ValueError:
            min_score = 0.5
        score = body.get("score")
        if isinstance(score, bool) or not isinstance(score, int | float) or score < min_score:
            raise CaptchaRejected("captcha score too low or missing")


async def verify(token: str | None, *, remote_ip: str | None, replay_guard: ReplayGuard) -> str:
    """Verify `token`; returns the provider name on success.

    Raises `CaptchaUnavailable` (503, fail closed) or `CaptchaRejected` (400). Failures are counted
    by provider + reason only; the token/secret are never logged.
    """
    name = provider()
    if name is None:
        telemetry.captcha_failures.add(1, {"provider": "none", "reason": "unconfigured"})
        logger.error("dsar.captcha_unconfigured")
        raise CaptchaUnavailable(
            "captcha provider configured but its key/secret is missing or invalid"
        )
    if not token or len(token) > _MAX_TOKEN_CHARS or not token.isprintable():
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "missing"})
        raise CaptchaRejected("captcha token missing or malformed")
    try:
        if name == PROVIDER_ALTCHA:
            await _verify_altcha(token, replay_guard)
        else:
            await _verify_remote(name, token, remote_ip)
    except CaptchaRejected:
        telemetry.captcha_failures.add(1, {"provider": name, "reason": "rejected"})
        raise
    logger.debug("dsar.captcha_passed", extra={"provider": name})
    return name
