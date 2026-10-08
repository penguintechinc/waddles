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
class ColumnValue:
    """Matches the generated ``db.ColumnValue`` record."""

    column: str
    value: Any


@dataclass
class DbRow:
    """Matches the generated ``db.Row`` record."""

    row_id: str
    version: int
    columns: list[ColumnValue]


@dataclass
class OrderColumn:
    """Matches the generated ``db.OrderColumn`` record."""

    name: str
    descending: bool


@dataclass
class OrderBy_Column:
    """Matches the generated ``db.OrderBy_Column`` variant case."""

    value: OrderColumn


@dataclass
class OrderBy_Random:
    """Matches the generated ``db.OrderBy_Random`` variant case (no payload)."""


@dataclass
class DbError_Denied:
    """Matches the generated ``db.Error_Denied`` case.

    Prefixed ``Db`` only to avoid a Python identifier collision with
    ``http``'s/``kv``'s own same-named ``Error_*`` cases in this one shared
    module -- see ``HttpError_Denied``'s own docstring for the full
    rationale. ``__name__``/``__qualname__`` forced back to the real
    generated name below, since ``waddle_sdk.db``'s error classification
    dispatches on ``type(value).__name__``.
    """

    value: str


DbError_Denied.__name__ = "Error_Denied"
DbError_Denied.__qualname__ = "Error_Denied"


@dataclass
class DbError_InvalidColumn:
    """Matches the generated ``db.Error_InvalidColumn`` case. See ``DbError_Denied`` docstring."""

    value: str


DbError_InvalidColumn.__name__ = "Error_InvalidColumn"
DbError_InvalidColumn.__qualname__ = "Error_InvalidColumn"


@dataclass
class DbError_InvalidValue:
    """Matches the generated ``db.Error_InvalidValue`` case. See ``DbError_Denied`` docstring."""

    value: str


DbError_InvalidValue.__name__ = "Error_InvalidValue"
DbError_InvalidValue.__qualname__ = "Error_InvalidValue"


@dataclass
class DbError_NotFound:
    """Matches ``db.Error_NotFound`` (no payload). See ``DbError_Denied`` docstring."""


DbError_NotFound.__name__ = "Error_NotFound"
DbError_NotFound.__qualname__ = "Error_NotFound"


@dataclass
class DbError_Conflict:
    """Matches the generated ``db.Error_Conflict`` case. See ``DbError_Denied`` docstring."""

    value: str


DbError_Conflict.__name__ = "Error_Conflict"
DbError_Conflict.__qualname__ = "Error_Conflict"


@dataclass
class DbError_QuotaExceeded:
    """Matches the generated ``db.Error_QuotaExceeded`` case. See ``DbError_Denied`` docstring."""

    value: str


DbError_QuotaExceeded.__name__ = "Error_QuotaExceeded"
DbError_QuotaExceeded.__qualname__ = "Error_QuotaExceeded"


@dataclass
class DbError_Timeout:
    """Matches ``db.Error_Timeout`` (no payload). See ``DbError_Denied`` docstring."""


DbError_Timeout.__name__ = "Error_Timeout"
DbError_Timeout.__qualname__ = "Error_Timeout"


@dataclass
class DbError_Backend:
    """Matches the generated ``db.Error_Backend`` case. See ``DbError_Denied`` docstring."""

    value: str


DbError_Backend.__name__ = "Error_Backend"
DbError_Backend.__qualname__ = "Error_Backend"


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
