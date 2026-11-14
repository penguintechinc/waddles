"""hub-api OpenTelemetry bootstrap: traces, metrics and logs over OTLP/gRPC from ``OTEL_*``.

Owner requirement (2026-11-14): every service emits all three signals through OpenTelemetry to
the SigNoz collector so any issue can be split into traces, time metrics and logs. Before this
module hub-api only used the OTel *API* (no provider, no exporter), so everything was dropped.

Shape
-----
* :func:`install` runs in ``create_app`` (sync, cheap): wraps ``app.asgi_app`` with a late-bound
  ASGI shim and registers the Quart request hooks (route-templated duration histogram, in-flight
  counter, ``http.route`` / numeric ``tenant_id`` / ``community_id`` span attributes).
* :func:`start` runs first thing in ``before_serving``: evaluates the kill switch, builds the
  Tracer/Meter/LoggerProvider + OTLP exporters, sets the W3C tracecontext+baggage propagator,
  installs the ASGI, SQLAlchemy and gRPC-client instrumentation and attaches a PII-scrubbing
  OTLP log handler.
* :meth:`TelemetryRuntime.shutdown` runs in ``after_serving`` with a bounded flush.

Rules enforced here (critical-rules.md Observability, security.md)
------------------------------------------------------------------
* Destination, protocol, headers, timeout, sampler, per-signal exporter and batch sizes come
  from the standard ``OTEL_*`` env only; nothing about the destination is hardcoded (no
  ``insecure=True``: the endpoint scheme decides).
* A dead exporter never breaks startup, slows a request or crashes: bounded drop-oldest queues,
  export timeouts, non-blocking batch processors, a daemon-thread bounded shutdown.
* PII/secrets never leave the process: every exported span passes
  :class:`_ScrubbingSpanExporter` and every log record :class:`_ScrubbingLoggingHandler`
  (emails, bearer/JWT, secret query params, username/token-style attribute keys, client IPs).
  Only numeric ids are added by our own code.
* Kill switch ``waddles.disable-hub-api-otel`` (opt-out: unseen/OFF = telemetry ON), evaluated
  once at startup (PostHog -> ``FLAG_WADDLES_DISABLE_HUB_API_OTEL`` env baseline -> default OFF);
  a flip needs a restart.
* Endpoint unset: a single INFO line says export is disabled and why; never an exception.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry import metrics, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import Meter, NoOpMeterProvider
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import Status
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from quart import Quart, g, request

from config import HubAPIConfig

logger = logging.getLogger(__name__)

#: Opt-out kill switch (critical-rules.md "core platform mechanisms"): unseen/OFF = telemetry on.
KILL_SWITCH_FLAG: Final = "waddles.disable-hub-api-otel"
#: Operator-plane tenant the (platform-wide) kill switch is evaluated against.
_KILL_SWITCH_TENANT: Final = "system"
_KILL_SWITCH_TIMEOUT_SECONDS: Final = 2.0
#: Hard ceiling on the final flush: a dead collector must not hold shutdown (and the pod) hostage.
SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0

#: Probe routes are excluded from tracing (anchored: `/health` must not match `/api/v1/health-x`).
_EXCLUDED_URLS: Final = "/health$,/healthz$,/ready$"
_INSTRUMENTATION_SCOPE: Final = "hub_api.http"


# ---------------------------------------------------------------------------
# env parsing
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    """Stripped env value, ``""`` when unset."""
    return os.environ.get(name, "").strip()


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """Float env value clamped to ``[lo, hi]``; a malformed value logs once and uses ``default``."""
    raw = _env(name)
    if not raw:
        return default
    try:
        return min(hi, max(lo, float(raw)))
    except ValueError:
        logger.warning("telemetry: ignoring non-numeric %s=%r", name, raw)
        return default


def _otlp_timeout_seconds(signal: str) -> float:
    """Export timeout in SECONDS from the per-signal or generic ``OTEL_EXPORTER_OTLP_*TIMEOUT``.

    The OTel spec defines these in MILLISECONDS (the chart renders 5000), but the Python exporter
    reads
    the raw value as seconds, so an unconverted ``5000`` would block an export worker for ~83
    minutes
    against a dead collector. We convert here and pass ``timeout=`` explicitly. Clamped to 0.1..30
    s.
    """
    raw = _env(f"OTEL_EXPORTER_OTLP_{signal}_TIMEOUT") or _env("OTEL_EXPORTER_OTLP_TIMEOUT")
    if not raw:
        return 5.0
    try:
        return min(30.0, max(0.1, float(raw) / 1000.0))
    except ValueError:
        logger.warning("telemetry: ignoring non-numeric OTLP timeout %r", raw)
        return 5.0


def _batch_settings(prefix: str) -> tuple[int, int, float]:
    """Bounded ``(queue size, batch size, schedule delay ms)`` from ``OTEL_{prefix}_*`` env.

    ``prefix`` is ``BSP`` for spans, ``BLRP`` for logs. Always explicit and always bounded: the
    queue is a drop-oldest deque (memory can never grow with a dead collector) and the batch size
    is clamped to the queue size (the SDK refuses a larger batch).
    """
    queue = int(_env_float(f"OTEL_{prefix}_MAX_QUEUE_SIZE", 2048, 1, 65536))
    batch = int(_env_float(f"OTEL_{prefix}_MAX_EXPORT_BATCH_SIZE", 512, 1, 65536))
    delay = _env_float(f"OTEL_{prefix}_SCHEDULE_DELAY", 5000, 10, 600_000)
    return queue, min(batch, queue), delay


def _signal_exporter(signal: str) -> str:
    """``otlp`` or ``none`` for a signal (``OTEL_<SIGNAL>_EXPORTER``); unknown disables it."""
    raw = (_env(f"OTEL_{signal}_EXPORTER") or "otlp").lower()
    if raw in ("otlp", "none"):
        return raw
    logger.warning(
        "telemetry: unsupported OTEL_%s_EXPORTER=%r (supported: otlp, none); %s export disabled",
        signal,
        raw,
        signal.lower(),
    )
    return "none"


def build_sampler() -> Sampler:
    """Sampler from ``OTEL_TRACES_SAMPLER[_ARG]`` (default ``parentbased_always_on``)."""
    name = (_env("OTEL_TRACES_SAMPLER") or "parentbased_always_on").lower()
    ratio = _env_float("OTEL_TRACES_SAMPLER_ARG", 1.0, 0.0, 1.0)
    samplers: dict[str, Callable[[], Sampler]] = {
        "always_on": lambda: ALWAYS_ON,
        "always_off": lambda: ALWAYS_OFF,
        "traceidratio": lambda: TraceIdRatioBased(ratio),
        "parentbased_always_on": lambda: ParentBased(ALWAYS_ON),
        "parentbased_always_off": lambda: ParentBased(ALWAYS_OFF),
        "parentbased_traceidratio": lambda: ParentBased(TraceIdRatioBased(ratio)),
    }
    factory = samplers.get(name)
    if factory is None:
        logger.warning(
            "telemetry: unknown OTEL_TRACES_SAMPLER=%r; using parentbased_always_on", name
        )
        return ParentBased(ALWAYS_ON)
    return factory()


def build_resource(cfg: HubAPIConfig) -> Resource:
    """Resource with service.name/version, deployment.environment and ``service.instance.id``.

    Defaults come from config; ``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES`` (rendered by the
    chart) win over them.
    """
    pod = _env("K8S_POD_NAME") or socket.gethostname()
    defaults: dict[str, Any] = {
        "service.name": cfg.module_name,
        "service.version": cfg.module_version,
        "deployment.environment": cfg.deployment_env,
        "service.instance.id": f"{pod}-{uuid.uuid4().hex[:8]}",
    }
    return Resource.create(defaults).merge(OTELResourceDetector().detect())


def _endpoint_configured() -> bool:
    """True when a generic or per-signal OTLP endpoint is set."""
    names = (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    )
    return any(_env(n) for n in names)


# ---------------------------------------------------------------------------
# PII / secret scrubbing
# ---------------------------------------------------------------------------

_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_JWT_RE: Final = re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*")
_BEARER_RE: Final = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_SECRET_KV_RE: Final = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|token|code|state|secret|client_secret|password|"
    r"passwd|api_key|apikey|key|sig|signature|authorization|email|username|user|name|handle)"
    r"=([^&\s\"',;]+)"
)
#: Attribute keys whose value is never exported (token-delimited: `user_name`,
#: `http.request.header.authorization`).
_SENSITIVE_KEY_RE: Final = re.compile(
    r"(?i)(^|[._\-])(email|e-mail|username|user[_\-]?name|user|password|passwd|secret|token|"
    r"authorization|cookie|set-cookie|api[_\-]?key|session|body|payload|phone|"
    r"display[_\-]?name|handle|enduser)([._\-]|$)"
)
#: Client network identity / user agent: personal data, dropped entirely from spans and logs.
_DROP_KEYS: Final = frozenset(
    {
        "client.address",
        "client.port",
        "net.peer.ip",
        "net.peer.port",
        "net.sock.peer.addr",
        "net.sock.peer.port",
        "network.peer.address",
        "network.peer.port",
        "http.client_ip",
        "http.user_agent",
        "user_agent.original",
        "url.query",
        "http.query",
    }
)
_URL_KEYS: Final = frozenset({"http.url", "url.full", "http.target", "http.request.url"})
_QUERY_RE: Final = re.compile(r"\?[^\s#]*")
REDACTED: Final = "[redacted]"


def scrub_text(value: str) -> str:
    """Redact emails, bearer/JWT tokens and secret-looking ``k=v`` pairs from free text."""
    value = _BEARER_RE.sub(r"\1 " + REDACTED, value)
    value = _JWT_RE.sub(REDACTED, value)
    value = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
    return _EMAIL_RE.sub(REDACTED, value)


def _scrub_value(key: str, value: Any) -> Any:
    """Scrub one attribute value (strings and string sequences); other primitives pass through."""
    if isinstance(value, str):
        if key in _URL_KEYS:
            value = _QUERY_RE.sub("?" + REDACTED, value)
        return scrub_text(value)
    if isinstance(value, list | tuple):
        return [scrub_text(v) if isinstance(v, str) else v for v in value]
    return value


def scrub_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy ``attributes`` dropping/redacting sensitive keys and scrubbing string values."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in _DROP_KEYS:
            continue
        if _SENSITIVE_KEY_RE.search(key):
            out[key] = REDACTED
            continue
        out[key] = _scrub_value(key, value)
    return out


class _ScrubbingSpanExporter(SpanExporter):
    """Wraps the OTLP span exporter so no span (ours or a library's) leaks PII."""

    def __init__(self, inner: SpanExporter) -> None:
        """Wrap ``inner``."""
        self._inner = inner

    @staticmethod
    def _clean(span: ReadableSpan) -> ReadableSpan:
        """Rebuild ``span`` with scrubbed name/attributes/events/status description."""
        status = span.status
        return ReadableSpan(
            name=scrub_text(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=scrub_attributes(span.attributes),
            events=tuple(
                Event(e.name, scrub_attributes(e.attributes), e.timestamp) for e in span.events
            ),
            links=span.links,
            kind=span.kind,
            status=Status(
                status.status_code, scrub_text(status.description) if status.description else None
            ),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Scrub then forward; a scrubbing bug drops the batch rather than leaking it."""
        try:
            cleaned = [self._clean(s) for s in spans]
        except Exception:  # noqa: BLE001 - never export unscrubbed data, never raise
            logger.exception("telemetry: span scrub failed; batch dropped")
            return SpanExportResult.FAILURE
        return self._inner.export(cleaned)

    def shutdown(self) -> None:
        """Shut down the wrapped exporter."""
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Delegate to the wrapped exporter."""
        return self._inner.force_flush(timeout_millis)


_STD_RECORD_KEYS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
#: Logger prefixes never forwarded to OTLP: the exporter's own diagnostics would feed back into
#: itself.
_SELF_LOGGERS: Final = ("opentelemetry", "grpc")


class _ScrubbingLoggingHandler(LoggingHandler):
    """OTLP log handler that scrubs the message, exception text and extras before translation.

    Trace/span ids come from the active span context at emit time (the SDK handler reads it), which
    is
    what lets SigNoz jump from a log line to its trace.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a scrubbed copy of ``record``; never raise into the logging caller."""
        if record.name.startswith(_SELF_LOGGERS):
            return
        try:
            rec = copy.copy(record)
            rec.msg = scrub_text(record.getMessage())
            rec.args = ()
            flat: dict[str, Any] = {}
            for key in [k for k in rec.__dict__ if k not in _STD_RECORD_KEYS]:
                value = rec.__dict__.pop(key)
                if isinstance(value, Mapping):
                    for sub_key, sub_value in value.items():
                        flat[f"{key}.{sub_key}"] = sub_value
                else:
                    flat[key] = value
            for key, value in scrub_attributes(
                {k: v for k, v in flat.items() if v is not None}
            ).items():
                rec.__dict__[key] = (
                    value if isinstance(value, str | bool | int | float) else str(value)
                )
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                rec.__dict__["exception.type"] = type(exc).__name__
                rec.__dict__["exception.message"] = scrub_text(str(exc))
                rec.__dict__["exception.stacktrace"] = scrub_text(
                    "".join(traceback.format_exception(*record.exc_info))
                )
                rec.exc_info = None
                rec.exc_text = None
            super().emit(rec)
        except Exception:  # noqa: BLE001 - telemetry failure is never a request failure
            self.handleError(record)


# ---------------------------------------------------------------------------
# HTTP hooks + ASGI shim
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _HttpInstruments:
    """HTTP metric instruments, built from the *explicit* MeterProvider in :func:`start`."""

    duration: Any
    in_flight: Any


class _TelemetryASGI:
    """Late-bound ASGI shim: passthrough until :func:`start` activates the OTel middleware.

    Lets ``create_app`` wrap ``app.asgi_app`` without a provider (the SDK is built in
    ``before_serving``)
    and keeps lifespan scopes entirely outside the middleware.
    """

    def __init__(self, inner: Callable[..., Awaitable[None]]) -> None:
        """Remember the wrapped ASGI app."""
        self.inner = inner
        self.active: Callable[..., Awaitable[None]] | None = None

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        """Dispatch http/websocket scopes through the OTel middleware when active."""
        target = (
            self.active
            if self.active is not None and scope["type"] in ("http", "websocket")
            else self.inner
        )
        await target(scope, receive, send)


@dataclass(slots=True)
class _HttpTelemetry:
    """Per-app hook state stored in ``app.extensions``."""

    shim: _TelemetryASGI
    instruments: _HttpInstruments | None = None


_EXT_KEY: Final = "hub_api_telemetry"


def _route_template() -> str:
    """Route template of the current request (``unmatched`` for 404s) -- never the raw path."""
    rule = request.url_rule
    return str(rule.rule) if rule is not None else "unmatched"


def _numeric(value: Any) -> int | None:
    """``value`` as a non-bool int, else ``None`` (ids in spans must be numeric)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _annotate_span(route: str) -> None:
    """Put the route template and numeric tenant/community ids on the active server span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("http.route", route)
    span.update_name(f"{request.method} {route}")
    if route != "unmatched":
        for key in ("http.target", "url.path"):
            span.set_attribute(key, route)
        attrs = getattr(span, "attributes", None) or {}
        for key in ("http.url", "url.full"):
            current = attrs.get(key)
            if isinstance(current, str) and "://" in current:
                scheme, rest = current.split("://", 1)
                host = rest.split("/", 1)[0].split("?", 1)[0]
                span.set_attribute(key, f"{scheme}://{host}{route}")
    ctx = getattr(request, "tenant_context", None)
    tenant_id = _numeric(getattr(ctx, "tenant_id", None))
    if tenant_id is not None:
        span.set_attribute("tenant_id", tenant_id)
    community_id = _numeric((request.view_args or {}).get("community_id"))
    if community_id is not None:
        span.set_attribute("community_id", community_id)


def install(app: Quart) -> None:
    """Wire the ASGI shim and request hooks. Idempotent per app; safe before any provider exists."""
    if _EXT_KEY in app.extensions:
        return
    state = _HttpTelemetry(shim=_TelemetryASGI(app.asgi_app))
    app.extensions[_EXT_KEY] = state
    app.asgi_app = state.shim  # type: ignore[method-assign, assignment]

    @app.before_request
    async def _otel_begin() -> None:
        """Start the request clock and bump the in-flight counter."""
        inst = state.instruments
        g.otel_t0 = time.perf_counter()
        g.otel_method = request.method
        g.otel_status = 500
        g.otel_counted = inst is not None
        if inst is not None:
            inst.in_flight.add(1, {"http.request.method": request.method})

    @app.after_request
    async def _otel_annotate(response: Any) -> Any:
        """Record the final status and annotate the span with the route template."""
        g.otel_status = response.status_code
        try:
            _annotate_span(_route_template())
        except Exception:  # noqa: BLE001 - telemetry failure is never a request failure
            logger.debug("telemetry: span annotation failed", exc_info=True)
        return response

    @app.teardown_request
    async def _otel_end(_exc: BaseException | None) -> None:
        """Record the duration histogram (route template, method, status); drop in-flight."""
        inst = state.instruments
        if inst is None or not getattr(g, "otel_counted", False):
            return
        try:
            method = g.otel_method
            inst.in_flight.add(-1, {"http.request.method": method})
            inst.duration.record(
                time.perf_counter() - g.otel_t0,
                {
                    "http.request.method": method,
                    "http.route": _route_template(),
                    "http.response.status_code": int(g.otel_status),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry failure is never a request failure
            logger.debug("telemetry: request metric failed", exc_info=True)


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TelemetryRuntime:
    """Handle returned by :func:`start`; ``enabled`` is False for every no-op (see ``reason``)."""

    enabled: bool
    reason: str
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    span_processor: BatchSpanProcessor | None = None
    log_processor: BatchLogRecordProcessor | None = None
    _undo: list[Callable[[], None]] = field(default_factory=list)

    def force_flush(self, timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Best-effort flush of all three signals (blocking; call from a thread or at shutdown)."""
        millis = int(timeout_seconds * 1000)
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            if provider is not None:
                try:
                    provider.force_flush(millis)
                except Exception:  # noqa: BLE001 - flush is best effort
                    logger.debug("telemetry: force_flush failed", exc_info=True)

    async def shutdown(self, timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Uninstrument, flush and shut providers down within ``timeout_seconds``.

        Returns True if it finished in time. The blocking SDK shutdown runs on a *daemon* thread
        so a dead collector can never hold the event loop or process exit past the bound.
        """
        if not self.enabled:
            return True
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("telemetry: undo step failed", exc_info=True)
        self._undo.clear()

        def _stop() -> None:
            self.force_flush(timeout_seconds)
            for provider in (self.tracer_provider, self.logger_provider, self.meter_provider):
                if provider is not None:
                    try:
                        provider.shutdown()
                    except Exception:  # noqa: BLE001 - shutdown must not raise
                        logger.debug("telemetry: provider shutdown failed", exc_info=True)

        worker = threading.Thread(target=_stop, name="otel-shutdown", daemon=True)
        worker.start()
        await asyncio.to_thread(worker.join, timeout_seconds)
        finished = not worker.is_alive()
        if not finished:
            logger.warning(
                "telemetry: shutdown flush exceeded %.1fs (collector unreachable?); continuing",
                timeout_seconds,
            )
        self.enabled = False
        return finished


async def _kill_switch_on() -> bool:
    """Evaluate ``waddles.disable-hub-api-otel`` (bounded; any failure = OFF = telemetry on)."""
    try:
        from flask_core.feature_flags import feature_enabled

        on = await asyncio.wait_for(
            feature_enabled(KILL_SWITCH_FLAG, tenant=_KILL_SWITCH_TENANT),
            _KILL_SWITCH_TIMEOUT_SECONDS,
        )
        return bool(on)
    except Exception as exc:  # noqa: BLE001 - flag plane down must never stop telemetry or startup
        logger.debug(
            "telemetry: kill-switch evaluation failed (%s); treating as OFF", type(exc).__name__
        )
        return False


def _attach_log_handler(
    handler: logging.Handler, extra_loggers: Sequence[logging.Logger], level: int
) -> Callable[[], None]:
    """Attach the OTLP handler to root + non-propagating loggers; returns the undo callable."""
    root = logging.getLogger()
    attached: list[logging.Logger] = [root, *extra_loggers]
    stderr_fallback: logging.Handler | None = None
    if not root.handlers:
        # Root had no handler, so WARNING+ reached stderr via logging.lastResort. Attaching ours
        # would
        # silence that; keep the same behaviour explicitly.
        stderr_fallback = logging.StreamHandler(sys.stderr)
        stderr_fallback.setLevel(logging.WARNING)
        stderr_fallback.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stderr_fallback)
    previous_level = root.level
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    quieted: list[logging.Logger] = []
    for noisy in ("httpx", "httpcore"):  # INFO lines carry full request URLs
        lg = logging.getLogger(noisy)
        if lg.level == logging.NOTSET:
            lg.setLevel(logging.WARNING)
            quieted.append(lg)
    # The SDK's own diagnostics are excluded from OTLP (feedback loop); keep them visible on stderr.
    otel_logger = logging.getLogger("opentelemetry")
    otel_handler = logging.StreamHandler(sys.stderr)
    otel_handler.setLevel(logging.WARNING)
    otel_logger.addHandler(otel_handler)
    for lg in attached:
        lg.addHandler(handler)

    def undo() -> None:
        for lg in attached:
            lg.removeHandler(handler)
        otel_logger.removeHandler(otel_handler)
        if stderr_fallback is not None:
            root.removeHandler(stderr_fallback)
        root.setLevel(previous_level)
        for lg in quieted:
            lg.setLevel(logging.NOTSET)

    return undo


async def start(app: Quart, cfg: HubAPIConfig, *, set_global: bool = True) -> TelemetryRuntime:
    """Build providers, exporters and instrumentation; never raises (disabled runtime on no-op)."""
    try:
        return await _start(app, cfg, set_global=set_global)
    except Exception:  # noqa: BLE001 - telemetry must never stop hub-api starting
        logger.exception("telemetry: initialisation failed; telemetry export is disabled")
        return TelemetryRuntime(enabled=False, reason="initialisation failed")


async def _start(app: Quart, cfg: HubAPIConfig, *, set_global: bool) -> TelemetryRuntime:
    """Implementation of :func:`start` (may raise)."""
    if not _endpoint_configured():
        reason = "OTEL_EXPORTER_OTLP_ENDPOINT is not set"
        logger.info("telemetry export is disabled: %s", reason)
        return TelemetryRuntime(enabled=False, reason=reason)
    if await _kill_switch_on():
        reason = f"kill switch {KILL_SWITCH_FLAG} is ON"
        logger.info("telemetry export is disabled: %s", reason)
        return TelemetryRuntime(enabled=False, reason=reason)
    protocol = (_env("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower()
    if protocol != "grpc":
        reason = f"unsupported OTEL_EXPORTER_OTLP_PROTOCOL={protocol!r} (only grpc is bundled)"
        logger.warning("telemetry export is disabled: %s", reason)
        return TelemetryRuntime(enabled=False, reason=reason)

    resource = build_resource(cfg)
    runtime = TelemetryRuntime(enabled=True, reason="enabled")
    modes = {s: _signal_exporter(s) for s in ("TRACES", "METRICS", "LOGS")}

    span_batch = _batch_settings("BSP")
    log_batch = _batch_settings("BLRP")
    tracer_provider = TracerProvider(resource=resource, sampler=build_sampler())
    if modes["TRACES"] == "otlp":
        runtime.span_processor = BatchSpanProcessor(
            _ScrubbingSpanExporter(OTLPSpanExporter(timeout=_otlp_timeout_seconds("TRACES"))),
            export_timeout_millis=_otlp_timeout_seconds("TRACES") * 1000,
            max_queue_size=span_batch[0],
            max_export_batch_size=span_batch[1],
            schedule_delay_millis=span_batch[2],
        )
        tracer_provider.add_span_processor(runtime.span_processor)
    runtime.tracer_provider = tracer_provider

    readers = []
    if modes["METRICS"] == "otlp":
        timeout = _otlp_timeout_seconds("METRICS")
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(timeout=timeout),
                export_interval_millis=_env_float(
                    "OTEL_METRIC_EXPORT_INTERVAL", 60000.0, 100.0, 3_600_000.0
                ),
                export_timeout_millis=timeout * 1000,
            )
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    runtime.meter_provider = meter_provider

    logger_provider = LoggerProvider(resource=resource)
    if modes["LOGS"] == "otlp":
        runtime.log_processor = BatchLogRecordProcessor(
            OTLPLogExporter(timeout=_otlp_timeout_seconds("LOGS")),
            export_timeout_millis=_otlp_timeout_seconds("LOGS") * 1000,
            max_queue_size=log_batch[0],
            max_export_batch_size=log_batch[1],
            schedule_delay_millis=log_batch[2],
        )
        logger_provider.add_log_record_processor(runtime.log_processor)
    runtime.logger_provider = logger_provider

    if set_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    meter: Meter = meter_provider.get_meter(_INSTRUMENTATION_SCOPE)
    state: _HttpTelemetry = app.extensions[_EXT_KEY]
    state.instruments = _HttpInstruments(
        duration=meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="HTTP server request duration by method, route template and status.",
        ),
        in_flight=meter.create_up_down_counter(
            "http.server.active_requests",
            unit="{request}",
            description="In-flight HTTP server requests by method.",
        ),
    )
    # The middleware's own HTTP metrics label raw paths (unbounded cardinality), so they are
    # disabled
    # and replaced by the route-templated instruments above.
    state.shim.active = OpenTelemetryMiddleware(
        state.shim.inner,
        excluded_urls=_EXCLUDED_URLS,
        # The per-message `<METHOD> <raw path> http send/receive` child spans carry the RAW path in
        # their names (ids/usernames) and triple the span volume; the server span is the signal.
        exclude_spans=["receive", "send"],
        tracer_provider=tracer_provider,
        meter_provider=NoOpMeterProvider(),
    )

    def _deactivate_http() -> None:
        state.shim.active = None
        state.instruments = None

    runtime._undo.append(_deactivate_http)

    sqlalchemy_instrumentor = SQLAlchemyInstrumentor()
    sqlalchemy_instrumentor.instrument(
        tracer_provider=tracer_provider, meter_provider=meter_provider
    )
    runtime._undo.append(sqlalchemy_instrumentor.uninstrument)
    grpc_client = GrpcAioInstrumentorClient()  # type: ignore[no-untyped-call]
    grpc_client.instrument(tracer_provider=tracer_provider)
    runtime._undo.append(grpc_client.uninstrument)

    handler = _ScrubbingLoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    aaa = app.config.get("logger")
    extra_loggers = [aaa.logger] if aaa is not None and hasattr(aaa, "logger") else []
    log_level = getattr(logging, str(cfg.log_level).upper(), logging.INFO)
    runtime._undo.append(_attach_log_handler(handler, extra_loggers, log_level))

    logger.info(
        "telemetry export enabled: service=%s protocol=grpc traces=%s metrics=%s logs=%s",
        resource.attributes.get("service.name"),
        modes["TRACES"],
        modes["METRICS"],
        modes["LOGS"],
    )
    return runtime
