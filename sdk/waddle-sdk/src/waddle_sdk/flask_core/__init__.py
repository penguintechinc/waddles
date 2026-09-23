"""Compatibility shim package -- same import names bundles use today.

``from flask_core import get_bundle_context, get_bundle_dal``, re-exported
from ``waddle_sdk.flask_core`` so an unchanged bundle's ``import flask_core``
line resolves once the SDK is installed in its place at build time (spec
Sec4.12).
"""

from __future__ import annotations

from waddle_sdk.flask_core.bundle_runtime import (
    BundleContext,
    BundleRuntimeError,
    bundle_context,
    get_bundle_context,
    get_bundle_dal,
    raw_sql_rows,
    raw_sql_write,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

__all__ = [
    "BundleContext",
    "BundleRuntimeError",
    "PlatformEvent",
    "StageEnvelope",
    "bundle_context",
    "get_bundle_context",
    "get_bundle_dal",
    "raw_sql_rows",
    "raw_sql_write",
    "reset_bundle_dal_for_tests",
    "set_bundle_dal",
]
