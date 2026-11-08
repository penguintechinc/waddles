using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.State;
using Xunit;
using static WaddleBundleSuperpenguinTimers.Tests.World;

namespace WaddleBundleSuperpenguinTimers.Tests;

/// <summary>Exercises every shared helper in BundleKit.cs (copied into each utility port by the scaffold).</summary>
public class BundleKitTests
{
    private static World NewWorld() => new("waddles.command-superpenguin-test");

    [Theory]
    [InlineData("{\"is_mod\":true}", true, false)]
    [InlineData("{\"is_broadcaster\":true}", true, true)]
    [InlineData("{\"is_mod\":false,\"is_broadcaster\":false}", false, false)]
    [InlineData("{\"is_mod\":\"true\"}", false, false)]
    [InlineData("{}", false, false)]
    [InlineData("null", false, false)]
    [InlineData("not json", false, false)]
    public void roles_fail_closed_unless_a_real_boolean_says_otherwise(string payload, bool moderator, bool broadcaster)
    {
        Assert.Equal(moderator, Kit.IsModerator(payload));
        Assert.Equal(broadcaster, Kit.IsBroadcaster(payload));
    }

    [Fact]
    public void try_actor_returns_the_actor_or_null_for_unlinked_and_non_members_and_throws_otherwise()
    {
        var w = NewWorld();
        Assert.Equal(Alice, Kit.TryActor(w.HostFor(Alice, "m1"), "t.unlinked"));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Null(Kit.TryActor(w.HostFor(Alice, "m2"), "t.unlinked"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "x");
        Assert.Null(Kit.TryActor(w.HostFor(Alice, "m3"), "t.unlinked"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "x");
        Assert.Throws<WaddleIdentityException>(() => Kit.TryActor(w.HostFor(Alice, "m4"), "t.unlinked"));
    }

    [Fact]
    public void claim_once_is_first_writer_wins_per_exact_invocation_and_refuses_a_blank_message_id()
    {
        var w = NewWorld();
        var store = new KvStateStore(w.Kv);
        var ev = TestKit.Chat("!x");

        Assert.True(Kit.ClaimOnce(w.HostFor(Alice, "m1"), store, "t.c", ev, Alice, "x"));
        Assert.False(Kit.ClaimOnce(w.HostFor(Alice, "m1"), store, "t.c", ev, Alice, "x"));
        Assert.True(Kit.ClaimOnce(w.HostFor(Bob, "m1"), store, "t.c", ev, Bob, "x"));
        Assert.True(Kit.ClaimOnce(w.HostFor(Alice, "m1"), store, "t.c", ev, Alice, "y"));
        Assert.Throws<InvalidOperationException>(() => Kit.ClaimOnce(w.HostFor(Alice, " "), store, "t.c", ev, Alice, "x"));
    }

    [Fact]
    public void numbers_round_trip_through_ascii_and_garbage_reads_as_zero()
    {
        var store = new KvStateStore(new WaddleSdk.Tests.FakeKvClient());
        Assert.Equal(0, Kit.ReadLong(store, "n"));
        Kit.WriteLong(store, "n", -42);
        Assert.Equal(-42, Kit.ReadLong(store, "n"));
        store.Set("n", [0xff, 0xfe], 0);
        Assert.Equal(0, Kit.ReadLong(store, "n"));
    }

    [Fact]
    public void clean_strips_controls_and_placeholder_braces_and_caps_the_length()
    {
        Assert.Equal("hi {user}".Replace("{", string.Empty).Replace("}", string.Empty), Kit.Clean("hi {user}\n", 50));
        Assert.Equal("abc", Kit.Clean("  abcdef ", 3));
        Assert.Equal(string.Empty, Kit.Clean("\t{}", 5));
    }

    [Fact]
    public void rest_of_joins_the_arguments_and_replies_keep_the_channel()
    {
        var cmd = ChatCommand.TryParse(TestKit.Chat("!say  one   two").PayloadJson, "!")!;
        Assert.Equal("one two", Kit.RestOf(cmd));
        var reply = Kit.Reply(TestKit.Chat("!say"), cmd, "ok");
        Assert.Equal("ok", World.TextOf(reply));
    }
}
