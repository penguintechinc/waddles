using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Media;
using WaddleSdk.Overlay;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;

namespace WaddleSdk.Tests;

public class ToolClientTests
{
    [Fact]
    public void points_view_round_trips_the_economy_service_contract()
    {
        var tools = new FakeToolClient();
        tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
            Encoding.UTF8.GetBytes("{\"user\":\"u1\",\"balance\":42,\"alias\":\"stars\"}");

        var view = EconomyServiceTools.PointsView(tools, "u1");

        Assert.Equal(new EconomyViewResult("u1", 42, "stars"), view);
        var call = Assert.Single(tools.Calls);
        Assert.Equal("{\"user\":\"u1\"}", call.Args);
        Assert.Null(call.Key);
    }

    [Fact]
    public void a_result_that_breaks_the_contract_is_loud_not_null()
    {
        var tools = new FakeToolClient();
        tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => Encoding.UTF8.GetBytes("{\"user\":\"u1\",\"balance\":\"many\"}");
        var ex = Assert.Throws<WaddleToolException>(() => EconomyServiceTools.PointsView(tools, "u1"));
        Assert.Equal(ToolErrorKind.InvalidArgs, ex.Kind);

        tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => Encoding.UTF8.GetBytes("null");
        Assert.Throws<WaddleToolException>(() => EconomyServiceTools.PointsView(tools, "u1"));
    }

    [Fact]
    public void a_missing_grant_surfaces_the_host_error()
    {
        var ex = Assert.Throws<WaddleToolException>(() => EconomyServiceTools.PointsView(new FakeToolClient(), "u1"));
        Assert.Equal(ToolErrorKind.NotGranted, ex.Kind);
    }

    [Theory]
    [InlineData(ToolErrorKind.NotGranted, "not_granted")]
    [InlineData(ToolErrorKind.Forbidden, "forbidden")]
    [InlineData(ToolErrorKind.Timeout, "timeout")]
    [InlineData(ToolErrorKind.DepthExceeded, "depth_exceeded")]
    [InlineData(ToolErrorKind.PoolExhausted, "pool_exhausted")]
    [InlineData(ToolErrorKind.TargetUnavailable, "target_unavailable")]
    [InlineData(ToolErrorKind.InvalidArgs, "invalid_args")]
    public void error_codes_match_the_python_sdk_vocabulary(ToolErrorKind kind, string code)
    {
        Assert.Equal(code, new WaddleToolException(kind, "x").Code);
    }

    [Fact]
    public void the_idempotency_key_is_forwarded_for_writes()
    {
        var tools = new FakeToolClient();
        tools.Handlers[("waddles.core.service.economy", "give")] = _ => Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"a\"}");
        tools.Invoke("waddles.core.service.economy", "give", Encoding.UTF8.GetBytes("{}"), "evt-9");
        Assert.Equal("evt-9", tools.Calls[0].Key);
    }
}

public class CapabilityRecordTests
{
    [Fact]
    public void user_placeholder_matches_the_egress_detokenizer_syntax()
    {
        Assert.Equal("{user:abc-123}", UserPlaceholder.For("abc-123"));
    }

    [Fact]
    public void economy_exception_carries_kind_and_amount()
    {
        var ex = new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "poor", 7);
        Assert.Equal(EconomyErrorKind.InsufficientFunds, ex.Kind);
        Assert.Equal(7UL, ex.Amount);
        Assert.Null(new WaddleEconomyException(EconomyErrorKind.Backend, "x").Amount);
    }

    [Fact]
    public void identity_media_and_ai_exceptions_carry_their_kinds()
    {
        Assert.Equal(IdentityErrorKind.NotLinked, new WaddleIdentityException(IdentityErrorKind.NotLinked, "x").Kind);
        Assert.Equal(MediaErrorKind.Quota, new WaddleMediaException(MediaErrorKind.Quota, "x").Kind);
        var ai = new Ai.WaddleAiException(Ai.AiErrorKind.TooManyTokens, "x", 512);
        Assert.Equal(512u, ai.Bound);
        Assert.Equal(new Ai.AiOptions(), new Ai.AiOptions(null, null, false));
    }

    [Fact]
    public void media_job_state_factories()
    {
        Assert.Equal(MediaJobStatus.Pending, MediaJobState.Pending().Status);
        Assert.Equal("h", MediaJobState.Done("h").Handle);
        var failed = MediaJobState.Failed(MediaErrorKind.ProviderRefused, "policy");
        Assert.Equal(MediaJobStatus.Failed, failed.Status);
        Assert.Equal(MediaErrorKind.ProviderRefused, failed.Error);
    }

    [Fact]
    public void fake_ledger_models_the_host_money_rules_the_contract_relies_on()
    {
        var econ = new FakeEconomyClient();
        econ.Seed("u", 100);
        Assert.Equal(90, econ.Wager("u", 10, 0));
        econ.BeginEvent("evt-2");
        Assert.Equal(130, econ.Wager("u", 10, 50));
        // Replaying the same event + ordinal returns the original balance and moves nothing.
        econ.BeginEvent("evt-2");
        Assert.Equal(130, econ.Wager("u", 10, 50));
        Assert.Equal(130, econ.Balance("u"));
        Assert.Throws<WaddleEconomyException>(() => econ.Wager("u", 1, 101));
        Assert.Throws<WaddleEconomyException>(() => econ.Wager("u", 0, 0));
        Assert.Throws<WaddleEconomyException>(() => econ.Wager("u", 501, 0));
        Assert.Throws<WaddleEconomyException>(() => econ.Wager("u", 400, 0));
    }

    [Fact]
    public void fake_economy_supports_the_full_surface()
    {
        var econ = new FakeEconomyClient();
        econ.Seed("a", 100);
        econ.Seed("b", 50);
        econ.Transfer("a", "b", 30);
        Assert.Equal(70, econ.Balance("a"));
        Assert.Equal(80, econ.Balance("b"));
        Assert.Equal(70, econ.MaxBet("a"));
        Assert.Equal("b", econ.Leaderboard(1)[0].User);
        Assert.Equal(75, econ.Credit("a", 5));
        econ.BatchCredit([new EconomyCredit("a", 5), new EconomyCredit("b", 5)]);
        econ.PayPot([new EconomyCredit("a", 1)]);
        Assert.Equal(80, econ.Debit("a", 1));
        Assert.Throws<WaddleEconomyException>(() => econ.ConditionalDebit("a", 79, 10));
    }
}

file sealed class NextFixtureProcess : WaddleProcessStageNext
{
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is("bal"))
        {
            return null;
        }

        var user = host.Identity.ResolveActor();
        var balance = host.Economy.Balance(user);
        return PlatformEventInfo.WithPayload(@event, new ChatReplyPayload($"{UserPlaceholder.For(user)}: {balance}", command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
    }
}

file sealed class NextFixtureAction : WaddleActionStageNext
{
    protected override TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHostNext host)
    {
        var push = host.Overlay.Push("widget", OverlaySurface.AlertBox, "{}");
        return TransportResultInfo.Success(detail: push.Ok ? "overlay" : "failed");
    }
}

public class NextStageBaseClassTests
{
    [Fact]
    public void process_stage_next_reaches_identity_and_economy()
    {
        var host = new FakeWaddleHostNext();
        host.EconomyFake.Seed(host.IdentityFake.Actor, 25);
        var ev = new PlatformEventInfo("twitch", "chat.message", "actor", "{\"text\":\"!bal\",\"channel_id\":\"c1\"}", "2026-11-07T00:00:00.000Z");

        var reply = new NextFixtureProcess().Run(ev, host);

        Assert.NotNull(reply);
        Assert.Contains("{user:11111111-1111-4111-8111-111111111111}: 25", reply!.PayloadJson);
        Assert.Null(new NextFixtureProcess().Run(ev with { PayloadJson = "{\"text\":\"hello\"}" }, host));
    }

    [Fact]
    public void action_stage_next_reaches_overlay()
    {
        var host = new FakeWaddleHostNext();
        var env = new StageEnvelopeInfo("t", "c", "app", "action", new PlatformEventInfo("twitch", "x", null, "{}", "ts"), "ts", null, null);
        Assert.Equal("overlay", new NextFixtureAction().Run(env, "{}", host).Detail);
        Assert.Single(host.OverlayFake.Pushes);
    }

    [Fact]
    public void the_remaining_fakes_behave()
    {
        var host = new FakeWaddleHostNext();
        Assert.Equal("ok", host.Ai.Generate("p", new Ai.AiOptions()));
        Assert.Equal("handle-img", host.Media.GenerateImage("p"));
        Assert.Equal("job-music", host.Media.SubmitMusic("p"));
        Assert.Equal("job-video", host.Media.SubmitVideo("p"));
        Assert.Equal("handle-j", host.Media.JobStatus("j").Handle);
        Assert.StartsWith("https://", host.Media.ObjectUrl("h"));
        Assert.Equal("tok", host.Identity.ResolveMention("tok"));
        host.Overlay.RegisterWidget(new OverlayWidget("w", "W", OverlaySurface.Chat, "{}"));
        Assert.Single(host.OverlayFake.Registered);
    }
}
