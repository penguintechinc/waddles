using WaddleSdk.Chat;
using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;
using WaddleBundleSuperpenguinRoll;
using Xunit;

namespace WaddleBundleSuperpenguinRoll.Tests;

// Minimal local fakes -- deliberately not shared with sdk/waddle-sdk-cs/tests
// (a separate xUnit test project; see this project's own csproj header
// comment for why it cannot reference the wasi-wasm bundle project either).
file sealed class FakeKvClient : IKvClient
{
    private readonly Dictionary<string, byte[]> _store = [];

    public byte[]? Get(string key) => _store.TryGetValue(key, out var v) ? v : null;
    public void Set(string key, byte[] value, uint ttlSeconds) => _store[key] = value;
    public void Delete(string key) => _store.Remove(key);
    public long Increment(string key, long delta, uint ttlSeconds) => 0;
}

file sealed class DenyingKvClient : IKvClient
{
    private static WaddleKvException Denied() => new(KvErrorKind.Denied, "kv capability denied by host");
    public byte[]? Get(string key) => throw Denied();
    public void Set(string key, byte[] value, uint ttlSeconds) => throw Denied();
    public void Delete(string key) => throw Denied();
    public long Increment(string key, long delta, uint ttlSeconds) => throw Denied();
}

file sealed class FakeRelayClient : IRelayClient
{
    public List<(string Provider, string MessageJson)> Pushes { get; } = [];
    public void Push(string provider, string messageJson) => Pushes.Add((provider, messageJson));
}

file sealed class FakeHost(IKvClient kv, IRelayClient relay, bool flagsEnabled = true) : IWaddleHost
{
    public BundleContextInfo Context { get; } = new("tenant-1", null, "waddles.integrations.superpenguin.roll", "waddles.integrations.superpenguin", "1.0.0", "msg-1", "{}");
    public IKvClient Kv { get; } = kv;
    public IDbClient Db => throw new NotSupportedException("!roll needs no db -- see RollLogic's own doc comment");
    public IRelayClient Relay { get; } = relay;
    public IHttpClient Http => throw new NotSupportedException("!roll needs no http");
    public IFlagsClient Flags { get; } = new NoopFlags(flagsEnabled);
    public ILogClient Log { get; } = new NoopLog();
    public IClockClient Clock { get; } = new FixedClock();

    // `flagsEnabled` defaults to true so every pre-existing behavioral test
    // above exercises the shipped-and-turned-on state without touching every
    // call site -- `RollLogicTransformTests` below adds the dedicated
    // flag-disabled coverage.
    private sealed class NoopFlags(bool enabled) : IFlagsClient
    {
        public bool Enabled(string key, bool defaultValue) => enabled;
        public string Tier() => "free";
    }

    private sealed class NoopLog : ILogClient
    {
        public void Write(WaddleLogLevel level, string message, string fieldsJson) { }
    }

    private sealed class FixedClock : IClockClient
    {
        public ulong NowMillis() => 0;
        public string NowRfc3339() => "2026-09-28T00:00:00.000Z";
        public ulong MonotonicNanos() => 0;
    }
}

public class RollLogicBuildResultTextTests
{
    [Theory]
    [InlineData(1, 40, "Snake eyes")]
    [InlineData(2, 160, "Hard four")]
    [InlineData(3, 360, "Hard six")]
    [InlineData(4, 640, "Hard eight")]
    [InlineData(5, 1000, "Hard ten")]
    public void doubles_below_six_announce_the_matching_prize_name_and_amount(int dice, int amount, string prizeName)
    {
        var text = RollLogic.BuildResultText(dice, dice, "alice");
        Assert.Contains($"alice rolls a [{dice}] and [{dice}].", text);
        Assert.Contains($"{prizeName} for {amount} points!", text);
    }

    [Fact]
    public void double_sixes_are_boxcars_with_the_original_exclamatory_phrasing()
    {
        var text = RollLogic.BuildResultText(6, 6, "alice");
        Assert.Contains("Boxcars to the max!!! 1440 points!", text);
    }

    [Fact]
    public void a_non_double_produces_a_losing_message_and_no_prize_text()
    {
        var text = RollLogic.BuildResultText(2, 5, "bob");
        Assert.Contains("bob rolls a [2] and [5].", text);
        Assert.DoesNotContain("points!", text);
    }

    [Fact]
    public void result_text_is_never_empty_for_any_dice_combination()
    {
        for (var d1 = 1; d1 <= 6; d1++)
        {
            for (var d2 = 1; d2 <= 6; d2++)
            {
                Assert.False(string.IsNullOrWhiteSpace(RollLogic.BuildResultText(d1, d2, "x")));
            }
        }
    }
}

public class RollLogicTransformTests
{
    private static PlatformEventInfo ChatEvent(string text, string? actor = "user-1", string? channelId = "42") =>
        new("twitch", "chat.message", actor,
            System.Text.Json.JsonSerializer.Serialize(new ChatMessagePayload(text, channelId), WaddleSdkJsonContext.Default.ChatMessagePayload),
            "2026-09-28T00:00:00.000Z");

    [Theory]
    [InlineData("!roll")]
    [InlineData("!dice")]
    public void both_command_aliases_produce_a_reply(string commandText)
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        var result = new RollLogic().Run(ChatEvent(commandText), host);

        Assert.NotNull(result);
        var reply = result!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload);
        Assert.NotNull(reply);
        Assert.Equal("42", reply!.ChannelId);
    }

    [Fact]
    public void non_matching_text_produces_no_reply()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        Assert.Null(new RollLogic().Run(ChatEvent("hello"), host));
    }

    [Fact]
    public void a_second_roll_within_the_cooldown_window_is_silently_dropped()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        var logic = new RollLogic();

        Assert.NotNull(logic.Run(ChatEvent("!roll"), host));
        Assert.Null(logic.Run(ChatEvent("!roll"), host));
    }

    [Fact]
    public void roll_and_dice_cooldowns_track_independently_like_the_original()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        var logic = new RollLogic();

        Assert.NotNull(logic.Run(ChatEvent("!roll"), host));
        // Original: `RegisterDefaultCommand` is called once per command name
        // with its own cooldown -- `!dice` must not be blocked by `!roll`'s.
        Assert.NotNull(logic.Run(ChatEvent("!dice"), host));
    }

    [Fact]
    public void different_users_have_independent_cooldowns()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        var logic = new RollLogic();

        Assert.NotNull(logic.Run(ChatEvent("!roll", actor: "user-1"), host));
        Assert.NotNull(logic.Run(ChatEvent("!roll", actor: "user-2"), host));
    }

    [Fact]
    public void still_produces_a_reply_when_kv_is_denied()
    {
        // Regression: as of 2026-09-28 `kv` is hardcoded to deny every call
        // host-side. `!roll` must keep working (cooldown just goes
        // unenforced) rather than failing the whole command.
        var host = new FakeHost(new DenyingKvClient(), new FakeRelayClient());
        Assert.NotNull(new RollLogic().Run(ChatEvent("!roll"), host));
    }

    [Theory]
    [InlineData("!roll")]
    [InlineData("!dice")]
    public void no_reply_when_the_posthog_flag_is_disabled(string commandText)
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient(), flagsEnabled: false);
        Assert.Null(new RollLogic().Run(ChatEvent(commandText), host));
    }

    [Fact]
    public void a_disabled_flag_never_consumes_the_cooldown_slot()
    {
        // The flag check must run before CooldownGuard.TryAcquire -- a
        // disabled flag should not burn the user's cooldown window, so a
        // later flag-enabled call still gets a reply.
        var kv = new FakeKvClient();
        var disabledHost = new FakeHost(kv, new FakeRelayClient(), flagsEnabled: false);
        var enabledHost = new FakeHost(kv, new FakeRelayClient(), flagsEnabled: true);
        var logic = new RollLogic();

        Assert.Null(logic.Run(ChatEvent("!roll"), disabledHost));
        Assert.NotNull(logic.Run(ChatEvent("!roll"), enabledHost));
    }
}

public class RollDispatchTests
{
    private static StageEnvelopeInfo EnvelopeWithReply(string platform, string text, string? channelId)
    {
        var replyJson = System.Text.Json.JsonSerializer.Serialize(new ChatReplyPayload(text, channelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
        return new StageEnvelopeInfo(
            "tenant-1", null, "waddles.integrations.superpenguin.roll", "action",
            new PlatformEventInfo(platform, "chat.message", "user-1", replyJson, "2026-09-28T00:00:00.000Z"),
            "2026-09-28T00:00:00.000Z", null, null);
    }

    [Theory]
    [InlineData("twitch")]
    [InlineData("discord")]
    public void relays_to_the_events_own_origin_platform_never_a_hardcoded_one(string platform)
    {
        var relay = new FakeRelayClient();
        var host = new FakeHost(new FakeKvClient(), relay);

        var result = new RollDispatch().Run(EnvelopeWithReply(platform, "alice rolls...", "42"), "{}", host);

        Assert.True(result.Ok);
        Assert.Equal(platform, result.Detail);
        Assert.Single(relay.Pushes);
        Assert.Equal(platform, relay.Pushes[0].Provider);
    }

    [Fact]
    public void throws_a_fatal_error_when_channel_id_is_missing()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        var ex = Assert.Throws<WaddleTransportException>(
            () => new RollDispatch().Run(EnvelopeWithReply("twitch", "text", null), "{}", host));

        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.False(ex.Error.Retryable);
    }

    [Fact]
    public void throws_a_fatal_error_for_a_non_reply_payload()
    {
        var host = new FakeHost(new FakeKvClient(), new FakeRelayClient());
        // A JSON array (not an object) fails to deserialize as `ChatReplyPayload`
        // -- `{}` would NOT reproduce this: System.Text.Json's source-gen
        // deserialization leaves a missing property at its type's default
        // (null for `string`) rather than throwing, so an empty object still
        // "successfully" parses into a reply with null Text/ChannelId and hits
        // the MISSING_CHANNEL path instead (covered by the test above).
        var envelope = new StageEnvelopeInfo(
            "tenant-1", null, "waddles.integrations.superpenguin.roll", "action",
            new PlatformEventInfo("twitch", "chat.message", "user-1", "[]", "2026-09-28T00:00:00.000Z"),
            "2026-09-28T00:00:00.000Z", null, null);

        var ex = Assert.Throws<WaddleTransportException>(() => new RollDispatch().Run(envelope, "{}", host));
        Assert.Equal("BAD_PAYLOAD", ex.Error.Code);
    }
}
