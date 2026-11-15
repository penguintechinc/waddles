using System.Text.Json;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using Xunit;

namespace SuperPenguin.DropGameCore.Tests;

/// <summary>Stake parsing/clamping, the overlay envelope + descriptor, and the replay guard/record on their own (the pieces Racing/UfoClaw reuse).</summary>
public class SupportTests
{
    [Theory]
    [InlineData("50", 100L, 500UL, 50UL)]
    [InlineData("900", 1000L, 500UL, 500UL)]
    [InlineData("all", 120L, 500UL, 120UL)]
    [InlineData("MAX", 800L, 500UL, 500UL)]
    [InlineData("+7", 100L, 500UL, 7UL)]
    public void Parse_accepts_and_clamps(string arg, long held, ulong maxBet, ulong expected)
    {
        var r = StakeParser.Parse(arg, held, maxBet);
        Assert.True(r.Ok);
        Assert.Equal(expected, r.Stake);
        Assert.Null(r.Refusal);
    }

    [Theory]
    [InlineData("nope", 100L, "The amount must be a number, max, or all")]
    [InlineData("1.5", 100L, "The amount must be a number, max, or all")]
    [InlineData("0", 100L, "The amount needs to be greater then 0")]
    [InlineData("-3", 100L, "The amount needs to be greater then 0")]
    [InlineData("101", 100L, "You don't have that many.")]
    [InlineData("all", 0L, "You don't have that many.")]
    [InlineData("all", -5L, "You don't have that many.")]
    [InlineData("5", -5L, "You don't have that many.")]
    public void Parse_refuses(string arg, long held, string refusal)
    {
        var r = StakeParser.Parse(arg, held, 500);
        Assert.False(r.Ok);
        Assert.Equal(0UL, r.Stake);
        Assert.Equal(refusal, r.Refusal);
    }

    [Fact]
    public void Envelope_has_the_documented_fixed_fields_and_embeds_data_verbatim()
    {
        var json = OverlayEnvelope.Build("plinko", "settled", "00000000000000ab", "uuid-1", 100, 250, "{\"k\":[1,2,3]}");
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        Assert.Single(root.EnumerateObject());
        var extra = root.GetProperty("extra");
        Assert.Equal(["v", "game", "kind", "seed", "actor", "stake", "payout", "data"], extra.EnumerateObject().Select(p => p.Name).ToArray());
        Assert.Equal(OverlayEnvelope.Version, extra.GetProperty("v").GetInt32());
        Assert.Equal("00000000000000ab", extra.GetProperty("seed").GetString());
        Assert.Equal(250, extra.GetProperty("payout").GetInt32());
        Assert.Equal("{\"k\":[1,2,3]}", extra.GetProperty("data").GetRawText());
    }

    [Fact]
    public void Envelope_rejects_invalid_data_json()
    {
        Assert.ThrowsAny<Exception>(() => OverlayEnvelope.Build("g", "settled", "s", "a", 1, 0, "{not json"));
    }

    [Fact]
    public void Descriptor_declares_size_anchor_binding_and_asset_prefix()
    {
        using var doc = JsonDocument.Parse(OverlayEnvelope.Descriptor("plinko", 1920, 1080));
        var d = doc.RootElement;
        Assert.Equal(1920, d.GetProperty("size").GetProperty("w").GetInt32());
        Assert.Equal(1080, d.GetProperty("size").GetProperty("h").GetInt32());
        Assert.Equal("center", d.GetProperty("anchor").GetString());
        Assert.Equal("extra", d.GetProperty("bindings")[0].GetString());
        Assert.Equal(OverlayEnvelope.Version, d.GetProperty("envelope_version").GetInt32());
        Assert.Equal("drop-games/plinko/", d.GetProperty("asset_prefix").GetString());
        using var top = JsonDocument.Parse(OverlayEnvelope.Descriptor("g", 10, 20, "top"));
        Assert.Equal("top", top.RootElement.GetProperty("anchor").GetString());
    }

    [Fact]
    public void Publisher_is_usable_standalone_by_games_that_do_not_use_the_base()
    {
        var world = new World("waddles.command-superpenguin-x");
        var host = world.HostFor(World.Alice, "m1");
        var widget = new OverlayWidget("x", "X", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor("x", 100, 100));
        var store = new KvStateStore(host.Kv);
        Assert.True(OverlayPublisher.Publish(host, store, "x", widget, "{\"extra\":{}}"));
        Assert.True(OverlayPublisher.Publish(host, store, "x", widget, "{\"extra\":{}}"));
        Assert.Single(world.Overlay.Registered);
        Assert.Equal(2, world.Overlay.Pushes.Count);
    }

    [Fact]
    public void Replay_key_binds_message_platform_actor_and_command()
    {
        var world = new World("f");
        var host = world.HostFor(World.Alice, "m1");
        var ev = World.Msg("!x 1");
        var key = DropReplayGuard.KeyFor("x", ev, host, World.Alice, "x");
        Assert.StartsWith("dg.x.out.", key);
        Assert.NotEqual(key, DropReplayGuard.KeyFor("x", ev, host, World.Bob, "x"));
        Assert.NotEqual(key, DropReplayGuard.KeyFor("x", ev, host, World.Alice, "y"));
        Assert.NotEqual(key, DropReplayGuard.KeyFor("x", World.Msg("!x 1", "discord"), host, World.Alice, "x"));
        Assert.NotEqual(key, DropReplayGuard.KeyFor("x", ev, world.HostFor(World.Alice, "m2"), World.Alice, "x"));
        Assert.Throws<InvalidOperationException>(() => DropReplayGuard.KeyFor("x", ev, world.HostFor(World.Alice, ""), World.Alice, "x"));
    }

    [Fact]
    public void TryReplay_requires_every_binding_field_to_match()
    {
        var world = new World("f");
        var store = new KvStateStore(world.Kv);
        var rec = new DropReplayRecord("a", "x", "h", 5, "0000000000000001", "cfg", "opaque");
        store.SetJson("k", rec, DropCoreJsonContext.Default.DropReplayRecord, 60);
        Assert.Equal(rec, DropReplayGuard.TryReplay(store, "k", "a", "x", "h"));
        Assert.Null(DropReplayGuard.TryReplay(store, "k", "b", "x", "h"));
        Assert.Null(DropReplayGuard.TryReplay(store, "k", "a", "y", "h"));
        Assert.Null(DropReplayGuard.TryReplay(store, "k", "a", "x", "other"));
        Assert.Null(DropReplayGuard.TryReplay(store, "absent", "a", "x", "h"));
    }

    [Fact]
    public void Chat_reply_goes_to_the_events_own_channel()
    {
        var ev = World.Msg("!x 1");
        var cmd = WaddleSdk.Chat.ChatCommand.TryParse(ev.PayloadJson, DropSupport.CommandPrefix)!;
        Assert.Equal("hi", World.TextOf(DropSupport.Reply(ev, cmd, "hi")));
    }
}
