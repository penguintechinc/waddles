"""OTel instrumentation for the marketplace-source flows (scan / build / approve / update).

Rides the repo's existing OTel API surface (`services.bundle_telemetry`: tracer + meter, no SDK,
no exporter). The OTLP destination is a deployment concern set ONLY through the standard
`OTEL_*` env vars -- nothing here names an endpoint. Telemetry failure is never a request
failure: every emit helper swallows (and logs the exception TYPE only) at this boundary.

Cardinality/PII rules: span attributes + metric labels carry `source_public_id` (UUID) and
bounded categories ONLY -- never a git URL, username, numeric pk, or free text. Rejection
reasons are folded to a small allow-list (`reason_category`) before they become a label.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from services.bundle_telemetry import bundle_span, get_meter

logger = logging.getLogger(__name__)

_REASON_CATEGORIES: dict[str, str] = {
    "no_bundles": "zero_examined",
    "clone_timeout": "clone",
    "git_failed": "clone",
    "ref_not_found": "clone",
    "sha_mismatch": "sha_mismatch",
    "clone_too_large": "guard",
    "too_many_files": "guard",
    "too_deep": "guard",
    "file_too_large": "guard",
    "submodule": "guard",
    "path_traversal": "guard",
    "symlink_absolute": "guard",
    "symlink_escape": "guard",
    "archive_bomb": "guard",
    "archive_corrupt": "guard",
    "artifact_unavailable": "artifact",
    "build_failed": "build",
    "output_too_large": "build",
    "output_not_wasm": "build",
}


def reason_category(reason: str) -> str:
    """Fold a machine reason code into a bounded label value (never free text)."""
    head = reason.split(":", 1)[0]
    if head in _REASON_CATEGORIES:
        return _REASON_CATEGORIES[head]
    if head.startswith(("manifest", "namespace", "denylist", "component", "attest")):
        return "validation"
    return "other"


def _safe(fn_name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """Run one emit; a telemetry failure is logged by type and swallowed, never propagated."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.warning(
            "marketplace telemetry emit failed",
            extra={"emit": fn_name, "error": type(exc).__name__},
        )


def _labels(**labels: str) -> dict[str, str]:
    return {k: v for k, v in labels.items() if v}


def record_scan(
    *, examined: int, rejected: int, outcome: str, duration_s: float, build_mode: str = ""
) -> None:
    """Scan count + examined/rejected counters + duration histogram; zero-examined is a FAILURE."""
    category = "ok" if outcome == "ok" else reason_category(outcome)

    def _emit() -> None:
        meter = get_meter()
        meter.create_counter("marketplace.scan.total").add(
            1, _labels(outcome=category, build_mode=build_mode)
        )
        meter.create_counter("marketplace.scan.bundles_examined").add(examined)
        meter.create_counter("marketplace.scan.bundles_rejected").add(rejected)
        meter.create_histogram("marketplace.scan.duration_s").record(duration_s)
        if outcome != "ok":
            meter.create_counter("marketplace.scan.failures").add(1, {"category": category})

    _safe("scan", _emit)


def record_build(*, duration_s: float, outcome: str) -> None:
    """Sandbox build duration histogram + outcome counter (bounded outcome category)."""
    category = "ok" if outcome == "ok" else reason_category(outcome)

    def _emit() -> None:
        meter = get_meter()
        meter.create_histogram("marketplace.sandbox.build.duration_s").record(
            duration_s, {"outcome": category}
        )
        meter.create_counter("marketplace.sandbox.build.total").add(1, {"outcome": category})

    _safe("build", _emit)


def adjust_pending_updates(delta: int) -> None:
    """Pending-update gauge: +1 when an update awaits approval, -1 when approved/cleared."""
    _safe(
        "pending_update",
        lambda: get_meter().create_up_down_counter("marketplace.source.pending_updates").add(delta),
    )


def record_approval(*, outcome: str) -> None:
    """Approve/deny counter (`signed`, `denied`, `update_approved`)."""
    _safe(
        "approve",
        lambda: (
            get_meter().create_counter("marketplace.approve.total").add(1, {"outcome": outcome})
        ),
    )


def record_update_check(outcome: str) -> None:
    """Auto-update check counter (`outcome` is the bounded `UpdateOutcome` value)."""
    _safe(
        "update_check",
        lambda: (
            get_meter()
            .create_counter("marketplace.source.update_check.total")
            .add(1, {"outcome": outcome})
        ),
    )


def record_ingest(outcome: str) -> None:
    """Ingest outcome counter; free-text reasons fold to a bounded category."""
    label = outcome if outcome in {"needs_approval", "conflict"} else reason_category(outcome)
    _safe(
        "ingest",
        lambda: get_meter().create_counter("marketplace.ingest.total").add(1, {"outcome": label}),
    )


@asynccontextmanager
async def source_span(name: str, source_public_id: uuid.UUID | str) -> AsyncIterator[Any]:
    """Span carrying `source_public_id` ONLY; the body's exceptions still propagate.

    A tracer/exporter failure entering or leaving the span is logged (type only) and swallowed --
    it must never fail the request the span wraps.
    """
    cm = bundle_span(name, source_public_id=str(source_public_id))
    try:
        span = await cm.__aenter__()
    except Exception as exc:
        logger.warning("marketplace span start failed", extra={"error": type(exc).__name__})
        yield None
        return
    try:
        yield span
    except BaseException as body_exc:
        try:
            await cm.__aexit__(type(body_exc), body_exc, body_exc.__traceback__)
        except Exception as exc:  # noqa: BLE001 - re-raises the body's own exception, not ours
            if exc is not body_exc:
                logger.warning("marketplace span end failed", extra={"error": type(exc).__name__})
        raise
    try:
        await cm.__aexit__(None, None, None)
    except Exception as exc:
        logger.warning("marketplace span end failed", extra={"error": type(exc).__name__})
