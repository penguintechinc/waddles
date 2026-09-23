"""Waddles bundle SDK for Python.

Ships the same import names bundles use today
(``flask_core.get_bundle_dal``/``get_bundle_context``,
``flask_core.feature_flags.feature_enabled``, ``flask_core.stream_pipeline``'s
dataclasses) plus a ``penguin_dal``-compatible database facade, all
implemented over the ``waddle:bundle/stage@1.0.0`` WIT imports (spec Sec4.12,
Sec6.5; normative WIT source ``wit/waddle-bundle/stage.wit``).
"""

from __future__ import annotations

__all__: list[str] = []
