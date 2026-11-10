using System.Text.Json;
using WaddleSdk.Identity;
using WaddleSdk.Overlay;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinWheel.Tests.World;

namespace WaddleBundleSuperpenguinWheel.Tests;

/// <summary>An overlay whose every call is refused like the host does today (`not-implemented`).</summary>
public sealed class RefusingOverlay : IOverlayClient
{
    public TransportResultInfo RegisterWidget(OverlayWidget widget) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));
}

public class WheelLogicTests
{
    private static World NewWorld()
    {
        var w = new World(WheelLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave })
        {
            w.Ledger.Seed(u, 0);
        }

        return w;
    }

    private static PlatformEventInfo Ev(string text, bool mod, bool bot = false)
    {
        var json = "{\"text\":\"" + text + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + (bot ? ",\"is_bot\":true" : "") + "}";
        return new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
    }

    private static string? Run(World w, WheelLogic l, string actor, string text, string id, bool mod = true, bool bot = false)
    {
        var reply = l.Run(Ev(text, mod, bot), w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    private static (string Type, JsonElement Amount) LastPush(World w)
    {
        var doc = JsonDocument.Parse(w.Pushed.Pushes[^1].Payload);
        var alert = doc.RootElement.GetProperty("alert");
        return (alert.GetProperty("alert_type").GetString()!, alert.GetProperty("amount").Clone());
    }

    private static WheelLogic Fixed(params int[] rolls) => new(new ScriptedRandom(rolls).Next);

    private static void DefineAndShow(World w, WheelLogic l, string spec = "Red*1|Blue*3", string name = "colors")
    {
        Assert.Contains("defined", Run(w, l, Alice, $"!wheel define {name} {spec}", "d" + name));
        Assert.Contains("Showing", Run(w, l, Alice, $"!wheel show {name}", "s" + name));
    }

    [Fact]
    public void other_commands_and_a_disabled_flag_are_ignored_and_a_missing_community_is_refused()
    {
        var w = NewWorld();
        var l = new WheelLogic();

        Assert.Null(Run(w, l, Alice, "hello", "m1"));
        Assert.Null(Run(w, l, Alice, "!other", "m2"));

        w.Flags.Overrides[WheelLogic.FlagKey] = false;
        Assert.Null(Run(w, l, Alice, "!wheel list", "m3"));
        w.Flags.Overrides[WheelLogic.FlagKey] = true;

        var noCommunity = l.Run(Ev("!wheel list", true), w.HostFor(Alice, "m4", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));
    }

    [Theory]
    [InlineData("!wheel")]
    [InlineData("!wheel nonsense")]
    public void an_unknown_or_missing_subcommand_gets_the_usage(string text) =>
        Assert.Equal(WheelLogic.Usage, Run(NewWorld(), new WheelLogic(), Alice, text, "m1", mod: false));

    [Theory]
    [InlineData("define a Red|Blue")]
    [InlineData("delete a")]
    [InlineData("show a")]
    [InlineData("hide")]
    [InlineData("spin")]
    [InlineData("message a hello")]
    [InlineData("name open")]
    public void management_commands_are_moderator_only(string sub)
    {
        var w = NewWorld();

        Assert.Equal(WheelLogic.ModOnly, Run(w, new WheelLogic(), Alice, "!wheel " + sub, "m1", mod: false));
        Assert.Empty(w.Pushed.Pushes);
    }

    [Fact]
    public void define_stores_a_wheel_updates_it_and_lists_it()
    {
        var w = NewWorld();
        var l = new WheelLogic();

        Assert.Equal("No wheels are defined.", Run(w, l, Bob, "!wheel list", "m0", mod: false));
        Assert.Equal("Wheel colors defined with 2 items.", Run(w, l, Alice, "!wheel define Colors Red*2|Blue", "m1"));
        Assert.Equal("Wheel colors updated with 3 items.", Run(w, l, Alice, "!wheel define colors Red|Blue|Green*5", "m2"));
        Assert.Equal("Wheels: colors (3)", Run(w, l, Bob, "!wheel list", "m3", mod: false));
    }

    [Theory]
    [InlineData("!wheel define", "Usage: !wheel define")]
    [InlineData("!wheel define onlyname", "Usage: !wheel define")]
    [InlineData("!wheel define bad!name Red|Blue", "Usage: !wheel define")]
    [InlineData("!wheel define a Red*0|Blue", "Weights must be whole numbers from 1 to 100.")]
    [InlineData("!wheel define a Red*101|Blue", "Weights must be whole numbers from 1 to 100.")]
    [InlineData("!wheel define a Red*x|Blue", "Weights must be whole numbers from 1 to 100.")]
    [InlineData("!wheel define a Red*-1|Blue", "Weights must be whole numbers from 1 to 100.")]
    [InlineData("!wheel define a Red||Blue", "Each label must be 1 to 24 characters.")]
    [InlineData("!wheel define a Red|AVeryLongLabelThatExceedsTwentyFour", "Each label must be 1 to 24 characters.")]
    [InlineData("!wheel define a Solo", "A wheel needs at least 2 items.")]
    public void define_validates_its_input(string text, string expectedStart) =>
        Assert.StartsWith(expectedStart, Run(NewWorld(), new WheelLogic(), Alice, text, "m1"));

    [Fact]
    public void define_refuses_too_many_items_and_too_many_wheels()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        var many = string.Join("|", Enumerable.Range(0, WheelLogic.MaxItems + 1).Select(i => "L" + i));

        Assert.Equal($"A wheel holds at most {WheelLogic.MaxItems} items.", Run(w, l, Alice, "!wheel define big " + many, "m1"));

        for (var i = 0; i < WheelLogic.MaxWheels; i++)
        {
            Assert.Contains("defined", Run(w, l, Alice, $"!wheel define w{i} A|B", "d" + i));
        }

        Assert.Contains("At most 20 wheels", Run(w, l, Alice, "!wheel define extra A|B", "m2"));
        Assert.Contains("updated", Run(w, l, Alice, "!wheel define w0 A|B|C", "m3"));
    }

    [Fact]
    public void delete_removes_a_wheel_and_reports_unknown_names()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        Run(w, l, Alice, "!wheel define a X|Y", "m1");

        Assert.StartsWith("Usage", Run(w, l, Alice, "!wheel delete", "m2"));
        Assert.Equal("No wheel named b.", Run(w, l, Alice, "!wheel delete b", "m3"));
        Assert.Equal("Wheel a deleted.", Run(w, l, Alice, "!wheel delete a", "m4"));
        Assert.Equal("No wheels are defined.", Run(w, l, Alice, "!wheel list", "m5"));
    }

    [Fact]
    public void message_sets_a_cleaned_template_that_keeps_the_label_token()
    {
        var w = NewWorld();
        var l = Fixed(0);
        DefineAndShow(w, l, "Red|Blue");

        Assert.Contains("will announce: Winner is {label} user:x now", Run(w, l, Alice, "!wheel message colors Winner   is {label} {user:x} now", "m1"));
        Run(w, l, Alice, "!wheel show colors", "show2");
        Run(w, l, Alice, "!wheel spin", "spin1");
        w.Clock.Now += WheelLogic.SpinMillis;

        Assert.Equal("Winner is Red user:x now", Run(w, l, Bob, "!wheel result", "r1", mod: false));
    }

    [Fact]
    public void message_validates_name_and_length()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        Run(w, l, Alice, "!wheel define a X|Y", "m1");

        Assert.StartsWith("Usage", Run(w, l, Alice, "!wheel message a", "m2"));
        Assert.Equal("No wheel named zzz.", Run(w, l, Alice, "!wheel message zzz hello", "m3"));
        Assert.Contains("1 to 120", Run(w, l, Alice, "!wheel message a {}", "m4"));
        Assert.Contains("1 to 120", Run(w, l, Alice, "!wheel message a " + new string('x', 121), "m5"));
    }

    [Fact]
    public void show_validates_and_pushes_the_wheel_to_the_overlay()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        Run(w, l, Alice, "!wheel define a Red*2|Blue", "m1");

        Assert.StartsWith("Usage", Run(w, l, Alice, "!wheel show", "m2"));
        Assert.Equal("No wheel named nope.", Run(w, l, Alice, "!wheel show nope", "m3"));
        Assert.Equal("Showing the a wheel.", Run(w, l, Alice, "!wheel show a", "m4"));

        Assert.Empty(w.Pushed.Registered);
        Assert.Equal(("superpenguin.wheel", OverlaySurface.AlertBox), (w.Pushed.Pushes[^1].WidgetId, w.Pushed.Pushes[^1].Surface));
        var (type, amount) = LastPush(w);
        Assert.Equal("superpenguin.wheel.show", type);
        Assert.Equal("a", amount.GetProperty("wheel").GetString());
        Assert.Equal(2, amount.GetProperty("items").GetArrayLength());
        Assert.Equal(2, amount.GetProperty("items")[0].GetProperty("weight").GetInt32());
        Assert.Equal("Blue", amount.GetProperty("labels")[1].GetString());
    }

    [Fact]
    public void hide_clears_the_wheel_and_tells_the_overlay()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        DefineAndShow(w, l);

        Assert.Equal("The wheel is hidden.", Run(w, l, Alice, "!wheel hide", "m1"));

        Assert.Equal("superpenguin.wheel.hide", LastPush(w).Type);
        Assert.Equal("There is no spin to report.", Run(w, l, Bob, "!wheel result", "m2", mod: false));
        Assert.Contains("No wheel is showing", Run(w, l, Alice, "!wheel spin", "m3"));
    }

    [Theory]
    [InlineData(0, "Red")]
    [InlineData(1, "Blue")]
    [InlineData(3, "Blue")]
    public void the_weighted_roll_selects_the_slice_and_the_overlay_spins_to_it(int roll, string expected)
    {
        var w = NewWorld();
        var l = Fixed(roll);
        DefineAndShow(w, l);

        var text = Run(w, l, Alice, "!wheel spin", "spin");

        Assert.Equal("The wheel is spinning! Ask for !wheel result in 10 seconds.", text);
        var (type, amount) = LastPush(w);
        Assert.Equal("superpenguin.wheel.spin", type);
        Assert.Equal(expected == "Red" ? 0 : 1, amount.GetProperty("spin_to").GetInt32());
        Assert.Equal(WheelLogic.SpinMillis, amount.GetProperty("duration_ms").GetInt32());
        Assert.Equal(16, amount.GetProperty("spin_id").GetString()!.Length);
        w.Clock.Now += WheelLogic.SpinMillis;
        Assert.Equal($"Congratulations {expected}!", Run(w, l, Bob, "!wheel result", "r", mod: false));
    }

    [Fact]
    public void the_result_waits_for_the_spin_duration()
    {
        var w = NewWorld();
        var l = Fixed(0);
        DefineAndShow(w, l);
        Assert.Equal("There is no spin to report.", Run(w, l, Bob, "!wheel result", "r0", mod: false));
        Run(w, l, Alice, "!wheel spin", "spin");

        Assert.Equal("The wheel is still spinning...", Run(w, l, Bob, "!wheel result", "r1", mod: false));
        w.Clock.Now += WheelLogic.SpinMillis - 1;
        Assert.Equal("The wheel is still spinning...", Run(w, l, Bob, "!wheel result", "r2", mod: false));
        w.Clock.Now += 1;
        Assert.Equal("Congratulations Red!", Run(w, l, Bob, "!wheel result", "r3", mod: false));
    }

    [Fact]
    public void a_spin_in_progress_blocks_another_until_it_finishes()
    {
        var w = NewWorld();
        var l = Fixed(0, 3);
        DefineAndShow(w, l);
        Run(w, l, Alice, "!wheel spin", "s1");

        Assert.Equal("The wheel is already spinning.", Run(w, l, Alice, "!wheel spin", "s2"));
        w.Clock.Now += WheelLogic.SpinMillis;
        Assert.Contains("spinning!", Run(w, l, Alice, "!wheel spin", "s3"));
        w.Clock.Now += WheelLogic.SpinMillis;
        Assert.Equal("Congratulations Blue!", Run(w, l, Bob, "!wheel result", "r", mod: false));
    }

    [Fact]
    public void the_winner_is_recorded_before_the_overlay_is_told_and_a_redelivery_reuses_it()
    {
        var w = NewWorld();
        var l = Fixed(0, 3);
        DefineAndShow(w, l);
        w.Overlay = new RefusingOverlay();

        Assert.Throws<WaddleTransportException>(() => Run(w, l, Alice, "!wheel spin", "spin-1"));

        w.Overlay = new WaddleSdk.Tests.FakeOverlayClient();
        var text = Run(w, l, Alice, "!wheel spin", "spin-1");
        Assert.Contains("spinning", text);
        var (_, amount) = LastPush(w);
        Assert.Equal(0, amount.GetProperty("spin_to").GetInt32());
        var firstId = amount.GetProperty("spin_id").GetString();

        var again = Run(w, l, Alice, "!wheel spin", "spin-1");
        Assert.Equal(text, again);
        Assert.Equal(firstId, LastPush(w).Amount.GetProperty("spin_id").GetString());
        Assert.Equal(0, LastPush(w).Amount.GetProperty("spin_to").GetInt32());
    }

    [Fact]
    public void a_redelivered_spin_after_the_wheel_was_hidden_says_so()
    {
        var w = NewWorld();
        var l = Fixed(0);
        DefineAndShow(w, l);
        Run(w, l, Alice, "!wheel spin", "spin");
        Run(w, l, Alice, "!wheel hide", "hide");

        Assert.Contains("No wheel is showing", Run(w, l, Alice, "!wheel spin", "spin"));
    }

    [Fact]
    public void spin_needs_a_linked_account_and_a_message_id()
    {
        var w = NewWorld();
        var l = Fixed(0);
        DefineAndShow(w, l);

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!wheel spin", "m1"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, l, Alice, "!wheel spin", "m2"));
        w.ActorFailure = null;
        Assert.Throws<ArgumentException>(() => Run(w, l, Alice, "!wheel spin", " "));
    }

    [Fact]
    public void overlay_refusals_fail_loud_for_show_and_hide()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        Run(w, l, Alice, "!wheel define a X|Y", "m1");
        w.Overlay = new RefusingOverlay();

        var show = Assert.Throws<WaddleTransportException>(() => Run(w, l, Alice, "!wheel show a", "m2"));
        Assert.Equal("not_implemented", show.Error.Code);
        Assert.Throws<WaddleTransportException>(() => Run(w, l, Alice, "!wheel hide", "m3"));
    }

    [Fact]
    public void the_viewer_wheel_collects_humans_once_and_hides_names_from_the_overlay()
    {
        var w = NewWorld();
        var l = Fixed(1);

        Assert.Equal("The viewer wheel is not open.", Run(w, l, Alice, "!wheel join", "j0", mod: false));
        Assert.Contains("is open", Run(w, l, Dave, "!wheel name open", "o1"));
        Assert.Null(Run(w, l, Alice, "!wheel join", "j1", mod: false));
        Assert.Null(Run(w, l, Alice, "!wheel join", "j2", mod: false));
        Assert.Null(Run(w, l, Bob, "!wheel name join", "j3", mod: false));
        Assert.Null(Run(w, l, Carol, "!wheel join", "j4", mod: false, bot: true));

        Assert.Contains("Showing the viewer wheel with 2 entrants", Run(w, l, Dave, "!wheel name show", "sh"));
        var payload = w.Pushed.Pushes[^1].Payload;
        Assert.Contains("Entrant 1", payload);
        Assert.DoesNotContain(Alice, payload);
        Assert.DoesNotContain(Bob, payload);
        Assert.Equal("The viewer wheel is not open.", Run(w, l, Carol, "!wheel join", "late", mod: false));

        Assert.Contains("spinning", Run(w, l, Dave, "!wheel name spin", "sp"));
        w.Clock.Now += WheelLogic.SpinMillis;
        Assert.Equal($"Congratulations {{user:{Bob}}}!", Run(w, l, Dave, "!wheel result", "res", mod: false));

        Assert.Contains("closed", Run(w, l, Dave, "!wheel name close", "cl"));
        Assert.Equal("superpenguin.wheel.hide", LastPush(w).Type);
        Assert.Equal("There is no spin to report.", Run(w, l, Dave, "!wheel result", "res2", mod: false));
    }

    [Fact]
    public void the_viewer_wheel_needs_two_entrants_and_a_name_wheel_for_name_spin()
    {
        var w = NewWorld();
        var l = new WheelLogic();

        Assert.Contains("needs at least 2", Run(w, l, Dave, "!wheel name show", "s0"));
        Run(w, l, Dave, "!wheel name open", "o1");
        Run(w, l, Alice, "!wheel join", "j1", mod: false);
        Assert.Contains("needs at least 2", Run(w, l, Dave, "!wheel name show", "s1"));

        Run(w, l, Dave, "!wheel define a X|Y", "d1");
        Run(w, l, Dave, "!wheel show a", "s2");
        Assert.Contains("viewer wheel is not showing", Run(w, l, Dave, "!wheel name spin", "sp"));
        Assert.Contains("Usage: !wheel name", Run(w, l, Dave, "!wheel name", "u1"));
        Assert.Contains("Usage: !wheel name", Run(w, l, Dave, "!wheel name bogus", "u2"));
    }

    [Fact]
    public void joining_needs_a_linked_account_and_stops_at_the_cap()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        Run(w, l, Dave, "!wheel name open", "o1");

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!wheel join", "j1", mod: false));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, l, Alice, "!wheel join", "j2", mod: false));
        w.ActorFailure = null;

        for (var i = 0; i < WheelLogic.MaxEntrants; i++)
        {
            Assert.Null(Run(w, l, $"user-{i}", "!wheel join", $"j{i}x", mod: false));
        }

        Assert.Contains("is full", Run(w, l, "one-more", "!wheel join", "jfull", mod: false));
    }

    [Fact]
    public void pick_walks_the_cumulative_weights_and_rejects_an_out_of_range_roll()
    {
        Assert.Equal(0, WheelLogic.Pick([2, 3], 1));
        Assert.Equal(1, WheelLogic.Pick([2, 3], 2));
        Assert.Equal(1, WheelLogic.Pick([2, 3], 4));
        Assert.Throws<ArgumentOutOfRangeException>(() => WheelLogic.Pick([2, 3], 5));
    }

    [Fact]
    public void the_default_random_source_always_lands_on_a_real_slice()
    {
        var w = NewWorld();
        var l = new WheelLogic();
        DefineAndShow(w, l);

        Run(w, l, Alice, "!wheel spin", "spin");

        Assert.InRange(LastPush(w).Amount.GetProperty("spin_to").GetInt32(), 0, 1);
    }

    [Fact]
    public void the_odds_follow_the_weights()
    {
        var counts = new int[3];
        for (var roll = 0; roll < 6; roll++)
        {
            counts[WheelLogic.Pick([1, 2, 3], roll)]++;
        }

        Assert.Equal([1, 2, 3], counts);
    }

    [Theory]
    [InlineData("  a\tb   {x}  ", "a b x")]
    [InlineData("{user:abc}", "user:abc")]
    [InlineData("", "")]
    public void clean_strips_braces_controls_and_extra_spaces(string raw, string expected) =>
        Assert.Equal(expected, WheelLogic.Clean(raw));

    [Fact]
    public void clean_template_keeps_only_the_label_token()
    {
        Assert.Equal("Hi {label} name", WheelLogic.CleanTemplate("Hi {label} {name}"));
    }

    [Fact]
    public void a_wheel_with_a_corrupt_stored_document_is_treated_as_absent()
    {
        var w = NewWorld();
        w.Kv.Set("wheel.defs", [1, 2, 3], 0);

        Assert.Equal("No wheels are defined.", Run(w, new WheelLogic(), Alice, "!wheel list", "m1", mod: false));
    }
}
