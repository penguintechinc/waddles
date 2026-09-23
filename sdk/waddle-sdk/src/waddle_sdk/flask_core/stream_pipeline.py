"""``PlatformEvent``/``StageEnvelope`` dataclasses -- same names, same fields.

Matches the real ``libs/flask_core/flask_core/stream_pipeline.py`` field-for-
field (``platform``, ``event_type``, ``actor``, ``payload: dict``,
``occurred_at``), so an unmodified bundle constructing
``PlatformEvent(platform=..., ...)`` works unchanged.

``from_wit_record``/``to_wit_record`` convert to/from the generated WIT
binding types (``wit_world.imports.types.PlatformEvent``/``StageEnvelope``,
confirmed via ``componentize-py bindings`` against the committed
``wit/waddle-bundle/stage.wit``), where the WIT record carries the payload as
canonical JSON text (``payload_json``) rather than a live dict -- parsed/
serialized here, once, at the SDK boundary, so bundle code always sees a
plain dict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PlatformEvent:
    """A normalized inbound platform event -- matches real ``flask_core.stream_pipeline``."""

    platform: str
    event_type: str
    actor: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        return {
            "platform": self.platform,
            "event_type": self.event_type,
            "actor": self.actor,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformEvent:
        """Construct from a plain dict (the inverse of :meth:`to_dict`)."""
        return cls(
            platform=data["platform"],
            event_type=data["event_type"],
            actor=data.get("actor"),
            payload=data.get("payload", {}),
            occurred_at=data.get("occurred_at", ""),
        )

    @classmethod
    def from_wit_record(cls, record: Any) -> PlatformEvent:
        """Construct from the generated WIT binding's ``types.PlatformEvent`` record.

        ``payload_json`` is canonical JSON text and is parsed here, once, at
        the SDK boundary.
        """
        return cls(
            platform=record.platform,
            event_type=record.event_type,
            actor=record.actor,
            payload=json.loads(record.payload_json) if record.payload_json else {},
            occurred_at=record.occurred_at,
        )

    def to_wit_record(self, types_mod: Any) -> Any:
        """Build the generated WIT binding's ``types.PlatformEvent`` record from this event."""
        return types_mod.PlatformEvent(
            platform=self.platform,
            event_type=self.event_type,
            actor=self.actor,
            payload_json=json.dumps(self.payload),
            occurred_at=self.occurred_at,
        )


@dataclass(slots=True)
class StageEnvelope:
    """One pipeline queue message -- matches the WIT ``types.stage-envelope`` record."""

    tenant: str
    community: str | None
    app_id: str
    stage: str
    event: PlatformEvent
    ts: str
    target_app_id: str | None = None
    trace_context: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        d: dict[str, Any] = {
            "tenant": self.tenant,
            "community": self.community,
            "app_id": self.app_id,
            "stage": self.stage,
            "event": self.event.to_dict(),
            "ts": self.ts,
        }
        if self.target_app_id is not None:
            d["target_app_id"] = self.target_app_id
        if self.trace_context is not None:
            d["trace_context"] = self.trace_context
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageEnvelope:
        """Construct from a plain dict (the inverse of :meth:`to_dict`)."""
        return cls(
            tenant=data["tenant"],
            community=data.get("community"),
            app_id=data["app_id"],
            stage=data["stage"],
            event=PlatformEvent.from_dict(data["event"]),
            ts=data["ts"],
            target_app_id=data.get("target_app_id"),
            trace_context=data.get("trace_context"),
        )

    @classmethod
    def from_wit_record(cls, record: Any) -> StageEnvelope:
        """Construct from the generated WIT binding's ``types.StageEnvelope`` record."""
        return cls(
            tenant=record.tenant,
            community=record.community,
            app_id=record.app_id,
            stage=record.stage,
            event=PlatformEvent.from_wit_record(record.event),
            ts=record.ts,
            target_app_id=record.target_app_id,
            trace_context=record.trace_context,
        )
