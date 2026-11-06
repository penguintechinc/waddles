"""Beta-trio acceptance (reputation leg): connector-path events feed ordinary-activity accrual.

The fixture `core/connector_e2e/tests/fixtures/accrual_contract.json` is the
decrypted event the REAL Twitch/YouTube platform connector service bundles
publish to the spine; `core/connector_e2e/tests/connector_acceptance.rs` asserts
the live path reproduces it byte-for-byte. This test pushes the same event
through the real `ProcessRunner` and asserts the reputation accrual hook fires
once per platform with the right scope and user key (the reputation service call
itself is stubbed -- `test_activity_accrual.py` covers its HTTP/cooldown logic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from flask_core import PlatformEvent, StageEnvelope
from flask_core.stage_runner import BundlePoller
from flask_core.stream_pipeline import bundle_stream_key

from runner import ProcessRunner
from services.activity_accrual import ActivityAccrualResult
from services.community_resolver import ResolvedCommunity

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "connector_e2e"
    / "tests"
    / "fixtures"
    / "accrual_contract.json"
)
TENANT = "acme-corp"
APP_ID = "waddles.core.demo.echo"
COMMUNITY = "42"


def _contract() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text())
    assert set(data) == {"twitch", "youtube"}, "fixture must cover both ON platforms"
    return data


@pytest.mark.parametrize("platform", ["twitch", "youtube"])
async def test_connector_message_event_drives_reputation_accrual(
    platform: str,
    redis_client: Any,
    http_client_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner as runner_module

    calls: list[dict[str, Any]] = []

    async def _accrue(**kwargs: Any) -> ActivityAccrualResult:
        calls.append(kwargs)
        return ActivityAccrualResult(applied=True, event_type=kwargs["event_type"], reason="ok")

    async def _resolve(**kwargs: Any) -> ResolvedCommunity:
        return ResolvedCommunity(community_id=int(COMMUNITY), source="demo_shim")

    monkeypatch.setattr(runner_module, "accrue_activity", _accrue)
    monkeypatch.setattr(runner_module, "resolve_community", _resolve)

    event = _contract()[platform]
    assert event["payload"]["text"], "contract event has message text"
    assert event["event_type"] == "message"

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        bundle = {
            "appId": APP_ID,
            "communityId": int(COMMUNITY),
            "entrypoint": "builtin_handlers.echo_process:transform",
            "spec": {},
            "config": {},
        }
        return httpx.Response(
            200, json={"success": True, "stage": "process", "bundles": [bundle], "meta": {}}
        )

    poller = BundlePoller(
        http_client_factory(handler),
        "http://hub-api/api/v1/distribution/bundles",
        stage="process",
        jwt_provider=lambda: "t",
    )
    runner = ProcessRunner(poller=poller, redis_client=redis_client, tenant_slug=TENANT)
    envelope = StageEnvelope(
        tenant=TENANT,
        community=COMMUNITY,
        app_id=APP_ID,
        stage="process",
        event=PlatformEvent(
            platform=event["platform"],
            event_type=event["event_type"],
            actor=event["actor"],
            payload=event["payload"],
            occurred_at="2026-11-06T00:00:00+00:00",
        ),
        ts="2026-11-06T00:00:00+00:00",
    )
    key = bundle_stream_key(TENANT, COMMUNITY, APP_ID, "process")
    await redis_client.lpush(key, json.dumps(envelope.to_dict()))

    await runner.run_once()

    assert len(calls) == 1, f"{platform}: accrual fired {len(calls)} times, want exactly 1"
    call = calls[0]
    assert call["event_type"] == "chat_message"
    assert call["tenant"] == TENANT
    assert call["community"] == COMMUNITY
    assert call["platform"] == platform
    # The accrual keys on the platform-native author id carried by the connector
    # event (the pre-existing runner convention), not the tokenized actor UUID.
    assert call["platform_user_id"] == event["payload"]["author_id"]
