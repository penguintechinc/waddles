"""The YouTube adapter in isolation: validate / resolve / refresh / discover, metadata contract.

Only the Google socket is faked (``fake_google.py``); the host allowlist, the real SSRF guard,
request construction, size bound and error mapping all run for real.
"""

from __future__ import annotations

import socket
from datetime import timedelta
from typing import Any

import httpx
import pytest

from services.provider_connections.adapter import Account, Credentials, validate_metadata
from services.provider_connections.errors import (
    CredentialsRejected,
    InsufficientScopes,
    InvalidConnectionInput,
    ProviderQuotaExceeded,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    ResourceUnresolvable,
)
from services.provider_connections.youtube import (
    ALLOWED_HOSTS,
    SCOPE_FORCE_SSL,
    YOUTUBE_SPEC,
    AppCredentialsUnavailable,
    YoutubeAdapter,
    guard_google_url,
)
from tests.provider_connections.conftest import FakeClock
from tests.provider_connections.fake_google import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    OTHER_CLIENT_ID,
    READONLY,
    FakeGoogle,
    broadcast,
)
from tests.provider_connections.youtube_fixtures import YT_CHANNEL, YT_CHAT, YT_OTHER_CHANNEL

FACTS = {"tenant_id": 1, "client_id": APP_CLIENT_ID, "channel_id": YT_CHANNEL.channel_id}


async def _creds(tenant_id: int) -> tuple[str, str]:
    return APP_CLIENT_ID, APP_CLIENT_SECRET


def _adapter(google: FakeGoogle, clock: FakeClock, **kwargs: Any) -> YoutubeAdapter:
    kwargs.setdefault("app_credentials", _creds)
    return YoutubeAdapter(transport=google.transport, clock=clock, **kwargs)


class TestSpecContract:
    def test_matches_the_data_plane_binding_contract(self) -> None:
        spec = YOUTUBE_SPEC
        assert spec.provider == "youtube" and spec.platform == "youtube"
        assert spec.refreshable is True
        assert spec.install_kind == "oauth_connect"
        assert spec.required_scopes == (SCOPE_FORCE_SSL,)
        (key,) = spec.binding_metadata
        assert (key.wire, key.stored, key.required) == ("live_chat_id", "live_chat_id", True)
        assert "readonly" not in " ".join(spec.required_scopes)

    def test_binding_metadata_is_never_secret(self) -> None:
        secretish = {"access_token", "refresh_token", "client_secret", "client_id", "tenant_id"}
        assert not secretish & (spec_keys := YOUTUBE_SPEC.runtime_metadata_keys)
        assert not secretish & YOUTUBE_SPEC.public_metadata_keys, spec_keys

    def test_capabilities_follow_the_force_ssl_scope(self) -> None:
        adapter = YoutubeAdapter()
        assert adapter.granted_capabilities([SCOPE_FORCE_SSL]) == {"chat_ingest", "action_relay"}
        assert adapter.granted_capabilities([READONLY]) == frozenset()

    def test_pasted_bearer_prefix_is_stripped(self) -> None:
        out = YoutubeAdapter().normalize_credentials(Credentials("Bearer tok123", "r"))
        assert (out.access_token, out.refresh_token) == ("tok123", "r")


class TestLiveChatIdMetadata:
    @pytest.mark.parametrize("value", ["Cg0KC-live_ID", "a", "A" * 128, "0123456789", "a_b-C"])
    def test_accepts_the_executor_contract(self, value: str) -> None:
        assert validate_metadata(YOUTUBE_SPEC, {"live_chat_id": value}) == {"live_chat_id": value}

    @pytest.mark.parametrize(
        "value",
        [
            "A" * 129,
            "has space",
            "bad!",
            "a/b",
            "a.b",
            "../etc",
            "naïve",
            "a\nb",
            "a\x00b",
            "<script>",
            "a;b",
            "x" * 1000,
        ],
    )
    def test_rejects_everything_else_without_echoing_it(self, value: str) -> None:
        with pytest.raises(InvalidConnectionInput) as raised:
            validate_metadata(YOUTUBE_SPEC, {"live_chat_id": value})
        assert value not in raised.value.message

    def test_surrounding_whitespace_is_trimmed_not_embedded(self) -> None:
        assert validate_metadata(YOUTUBE_SPEC, {"live_chat_id": "  abc  "}) == {
            "live_chat_id": "abc"
        }

    def test_blank_means_unset(self) -> None:
        assert validate_metadata(YOUTUBE_SPEC, {"live_chat_id": "   "}) == {}
        assert validate_metadata(YOUTUBE_SPEC, {}) == {}

    @pytest.mark.parametrize("value", [123, True, ["a"], {"a": "b"}])
    def test_non_strings_are_refused(self, value: object) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_metadata(YOUTUBE_SPEC, {"live_chat_id": value})

    def test_unknown_keys_are_refused(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="unknown metadata field"):
            validate_metadata(YOUTUBE_SPEC, {"live_chat_id": "a", "channel_id": "UC1"})


class TestValidateAndResolve:
    async def test_validate_reports_client_scopes_and_lifetime(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        account = await _adapter(fake_google, clock).validate_credentials(Credentials(yt_tokens[0]))
        assert account.app_id == APP_CLIENT_ID
        assert account.scopes == (SCOPE_FORCE_SSL,)
        assert account.expires_at == clock.now + timedelta(seconds=3599)

    async def test_the_token_is_sent_in_a_form_body_never_a_url(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        seen: list[httpx.Request] = []

        def spy(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return fake_google._handle(request)

        adapter = YoutubeAdapter(transport=httpx.MockTransport(spy), clock=clock)
        await adapter.validate_credentials(Credentials(yt_tokens[0]))
        assert all(yt_tokens[0] not in str(request.url) for request in seen)
        assert fake_google.bodies[-1] == {"access_token": yt_tokens[0]}

    async def test_unknown_token_is_rejected(
        self, fake_google: FakeGoogle, clock: FakeClock
    ) -> None:
        with pytest.raises(CredentialsRejected):
            await _adapter(fake_google, clock).validate_credentials(Credentials("nope"))

    async def test_readonly_only_token_lacks_the_required_scope(
        self, fake_google: FakeGoogle, clock: FakeClock
    ) -> None:
        access, _ = fake_google.mint(channel=YT_CHANNEL, scopes=[READONLY])
        with pytest.raises(InsufficientScopes):
            await _adapter(fake_google, clock).validate_credentials(Credentials(access))

    async def test_tokeninfo_outage_is_unavailable_not_a_verdict(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        fake_google.down = True
        with pytest.raises(ProviderUnavailable):
            await _adapter(fake_google, clock).validate_credentials(Credentials(yt_tokens[0]))

    async def test_resolve_returns_the_channel(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        adapter = _adapter(fake_google, clock)
        credentials = Credentials(yt_tokens[0])
        account = await adapter.validate_credentials(credentials)
        resource = await adapter.resolve_resource(credentials, account, {})
        assert resource.resource_id == YT_CHANNEL.channel_id
        assert resource.facts["channel_id"] == YT_CHANNEL.channel_id
        assert resource.facts["client_id"] == APP_CLIENT_ID
        assert set(resource.facts) <= {"channel_id", "client_id", "channel_title"}

    async def test_an_account_without_a_channel_is_unresolvable(
        self, fake_google: FakeGoogle, clock: FakeClock
    ) -> None:
        access, _ = fake_google.mint(channel=None)
        adapter = _adapter(fake_google, clock)
        credentials = Credentials(access)
        account = await adapter.validate_credentials(credentials)
        with pytest.raises(ResourceUnresolvable):
            await adapter.resolve_resource(credentials, account, {})

    @pytest.mark.parametrize(
        ("status", "reason", "error"),
        [
            (403, "quotaExceeded", ProviderQuotaExceeded),
            (429, "rateLimitExceeded", ProviderQuotaExceeded),
            (403, "insufficientPermissions", InsufficientScopes),
            (403, "liveStreamingNotEnabled", ResourceUnresolvable),
            (500, None, ProviderUnavailable),
            (403, "somethingNew", ProviderUnavailable),
        ],
    )
    async def test_youtube_errors_map_to_typed_errors(
        self,
        fake_google: FakeGoogle,
        clock: FakeClock,
        yt_tokens: Any,
        status: int,
        reason: str | None,
        error: type[Exception],
    ) -> None:
        adapter = _adapter(fake_google, clock)
        credentials = Credentials(yt_tokens[0])
        account = await adapter.validate_credentials(credentials)
        fake_google.youtube_status, fake_google.youtube_reason = status, reason
        with pytest.raises(error):
            await adapter.resolve_resource(credentials, account, {})


class TestRefresh:
    async def test_refresh_returns_a_fresh_access_token_and_keeps_the_refresh_token(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        result = await _adapter(fake_google, clock).refresh(yt_tokens[1] or "", FACTS)
        assert result.access_token != yt_tokens[0]
        assert result.refresh_token is None  # Google omits it: the framework keeps the stored one
        assert result.expires_at is not None and result.expires_at > clock.now
        assert result.scopes == (SCOPE_FORCE_SSL,)
        assert fake_google.bodies[-1]["client_secret"] == APP_CLIENT_SECRET

    async def test_a_rotated_refresh_token_is_returned_for_storage(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        fake_google.rotate_refresh = True
        result = await _adapter(fake_google, clock).refresh(yt_tokens[1] or "", FACTS)
        assert result.refresh_token and result.refresh_token != yt_tokens[1]
        assert yt_tokens[1] not in fake_google.refresh_tokens
        again = await _adapter(fake_google, clock).refresh(result.refresh_token, FACTS)
        assert again.access_token

    async def test_invalid_grant_means_the_refresh_token_is_dead(
        self, fake_google: FakeGoogle, clock: FakeClock
    ) -> None:
        with pytest.raises(RefreshRejected):
            await _adapter(fake_google, clock).refresh("1//revoked", FACTS)

    @pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
    async def test_our_own_app_being_rejected_never_kills_the_token(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any, error: str
    ) -> None:
        fake_google.token_error = error
        with pytest.raises(RefreshNotConfigured):
            await _adapter(fake_google, clock).refresh(yt_tokens[1] or "", FACTS)

    @pytest.mark.parametrize("error", ["temporarily_unavailable", "", "invalid_request"])
    async def test_other_oauth_failures_are_transient(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any, error: str
    ) -> None:
        fake_google.token_error = error
        with pytest.raises(ProviderUnavailable):
            await _adapter(fake_google, clock).refresh(yt_tokens[1] or "", FACTS)

    async def test_a_token_from_another_google_app_is_refused_without_calling_google(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        with pytest.raises(RefreshNotConfigured):
            await _adapter(fake_google, clock).refresh(
                yt_tokens[1] or "", {**FACTS, "client_id": OTHER_CLIENT_ID}
            )
        assert fake_google.refresh_calls == 0

    @pytest.mark.parametrize("facts", [{}, {"tenant_id": "1"}, {"tenant_id": True}])
    async def test_a_connection_that_does_not_name_its_tenant_cannot_refresh(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any, facts: dict[str, Any]
    ) -> None:
        with pytest.raises(RefreshNotConfigured):
            await _adapter(fake_google, clock).refresh(yt_tokens[1] or "", facts)
        assert fake_google.refresh_calls == 0

    async def test_no_app_for_the_tenant_fails_loudly(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        async def missing(tenant_id: int) -> tuple[str, str]:
            raise AppCredentialsUnavailable("none")

        with pytest.raises(RefreshNotConfigured):
            await _adapter(fake_google, clock, app_credentials=missing).refresh(
                yt_tokens[1] or "", FACTS
            )

    async def test_the_default_resolver_never_guesses_a_global_app(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        with pytest.raises(RefreshNotConfigured):
            await YoutubeAdapter(transport=fake_google.transport, clock=clock).refresh(
                yt_tokens[1] or "", FACTS
            )

    async def test_outage_and_malformed_answers_are_unavailable(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        adapter = _adapter(fake_google, clock)
        fake_google.down = True
        with pytest.raises(ProviderUnavailable):
            await adapter.refresh(yt_tokens[1] or "", FACTS)
        fake_google.down = False

        def garbage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        bad = YoutubeAdapter(
            transport=httpx.MockTransport(garbage), app_credentials=_creds, clock=clock
        )
        with pytest.raises(ProviderUnavailable):
            await bad.refresh("r", FACTS)

        def no_token(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"expires_in": 5})

        empty = YoutubeAdapter(
            transport=httpx.MockTransport(no_token), app_credentials=_creds, clock=clock
        )
        with pytest.raises(ProviderUnavailable):
            await empty.refresh("r", FACTS)

        def server_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        down = YoutubeAdapter(
            transport=httpx.MockTransport(server_error), app_credentials=_creds, clock=clock
        )
        with pytest.raises(ProviderUnavailable):
            await down.refresh("r", FACTS)


class TestDiscover:
    async def _discover(self, google: FakeGoogle, clock: FakeClock, access: str) -> Any:
        return await _adapter(google, clock).discover_metadata(Credentials(access), FACTS)

    async def test_finds_the_live_chat_with_exactly_one_call(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        fake_google.broadcasts[YT_CHANNEL.channel_id] = [
            broadcast(YT_CHAT, channel_id=YT_CHANNEL.channel_id)
        ]
        found = await self._discover(fake_google, clock, yt_tokens[0])
        assert found == {"live_chat_id": YT_CHAT}
        assert [c for c in fake_google.calls if "liveBroadcasts" in c[1]] == [
            ("GET", "www.googleapis.com/youtube/v3/liveBroadcasts")
        ]
        assert len(fake_google.calls) == 1

    async def test_the_most_recent_live_broadcast_wins_and_live_beats_testing(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        cid = YT_CHANNEL.channel_id
        fake_google.broadcasts[cid] = [
            broadcast("old_live", channel_id=cid, started="2026-11-05T08:00:00Z"),
            broadcast("new_live", channel_id=cid, started="2026-11-05T11:00:00Z"),
            broadcast("a_test", channel_id=cid, status="testing", started="2026-11-05T12:00:00Z"),
        ]
        assert await self._discover(fake_google, clock, yt_tokens[0]) == {
            "live_chat_id": "new_live"
        }

    @pytest.mark.parametrize("status", ["complete", "created", "ready", "revoked"])
    async def test_non_active_broadcasts_do_not_count(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any, status: str
    ) -> None:
        cid = YT_CHANNEL.channel_id
        fake_google.broadcasts[cid] = [broadcast(YT_CHAT, channel_id=cid, status=status)]
        with pytest.raises(ResourceUnresolvable):
            await self._discover(fake_google, clock, yt_tokens[0])

    async def test_other_channels_malformed_and_missing_chat_ids_are_ignored(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        cid = YT_CHANNEL.channel_id
        fake_google.broadcasts[cid] = [
            broadcast("foreign", channel_id=YT_OTHER_CHANNEL.channel_id),
            broadcast("bad chat id!", channel_id=cid),
            broadcast(None, channel_id=cid),
            {"snippet": "nope", "status": {}},
            "garbage",  # type: ignore[list-item]
        ]
        with pytest.raises(ResourceUnresolvable):
            await self._discover(fake_google, clock, yt_tokens[0])

    async def test_quota_exhaustion_is_typed(
        self, fake_google: FakeGoogle, clock: FakeClock, yt_tokens: Any
    ) -> None:
        fake_google.youtube_status, fake_google.youtube_reason = 403, "quotaExceeded"
        with pytest.raises(ProviderQuotaExceeded):
            await self._discover(fake_google, clock, yt_tokens[0])


class TestNetworkGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/token",
            "https://oauth2.googleapis.com.evil.com/token",
            "http://169.254.169.254/latest/meta-data",
            "https://accounts.google.com@evil.com/x",
            "file:///etc/passwd",
        ],
    )
    async def test_only_the_declared_google_hosts_are_ever_contacted(self, url: str) -> None:
        with pytest.raises(ProviderUnavailable):
            await guard_google_url(url)

    def test_the_allowlist_is_exactly_the_three_google_hosts(self) -> None:
        assert ALLOWED_HOSTS == {
            "accounts.google.com",
            "oauth2.googleapis.com",
            "www.googleapis.com",
        }

    async def test_a_google_host_resolving_to_a_private_address_is_blocked(
        self,
        fake_google: FakeGoogle,
        clock: FakeClock,
        yt_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def private(host: str, *args: Any, **kwargs: Any) -> Any:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        monkeypatch.setattr("services.url_guard.socket.getaddrinfo", private)
        with pytest.raises(ProviderUnavailable):
            await _adapter(fake_google, clock).validate_credentials(Credentials(yt_tokens[0]))
        assert fake_google.calls == []  # the guard refused before any request

    async def test_redirects_are_never_followed(self, clock: FakeClock) -> None:
        def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

        adapter = YoutubeAdapter(transport=httpx.MockTransport(redirect), clock=clock)
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials("t"))

    async def test_an_oversized_answer_is_refused(self, clock: FakeClock) -> None:
        def huge(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{" + b" " * 600_000 + b"}")

        adapter = YoutubeAdapter(transport=httpx.MockTransport(huge), clock=clock)
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials("t"))

    @pytest.mark.parametrize("body", [b"[]", b"not json"])
    async def test_malformed_validate_bodies_are_unavailable(
        self, clock: FakeClock, body: bytes
    ) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        adapter = YoutubeAdapter(transport=httpx.MockTransport(answer), clock=clock)
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials("t"))

    async def test_expiry_arithmetic_tolerates_odd_expires_in_values(
        self, clock: FakeClock
    ) -> None:
        adapter = YoutubeAdapter(clock=clock)
        assert adapter._expiry("abc") is None
        assert adapter._expiry(True) is None
        assert adapter._expiry(0) is None
        assert adapter._expiry(None) is None
        assert adapter._expiry("120") == clock.now + timedelta(seconds=120)


class TestMalformedGoogleAnswers:
    @staticmethod
    def _adapter_for(handler: Any, clock: FakeClock) -> YoutubeAdapter:
        return YoutubeAdapter(
            transport=httpx.MockTransport(handler), app_credentials=_creds, clock=clock
        )

    async def test_tokeninfo_without_an_audience_is_unavailable(self, clock: FakeClock) -> None:
        adapter = self._adapter_for(
            lambda request: httpx.Response(200, json={"scope": SCOPE_FORCE_SSL}), clock
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials("t"))

    async def test_a_youtube_401_is_a_rejected_credential(
        self, fake_google: FakeGoogle, clock: FakeClock
    ) -> None:
        adapter = _adapter(fake_google, clock)
        account = await adapter.validate_credentials(Credentials(fake_google.mint(channel=None)[0]))
        with pytest.raises(CredentialsRejected):
            await adapter.resolve_resource(Credentials("unknown"), account, {})

    async def test_non_json_error_bodies_never_crash_the_mapping(self, clock: FakeClock) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"<html>oops</html>")

        adapter = self._adapter_for(broken, clock)
        account = Account("a", "", "client", (SCOPE_FORCE_SSL,), None)
        with pytest.raises(ProviderUnavailable):
            await adapter.resolve_resource(Credentials("t"), account, {})

        def broken_token(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=b"<html>oops</html>")

        with pytest.raises(ProviderUnavailable):
            await self._adapter_for(broken_token, clock).refresh("r", FACTS)
