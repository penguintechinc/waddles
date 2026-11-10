using System.Text.Json.Serialization;
using System.Text.Json.Serialization.Metadata;
using WaddleSdk.Json;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Tool;

namespace WaddleSdk.Economy;

public sealed record EconomyAdjustArgs(
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("delta")] long Delta);
public sealed record EconomyAmountResult(
    [property: JsonRequired, JsonPropertyName("user")] string User,
    [property: JsonRequired, JsonPropertyName("balance")] long Balance);

/// <summary>
/// App-side balance and credit/debit access through the economy SERVICE bundle. No private ledger
/// or fallback. Game wagering and maximum-bet policy remain in the C# app. Write keys bind to the invocation and operation; the host additionally
/// namespaces them by tenant, community, caller, actor and target tool. The service owns all caps.
/// </summary>
public sealed class EconomyServiceClient(IToolClient tools, string messageId, string operationId = "")
{
    public static EconomyServiceClient For(IWaddleHostNext host, string operationId = "") => new(host.Tool, host.Context.MessageId, operationId);

    public long Balance(string user) => Read("get_balance", user);
    public long Credit(string user, ulong amount) => Adjust(user, amount, false);
    public long Debit(string user, ulong amount) => Adjust(user, amount, true);

    private long Read(string name, string user) => Call(name, user, new EconomyViewArgs(user),
        WaddleSdkJsonContext.Default.EconomyViewArgs, null);

    private long Adjust(string user, ulong amount, bool debit)
    {
        if (amount == 0 || amount > long.MaxValue)
            throw new WaddleEconomyException(EconomyErrorKind.Invalid, "invalid_amount");
        var delta = debit ? -(long)amount : (long)amount;
        return Call("adjust_balance", user, new EconomyAdjustArgs(user, delta),
            WaddleSdkJsonContext.Default.EconomyAdjustArgs, Key(debit ? "debit" : "credit", user));
    }

    private string Key(string operation, string user)
    {
        if (string.IsNullOrWhiteSpace(messageId))
            throw new InvalidOperationException("economy writes require an invocation message id");
        return StateKey.Hash(messageId + "\u001f" + operationId + "\u001f" + operation + "\u001f" + user);
    }

    private long Call<T>(string name, string user, T args, JsonTypeInfo<T> info, string? key)
    {
        try
        {
            var result = tools.Invoke(EconomyServiceTools.Target, name, args, info,
                WaddleSdkJsonContext.Default.EconomyAmountResult, key);
            if (result.User != user || result.Balance < 0)
                throw new WaddleToolException(ToolErrorKind.InvalidArgs, "invalid economy result");
            return result.Balance;
        }
        catch (WaddleToolException ex) when (ex.Kind == ToolErrorKind.InvalidArgs &&
            ex.Message is "insufficient_funds" or "over_cap" or "not_a_member")
        {
            throw new WaddleEconomyException(ex.Message switch {
                "insufficient_funds" => EconomyErrorKind.InsufficientFunds,
                "over_cap" => EconomyErrorKind.OverCap,
                _ => EconomyErrorKind.NotAMember,
            }, ex.Message);
        }
        catch (WaddleToolException ex) when (ex.Kind is ToolErrorKind.NotGranted or ToolErrorKind.Forbidden or ToolErrorKind.TargetUnavailable or ToolErrorKind.Timeout)
        {
            throw new WaddleEconomyException(ex.Kind is ToolErrorKind.NotGranted or ToolErrorKind.Forbidden
                ? EconomyErrorKind.Denied : EconomyErrorKind.Unavailable, ex.Code);
        }
    }
}
