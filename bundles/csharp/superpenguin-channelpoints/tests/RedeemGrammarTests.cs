using Xunit;

namespace WaddleBundleSuperpenguinChannelpoints.Tests;

public class RedeemGrammarTests
{
    [Theory]
    [InlineData("Hydrate award 5", "Hydrate", "award 5")]
    [InlineData("\"Hydrate now\" award 5", "Hydrate now", "award 5")]
    [InlineData("  \"A b\"   say hi  ", "A b", "say hi")]
    [InlineData("Solo", "Solo", "")]
    [InlineData("\"Quoted only\"", "Quoted only", "")]
    [InlineData("11111111-1111-4111-8111-111111111111 say x", "11111111-1111-4111-8111-111111111111", "say x")]
    public void references_are_a_quoted_title_or_one_token(string text, string reference, string rest)
    {
        Assert.True(RedeemGrammar.TryParseReference(text, out var r, out var tail));
        Assert.Equal((reference, rest), (r, tail));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("\"unterminated award 5")]
    [InlineData("\"\" award 5")]
    public void malformed_references_are_rejected(string text) =>
        Assert.False(RedeemGrammar.TryParseReference(text, out _, out _));

    [Fact]
    public void a_reference_longer_than_the_limit_is_rejected() =>
        Assert.False(RedeemGrammar.TryParseReference(new string('a', RedeemGrammar.MaxReference + 1), out _, out _));

    [Fact]
    public void keys_are_id_for_a_uuid_and_a_case_insensitive_hash_for_a_title()
    {
        const string id = "AAAAAAAA-1111-4111-8111-111111111111";
        Assert.Equal("id.aaaaaaaa-1111-4111-8111-111111111111", RedeemGrammar.KeyFor(id));
        Assert.Equal(RedeemGrammar.KeyFor("Hydrate"), RedeemGrammar.KeyFor("  hYDRATE "));
        Assert.StartsWith("title.", RedeemGrammar.KeyFor("Hydrate"));
        Assert.NotEqual(RedeemGrammar.KeyFor("Hydrate"), RedeemGrammar.KeyFor("Hydrate2"));
        Assert.Equal(RedeemGrammar.KeyForTitle("x"), RedeemGrammar.KeyFor("x"));
    }

    [Fact]
    public void steps_parse_in_order_with_their_arguments()
    {
        Assert.True(RedeemGrammar.TryParseSteps("award 25 && say Thanks {user}! && ALERT  big   one", out var steps, out var error));

        Assert.Equal(string.Empty, error);
        Assert.Equal([("award", 25L, ""), ("say", 0L, "Thanks {user}!"), ("alert", 0L, "big one")], steps.Select(s => (s.Kind, s.Amount, s.Text)).ToArray());
    }

    [Theory]
    [InlineData("", "Usage: !redeem map")]
    [InlineData("award", "award needs a whole amount")]
    [InlineData("award 0", "award needs a whole amount")]
    [InlineData("award 1001", "award needs a whole amount")]
    [InlineData("award -5", "award needs a whole amount")]
    [InlineData("award x", "award needs a whole amount")]
    [InlineData("award 5 && award 6", "Only one award step")]
    [InlineData("say", "say needs text")]
    [InlineData("alert  ", "alert needs text")]
    [InlineData("dance now", "Steps are:")]
    [InlineData("say a && say b && say c && say d", "At most 3 steps")]
    public void bad_actions_are_refused_with_a_reason(string text, string reason)
    {
        Assert.False(RedeemGrammar.TryParseSteps(text, out _, out var error));

        Assert.Contains(reason, error);
    }

    [Fact]
    public void a_template_over_the_limit_is_refused()
    {
        Assert.False(RedeemGrammar.TryParseSteps("say " + new string('x', RedeemGrammar.MaxTemplate + 1), out _, out var error));
        Assert.Contains("1 to 200", error);
    }

    [Fact]
    public void descriptions_truncate_long_text()
    {
        var steps = new List<RedeemStep> { new("award", 5, ""), new("say", 0, new string('y', 50)) };

        Assert.Equal($"award 5 + say \"{new string('y', 30)}\"", RedeemGrammar.Describe(steps));
    }

    [Theory]
    [InlineData("!ban {user:evil} @everyone hi", "ban user:evil everyone hi")]
    [InlineData("   /me   spaces\tand\nnewlines ", "me spaces and newlines")]
    [InlineData("normal text", "normal text")]
    [InlineData("", "")]
    [InlineData("   ", "")]
    [InlineData(null, "")]
    public void viewer_text_is_made_safe_to_echo(string? raw, string expected) =>
        Assert.Equal(expected, RedeemGrammar.Clean(raw));

    [Fact]
    public void viewer_text_is_bounded()
    {
        Assert.Equal(RedeemGrammar.MaxViewerText, RedeemGrammar.Clean(new string('z', 500)).Length);
    }

    [Fact]
    public void control_characters_are_replaced_in_templates() =>
        Assert.Equal("a b c", RedeemGrammar.StripControl("a\tb\r\nc"));

    [Fact]
    public void every_documented_placeholder_expands_and_unknown_ones_stay()
    {
        var text = RedeemGrammar.Render(
            "{USER}/{DisplayName}/{reward}/{RewardTitle}/{cost}/{RewardCost}/{input}/{UserInput}/{message}/{amount}/{currency}/{nope}",
            "{user:u1}", "Hydrate", 300, "water", 7, "stars");

        Assert.Equal("{user:u1}/{user:u1}/Hydrate/Hydrate/300/300/water/water/water/7/stars/{nope}", text);
    }

    [Fact]
    public void truncate_leaves_short_text_alone()
    {
        Assert.Equal("abc", RedeemGrammar.Truncate("abc", 5));
        Assert.Equal("ab", RedeemGrammar.Truncate("abc", 2));
    }
}
