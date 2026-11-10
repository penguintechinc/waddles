using System.Text;
using System.Text.Json;
using WaddleSdk.Economy;
using WaddleSdk.Tool;
using Xunit;

namespace WaddleSdk.Tests;

public class EconomyServiceClientTests
{
    private const string User = "11111111-1111-4111-8111-111111111111";
    private static byte[] Result(long balance = 123) => Encoding.UTF8.GetBytes($"{{\"user\":\"{User}\",\"balance\":{balance}}}");

    [Fact]
    public void credit_calls_only_the_service_with_a_stable_write_key()
    {
        var tool = new FakeToolClient();
        tool.Handlers[(EconomyServiceTools.Target, "adjust_balance")] = _ => Result();
        var client = new EconomyServiceClient(tool, "event-1");
        Assert.Equal(123, client.Credit(User, 10));
        client.Credit(User, 10);
        var first = tool.Calls[0];
        Assert.Equal(EconomyServiceTools.Target, first.Target);
        Assert.Equal("adjust_balance", first.Tool);
        Assert.Equal(first.Key, tool.Calls[1].Key);
        Assert.NotNull(first.Key);
        using var args = JsonDocument.Parse(first.Args);
        Assert.Equal(2, args.RootElement.EnumerateObject().Count());
        Assert.Equal(10L, args.RootElement.GetProperty("delta").GetInt64());
        new EconomyServiceClient(tool, "event-2").Credit(User, 10);
        Assert.NotEqual(first.Key, tool.Calls[2].Key);
    }

    [Fact]
    public void changing_retry_amount_preserves_the_key_and_surfaces_host_conflict()
    {
        var tool = new FakeToolClient();
        byte[]? firstArgs = null;
        tool.Handlers[(EconomyServiceTools.Target, "adjust_balance")] = args =>
        {
            if (firstArgs is not null && !args.SequenceEqual(firstArgs))
                throw new WaddleToolException(ToolErrorKind.Forbidden, "idempotency_conflict");
            firstArgs = args;
            return Result();
        };
        var client = new EconomyServiceClient(tool, "event-1", "plan.own.0");
        client.Credit(User, 10);
        Assert.Equal(EconomyErrorKind.Denied,
            Assert.Throws<WaddleEconomyException>(() => client.Credit(User, 20)).Kind);
        Assert.Equal(tool.Calls[0].Key, tool.Calls[1].Key);
        Assert.NotEqual(tool.Calls[0].Args, tool.Calls[1].Args);
    }

    [Fact]
    public void reads_have_no_write_key_and_adjustments_keep_their_sign()
    {
        var tool = new FakeToolClient();
        foreach (var op in new[] { "get_balance", "adjust_balance" })
            tool.Handlers[(EconomyServiceTools.Target, op)] = _ => Result();
        var client = new EconomyServiceClient(tool, "event-1");
        Assert.Equal(123, client.Balance(User));
        client.Credit(User, 9);
        client.Debit(User, 9);
        Assert.Null(tool.Calls[0].Key);
        Assert.NotEqual(tool.Calls[1].Key, tool.Calls[2].Key);
        Assert.Contains("\"delta\":9", tool.Calls[1].Args);
        Assert.Contains("\"delta\":-9", tool.Calls[2].Args);
    }

    [Theory]
    [InlineData("insufficient_funds", EconomyErrorKind.InsufficientFunds)]
    [InlineData("over_cap", EconomyErrorKind.OverCap)]
    [InlineData("not_a_member", EconomyErrorKind.NotAMember)]
    public void known_economy_refusals_keep_their_typed_reason(string code, EconomyErrorKind kind)
    {
        var tool = new FakeToolClient();
        tool.Handlers[(EconomyServiceTools.Target, "adjust_balance")] = _ => throw new WaddleToolException(ToolErrorKind.InvalidArgs, code);
        Assert.Equal(kind, Assert.Throws<WaddleEconomyException>(() => new EconomyServiceClient(tool, "event").Credit(User, 10)).Kind);
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("{\"user\":\"wrong\",\"balance\":1}")]
    [InlineData("{\"user\":\"11111111-1111-4111-8111-111111111111\",\"balance\":-1}")]
    [InlineData("null")]
    [InlineData("bad json")]
    public void malformed_or_wrong_user_results_fail_loud(string result)
    {
        var tool = new FakeToolClient();
        tool.Handlers[(EconomyServiceTools.Target, "get_balance")] = _ => Encoding.UTF8.GetBytes(result);
        Assert.Throws<WaddleToolException>(() => new EconomyServiceClient(tool, "event").Balance(User));
    }

    [Fact]
    public void missing_grant_or_message_id_never_uses_a_private_ledger()
    {
        var tool = new FakeToolClient();
        Assert.Throws<InvalidOperationException>(() => new EconomyServiceClient(tool, " ").Credit(User, 10));
        Assert.Empty(tool.Calls);
        Assert.Equal(EconomyErrorKind.Denied, Assert.Throws<WaddleEconomyException>(() => new EconomyServiceClient(tool, "event").Credit(User, 10)).Kind);
        Assert.Throws<WaddleEconomyException>(() => new EconomyServiceClient(tool, "event").Credit(User, ulong.MaxValue));
        Assert.Throws<WaddleEconomyException>(() => new EconomyServiceClient(tool, "event").Debit(User, 0));
    }
}
