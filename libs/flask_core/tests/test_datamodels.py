"""Tests for flask_core.datamodels -- slotted/frozen contracts, defaults, tier + rate-limit logic."""

import dataclasses
from datetime import datetime, timedelta

import pytest

from flask_core import datamodels as dm
from flask_core.datamodels import (
    Activity,
    APIKeyInfo,
    BrowserSourceMessage,
    CommandRequest,
    CommandResult,
    CoordinationClaim,
    EventPayload,
    HealthCheckResult,
    IdentityPayload,
    MessageType,
    MetricsSnapshot,
    ModuleResponse,
    OAuthToken,
    Platform,
    RateLimitInfo,
    ReputationAdjustment,
    ReputationEvent,
    ReputationEventType,
    ReputationInfo,
    ReputationTier,
    ReputationWeights,
    ResponseAction,
)

NOW = datetime(2026, 1, 1, 12, 0, 0)


def _req(**kw):
    base = dict(
        entity_id="e", user_id="u", message="!help", message_type=MessageType.SLASH_COMMAND,
        platform=Platform.DISCORD,
    )
    base.update(kw)
    return CommandRequest(**base)


class TestEnums:
    def test_string_enums_compare_to_wire_values(self):
        assert MessageType.CHAT_MESSAGE == "chatMessage"
        assert Platform("twitch") is Platform.TWITCH
        assert ResponseAction.TICKER.value == "ticker"
        assert ReputationEventType.AUTO_BAN == "auto_ban"

    def test_unknown_platform_value_rejected(self):
        with pytest.raises(ValueError):
            Platform("myspace")

    def test_wire_values_are_unique(self):
        for enum_cls in (MessageType, Platform, ResponseAction, ReputationTier, ReputationEventType):
            values = [m.value for m in enum_cls]
            assert len(values) == len(set(values))


class TestFrozenModels:
    @pytest.mark.parametrize(
        "obj,attr",
        [
            (_req(), "message"),
            (IdentityPayload(Platform.TWITCH, "1", "n", "e"), "entity_id"),
            (Activity("e", "u", "n", MessageType.FOLLOW, Platform.TWITCH, 5), "amount"),
            (OAuthToken(Platform.TWITCH, "tok"), "access_token"),
            (APIKeyInfo("k", "n", "u", "h", ["r"], True, NOW), "is_active"),
            (CoordinationClaim("e", Platform.TWITCH, None, None, "c", NOW, NOW), "claimed_by"),
            (ReputationInfo(600, ReputationTier.FAIR, "Fair"), "score"),
            (ReputationWeights(community_id=1), "ban"),
        ],
    )
    def test_cannot_mutate(self, obj, attr):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")

    @pytest.mark.parametrize(
        "cls",
        [CommandRequest, CommandResult, IdentityPayload, Activity, EventPayload, ModuleResponse,
         OAuthToken, BrowserSourceMessage, APIKeyInfo, RateLimitInfo, CoordinationClaim,
         HealthCheckResult, MetricsSnapshot, ReputationInfo, ReputationEvent,
         ReputationAdjustment, ReputationWeights],
    )
    def test_slots_forbid_stray_attributes(self, cls):
        assert hasattr(cls, "__slots__")
        fields = {f.name for f in dataclasses.fields(cls)}
        assert fields <= set(cls.__slots__)


class TestDefaults:
    def test_mutable_defaults_not_shared(self):
        a, b = _req(), _req()
        assert a.metadata == {} and a.metadata is not b.metadata
        r1 = CommandResult("x", None, True)
        r2 = CommandResult("y", None, True)
        r1.response_data["k"] = 1
        assert r2.response_data == {}
        h1 = HealthCheckResult("m", "healthy", "1", 1, True, True)
        h2 = HealthCheckResult("m", "healthy", "1", 1, True, True)
        h1.errors.append("boom")
        assert h2.errors == [] and h2.details == {}

    def test_timestamps_default_to_now(self):
        before = datetime.utcnow()
        r = CommandResult("x", None, True)
        assert before <= r.timestamp <= datetime.utcnow()

    def test_command_request_optional_fields(self):
        r = _req()
        assert (r.session_id, r.username, r.display_name, r.channel_id, r.server_id) == (None,) * 5

    def test_module_response_and_event_defaults(self):
        m = ModuleResponse("x", "s", "mod", True, ResponseAction.CHAT)
        assert m.ticker_duration == 10 and m.processing_time_ms == 0
        e = EventPayload("id", MessageType.RAID, Platform.TWITCH, "e", "u", "n")
        assert e.processed is False and e.error_message is None

    def test_oauth_and_identity_defaults(self):
        t = OAuthToken(Platform.DISCORD, "a")
        assert t.token_type == "Bearer" and t.scope == [] and t.refresh_token is None
        i = IdentityPayload(Platform.TWITCH, "1", "n", "e")
        assert i.is_verified is False and i.roles == [] and i.labels == []

    def test_browser_source_defaults(self):
        m = BrowserSourceMessage("ticker", "display", {"t": 1})
        assert (m.duration, m.priority) == (10, 0)

    def test_claim_defaults(self):
        c = CoordinationClaim("e", Platform.TWITCH, "s", "ch", "c1", NOW, NOW)
        assert (c.heartbeat_interval, c.is_live, c.viewer_count, c.priority) == (300, False, 0, 0)

    def test_metrics_snapshot(self):
        m = MetricsSnapshot("m", 10, 9, 1, 1.5, 2.5, 3.5, 4, 0.9)
        assert m.requests_failed == 1 and isinstance(m.timestamp, datetime)

    def test_reputation_adjustment_event(self):
        a = ReputationAdjustment(True, 600, 610, 10.0)
        assert a.event_id is None and a.error is None
        ev = ReputationEvent(1, 2, 3, ReputationEventType.FOLLOW, 1.0, 600, 601, Platform.TWITCH, "p")
        assert ev.metadata == {} and ev.reason is None


class TestReputationInfoTier:
    @pytest.mark.parametrize(
        "score,tier,label",
        [
            (850, ReputationTier.EXCEPTIONAL, "Exceptional"),
            (800, ReputationTier.EXCEPTIONAL, "Exceptional"),
            (799, ReputationTier.VERY_GOOD, "Very Good"),
            (740, ReputationTier.VERY_GOOD, "Very Good"),
            (739, ReputationTier.GOOD, "Good"),
            (670, ReputationTier.GOOD, "Good"),
            (669, ReputationTier.FAIR, "Fair"),
            (580, ReputationTier.FAIR, "Fair"),
            (579, ReputationTier.POOR, "Poor"),
            (300, ReputationTier.POOR, "Poor"),
        ],
    )
    def test_get_tier_boundaries(self, score, tier, label):
        assert ReputationInfo.get_tier(score) == (tier, label)


class TestReputationWeights:
    def test_defaults_are_consistent(self):
        w = ReputationWeights(community_id=5)
        assert w.starting_score == 600
        assert w.min_score < w.auto_ban_threshold < w.starting_score < w.max_score
        assert w.warn > w.timeout > w.kick > w.ban  # harsher actions weigh more negative
        assert w.is_premium is False and w.auto_ban_enabled is False

    def test_custom_override(self):
        w = ReputationWeights(community_id=5, is_premium=True, ban=-500.0)
        assert w.is_premium and w.ban == -500.0


class TestRateLimitInfo:
    def test_is_exceeded_threshold(self):
        info = RateLimitInfo("k", NOW, request_count=4, limit=5, window_seconds=60)
        assert info.is_exceeded() is False
        info.request_count = 5
        assert info.is_exceeded() is True

    def test_reset_when_window_expired(self):
        info = RateLimitInfo("k", NOW, request_count=9, limit=5, window_seconds=60)
        later = NOW + timedelta(seconds=61)
        info.reset_if_expired(now=later)
        assert info.request_count == 0 and info.window_start == later

    def test_no_reset_inside_window(self):
        info = RateLimitInfo("k", NOW, request_count=3, limit=5, window_seconds=60)
        info.reset_if_expired(now=NOW + timedelta(seconds=59))
        assert info.request_count == 3 and info.window_start == NOW

    def test_reset_exactly_at_boundary(self):
        info = RateLimitInfo("k", NOW, request_count=3, limit=5, window_seconds=60)
        info.reset_if_expired(now=NOW + timedelta(seconds=60))
        assert info.request_count == 0

    def test_default_now_is_used(self):
        stale = datetime.utcnow() - timedelta(hours=1)
        info = RateLimitInfo("k", stale, request_count=3, limit=5, window_seconds=60)
        info.reset_if_expired()
        assert info.request_count == 0 and info.window_start > stale


def test_type_aliases_exist():
    assert dm.CommandHandler.__name__ == "CommandHandler"
    assert dm.AsyncEventHandler.__name__ == "AsyncEventHandler"
