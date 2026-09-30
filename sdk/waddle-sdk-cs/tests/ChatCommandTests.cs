using WaddleSdk.Chat;
using Xunit;

namespace WaddleSdk.Tests;

public class ChatCommandTests
{
    private static string Payload(string text, string? channelId = "12345") =>
        $$"""{"text": {{System.Text.Json.JsonSerializer.Serialize(text)}}, "channel_id": {{(channelId is null ? "null" : System.Text.Json.JsonSerializer.Serialize(channelId))}}}""";

    [Fact]
    public void parses_a_bare_command_with_no_args()
    {
        var cmd = ChatCommand.TryParse(Payload("!roll"), "!");
        Assert.NotNull(cmd);
        Assert.Equal("roll", cmd!.Name);
        Assert.Empty(cmd.Args);
        Assert.Equal("12345", cmd.ChannelId);
    }

    [Fact]
    public void parses_a_command_with_args()
    {
        var cmd = ChatCommand.TryParse(Payload("!roll 20 extra"), "!");
        Assert.NotNull(cmd);
        Assert.Equal("roll", cmd!.Name);
        Assert.Equal(["20", "extra"], cmd.Args);
    }

    [Fact]
    public void trims_surrounding_whitespace()
    {
        var cmd = ChatCommand.TryParse(Payload("   !roll   "), "!");
        Assert.NotNull(cmd);
        Assert.Equal("roll", cmd!.Name);
    }

    [Theory]
    [InlineData("roll")]
    [InlineData("!")]
    [InlineData("")]
    [InlineData("hello world")]
    public void returns_null_for_non_matching_or_empty_text(string text)
    {
        Assert.Null(ChatCommand.TryParse(Payload(text), "!"));
    }

    [Fact]
    public void returns_null_for_non_json_payload()
    {
        Assert.Null(ChatCommand.TryParse("not json", "!"));
    }

    [Fact]
    public void returns_null_for_non_object_payload()
    {
        Assert.Null(ChatCommand.TryParse("[1,2,3]", "!"));
    }

    [Fact]
    public void returns_null_for_a_json_null_literal()
    {
        Assert.Null(ChatCommand.TryParse("null", "!"));
    }

    [Fact]
    public void throws_for_empty_prefix()
    {
        Assert.Throws<ArgumentException>(() => ChatCommand.TryParse(Payload("!roll"), ""));
    }

    [Fact]
    public void is_matches_case_sensitively()
    {
        var cmd = ChatCommand.TryParse(Payload("!roll"), "!")!;
        Assert.True(cmd.Is("roll"));
        Assert.False(cmd.Is("Roll"));
    }

    [Fact]
    public void is_any_matches_multiple_aliases()
    {
        var roll = ChatCommand.TryParse(Payload("!roll"), "!")!;
        var dice = ChatCommand.TryParse(Payload("!dice"), "!")!;
        Assert.True(roll.IsAny("roll", "dice"));
        Assert.True(dice.IsAny("roll", "dice"));
        Assert.False(roll.IsAny("other"));
    }

    [Fact]
    public void channel_id_is_null_when_absent()
    {
        var cmd = ChatCommand.TryParse(Payload("!roll", channelId: null), "!");
        Assert.NotNull(cmd);
        Assert.Null(cmd!.ChannelId);
    }
}
