"""`!weather <location>` -> current conditions (WeatherAPI.com). SuperPenguin #496 request.

The logic is original (nothing ported from PenguinTwitchBot), so no upstream attribution
notice applies.

**Provider / egress.** WeatherAPI.com (`https://api.weatherapi.com/v1/current.json`) -- a
non-sanctioned, Western-based provider. The only egress is the single V2 permission
`net.http.fqdn:api.weatherapi.com` (GET only), mirrored in the legacy `egress:` list; the
host denies every other destination.

**API key handling (never in code).** The key is sent as the `key` request header through a
WIT `secret-ref` (`SecretRef("WEATHER_API_KEY")`): the stage resolves the reference name to
the operator-provisioned secret and injects the header, so the value NEVER enters this
component, its config, its manifest, its logs or this repository. The constant below is a
secret NAME, not a secret. If the secret is missing/invalid the provider (or the stage)
rejects the call, which surfaces as an explicit "unavailable" reply, never a fake answer.

**Fail-loud, no silent fallback.** Every failure path logs (status code / exception TYPE
only) AND replies with an explicit message -- there is no default weather, no cached guess:

* transport/denied/timeout/rate-limit -> ``weather.api_unavailable`` + "unavailable" reply
* HTTP 401/403 (bad/missing key)      -> ``weather.auth_rejected`` + "not configured" reply
* HTTP 400 w/ provider code 1006      -> "couldn't find that location" (a user error, INFO)
* any other non-200, truncated, malformed JSON, missing fields
                                      -> ``weather.bad_response`` + "unavailable" reply

**PII-free logs.** The location the user typed is sent to the provider (that is the feature)
but is NEVER logged, stored or echoed from the user's text -- the reply names the place as
the provider resolved it. No `kv`, no user identity is touched at all.

Gated behind the PostHog flag ``waddles.command-weather``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, resolve_secret

FLAG_KEY = "waddles.command-weather"
COMMAND = "!weather"

API_HOST = "api.weatherapi.com"
API_URL = f"https://{API_HOST}/v1/current.json"
#: Header the provider reads the key from, and the secret-ref NAME the stage resolves for it.
API_KEY_HEADER = "key"
API_KEY_SECRET_REF = "WEATHER_API_KEY"  # noqa: S105 - a secret reference name, not a value

MAX_LOCATION_LEN = 100
_PROVIDER_NO_MATCH_CODE = 1006

_USAGE = "Usage: !weather <location>"
_UNAVAILABLE = "The weather service is unavailable right now - please try again later."
_NOT_CONFIGURED = "The weather service isn't configured correctly - please tell a moderator."


class _WeatherError(Exception):
    """Internal-only: carries the explicit user-facing reply for a failure already logged."""

    def __init__(self, reply: str) -> None:
        """Record the reply to send back to chat."""
        super().__init__(reply)
        self.reply = reply


def _validate_location(raw: str) -> str | None:
    """Return an error message, or `None` if `raw` is an acceptable location query."""
    if not raw:
        return _USAGE
    if len(raw) > MAX_LOCATION_LEN:
        return f"Locations must be {MAX_LOCATION_LEN} characters or fewer."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return "That location contains characters I can't use."
    return None


def _num(value: Any) -> float | int:
    """Return `value` if it's a real number (not bool), else raise `ValueError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return value


def _format_conditions(body: bytes) -> str:
    """Build the reply from a provider 200 body.

    Raises:
        ValueError: Not JSON, or a required field is missing/mistyped (caller logs + replies).
    """
    data = json.loads(body.decode("utf-8"))
    loc = data["location"]
    cur = data["current"]
    place = ", ".join(str(p) for p in (loc["name"], loc.get("country")) if p)
    text = str(cur["condition"]["text"])
    temp_c, temp_f = _num(cur["temp_c"]), _num(cur["temp_f"])
    feels_c = _num(cur["feelslike_c"])
    humidity = _num(cur["humidity"])
    wind_kph = _num(cur["wind_kph"])
    return (
        f"Weather in {place}: {text}, {temp_c:g}°C / {temp_f:g}°F "
        f"(feels like {feels_c:g}°C), humidity {humidity:g}%, wind {wind_kph:g} km/h."
    )


def _is_no_match(body: bytes) -> bool:
    """True iff a provider 400 body carries the documented "no matching location" code."""
    try:
        return bool(json.loads(body.decode("utf-8"))["error"]["code"] == _PROVIDER_NO_MATCH_CODE)
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return False


async def _fetch(location: str) -> str:
    """Call the provider and return the reply text.

    Raises:
        _WeatherError: Any failure -- already logged, carries the explicit reply.
    """
    url = f"{API_URL}?q={quote(location, safe='')}&aqi=no"
    try:
        response = await HttpClient().get(
            url, secret_refs={API_KEY_HEADER: resolve_secret(API_KEY_SECRET_REF)}
        )
    except Exception as exc:  # noqa: BLE001 - SDK raises Retryable/NonRetryable transport errors
        log.error("weather.api_unavailable", error_type=type(exc).__name__)
        raise _WeatherError(_UNAVAILABLE) from exc

    status = response["status"]
    body: bytes = response["body"]
    if status in (401, 403):
        log.error("weather.auth_rejected", status=status)
        raise _WeatherError(_NOT_CONFIGURED)
    if status == 400 and _is_no_match(body):
        log.info("weather.location_not_found")
        raise _WeatherError("I couldn't find that location.")
    if status != 200 or response["truncated"]:
        log.error("weather.bad_response", status=status, truncated=bool(response["truncated"]))
        raise _WeatherError(_UNAVAILABLE)
    try:
        return _format_conditions(body)
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        log.error("weather.bad_response", status=status, error_type=type(exc).__name__)
        raise _WeatherError(_UNAVAILABLE) from exc


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!weather <location>` and reply.

    Exact first-token match comes before the flag check. Every failure replies explicitly
    (see module docstring); nothing is ever silently dropped or defaulted.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    location = rest.strip()
    reply: str
    error = _validate_location(location)
    if error is not None:
        reply = error
    else:
        try:
            reply = await _fetch(location)
        except _WeatherError as exc:
            reply = exc.reply

    log.info("weather.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("weather reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("weather reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("weather.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
