"""Shared WIT-generated-binding shapes for waddle-sdk's host-side pytest suite.

No WASM/wasmtime here -- every dataclass below reproduces the exact shape
``componentize-py==0.25.1``'s ``bindings`` subcommand generates from the
committed ``wit/waddle-bundle/stage.wit`` (verified directly by running
``componentize-py -d wit/waddle-bundle -w stage bindings <dir>`` during
development of this SDK and reading the output -- not guessed). Individual
test modules compose these into a fake ``wit_world`` package (via
``sys.modules`` monkeypatching) with test-specific function bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class Err[E](Exception):
    """Matches ``componentize_py_types.Err`` -- the failure arm of a WIT ``result``."""

    value: E


@dataclass
class Ok[T]:
    """Matches ``componentize_py_types.Ok`` -- the success arm of a WIT ``result``."""

    value: T


# ---- wit_world.imports.db ----


@dataclass
class Value_NullValue:
    """Matches the generated ``db.Value_NullValue`` case."""


@dataclass
class Value_BoolValue:
    """Matches the generated ``db.Value_BoolValue`` case."""

    value: bool


@dataclass
class Value_IntValue:
    """Matches the generated ``db.Value_IntValue`` case."""

    value: int


@dataclass
class Value_FloatValue:
    """Matches the generated ``db.Value_FloatValue`` case."""

    value: float


@dataclass
class Value_TextValue:
    """Matches the generated ``db.Value_TextValue`` case."""

    value: str


@dataclass
class Value_BytesValue:
    """Matches the generated ``db.Value_BytesValue`` case."""

    value: bytes


@dataclass
class DbRows:
    """Matches the generated ``db.Rows`` record."""

    columns: list[str]
    rows: list[list[Any]]
    rows_affected: int


@dataclass
class DbError_Backend:
    """Matches the generated ``db.Error_Backend`` case."""

    value: str


# ---- wit_world.imports.http ----


@dataclass
class Header:
    """Matches the generated ``http.Header`` record."""

    name: str
    value: str


@dataclass
class Request:
    """Matches the generated ``http.Request`` record."""

    method: str
    url: str
    headers: list[Header]
    body: bytes | None
    secret_refs: list[tuple[str, str]]


@dataclass
class Response:
    """Matches the generated ``http.Response`` record."""

    status: int
    headers: list[Header]
    body: bytes
    truncated: bool


@dataclass
class HttpError_Denied:
    """Matches the generated ``http.Error_Denied`` case.

    Prefixed ``Http`` only to avoid a Python identifier collision with
    ``db``'s/``kv``'s/``relay``'s own same-named ``Error_*`` cases in this one
    shared module -- real componentize-py bindings define each interface's
    error cases in that interface's own module, so there is no collision
    there. ``__name__``/``__qualname__`` are forced back to the real
    generated name below, since ``waddle_sdk.http``'s error classification
    dispatches on ``type(value).__name__`` and must see ``"Error_Denied"``,
    not ``"HttpError_Denied"``.
    """

    value: str


HttpError_Denied.__name__ = "Error_Denied"
HttpError_Denied.__qualname__ = "Error_Denied"


@dataclass
class HttpError_Timeout:
    """Matches the generated ``http.Error_Timeout`` case. See ``HttpError_Denied`` docstring."""


HttpError_Timeout.__name__ = "Error_Timeout"
HttpError_Timeout.__qualname__ = "Error_Timeout"


@dataclass
class HttpError_RateLimited:
    """Matches the generated ``http.Error_RateLimited`` case. See ``HttpError_Denied`` docstring."""

    value: int


HttpError_RateLimited.__name__ = "Error_RateLimited"
HttpError_RateLimited.__qualname__ = "Error_RateLimited"


@dataclass
class HttpError_Transport:
    """Matches the generated ``http.Error_Transport`` case. See ``HttpError_Denied`` docstring."""

    value: str


HttpError_Transport.__name__ = "Error_Transport"
HttpError_Transport.__qualname__ = "Error_Transport"


@dataclass
class HttpError_TooLarge:
    """Matches the generated ``http.Error_TooLarge`` case. See ``HttpError_Denied`` docstring."""

    value: int


HttpError_TooLarge.__name__ = "Error_TooLarge"
HttpError_TooLarge.__qualname__ = "Error_TooLarge"


# ---- wit_world.imports.log ----


class Level(Enum):
    """Matches the generated ``log.Level`` enum exactly (including int values)."""

    ERROR = 0
    WARN = 1
    INFO = 2
    DEBUG = 3


# ---- wit_world.imports.context ----


@dataclass
class BundleContext:
    """Matches the generated ``context.BundleContext`` record."""

    tenant: str
    community: str | None
    app_id: str
    feature: str
    version: str
    message_id: str
    config_json: str


# ---- wit_world.imports.types ----


@dataclass
class WitPlatformEvent:
    """Matches the generated ``types.PlatformEvent`` record."""

    platform: str
    event_type: str
    actor: str | None
    payload_json: str
    occurred_at: str


@dataclass
class WitStageEnvelope:
    """Matches the generated ``types.StageEnvelope`` record."""

    tenant: str
    community: str | None
    app_id: str
    stage: str
    event: WitPlatformEvent
    ts: str
    target_app_id: str | None
    trace_context: str | None


@dataclass
class TransportResult:
    """Matches the generated ``types.TransportResult`` record."""

    ok: bool
    status: int | None
    detail: str | None
    provider_message_id: str | None


@dataclass
class TransportError:
    """Matches the generated ``types.TransportError`` record."""

    retryable: bool
    code: str
    message: str
    retry_after_ms: int | None


@dataclass
class UnsupportedStage:
    """Matches the generated ``types.UnsupportedStage`` record."""

    stage: str
