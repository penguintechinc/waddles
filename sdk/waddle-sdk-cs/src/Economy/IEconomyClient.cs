namespace WaddleSdk.Economy;

/// <summary>
/// Mirrors `waddle:bundle/economy`'s `error` variant (`wit/waddle-bundle/stage.wit`,
/// `stage-next`/`stage-v2` worlds only).
/// </summary>
public enum EconomyErrorKind
{
    /// <summary>The gate denied the call (`actor_mismatch`, `idempotency_conflict`, `not_linked`, ...); the message carries the stable code.</summary>
    Denied,

    /// <summary>The debited user holds less than the stake; <see cref="WaddleEconomyException.Amount"/> is the balance they hold.</summary>
    InsufficientFunds,

    /// <summary>The stake/amount/payout exceeds the server-enforced cap; <see cref="WaddleEconomyException.Amount"/> is that cap.</summary>
    OverCap,

    /// <summary>A named user is not a member of this invocation's community.</summary>
    NotAMember,

    /// <summary>Malformed argument (non-UUID user, zero amount, ...).</summary>
    Invalid,

    /// <summary>The capability is not wired/enabled on this host. Fail loud -- never fall back to a private ledger.</summary>
    Unavailable,

    Backend,
}

/// <summary>
/// Thrown by <see cref="IEconomyClient"/> implementations. The per-project adapter converts the
/// generated `WitException&lt;IEconomyImports.Error&gt;` into this.
/// </summary>
public sealed class WaddleEconomyException(EconomyErrorKind kind, string message, ulong? amount = null) : Exception(message)
{
    public EconomyErrorKind Kind { get; } = kind;

    /// <summary>Balance (<see cref="EconomyErrorKind.InsufficientFunds"/>) or cap (<see cref="EconomyErrorKind.OverCap"/>); null otherwise.</summary>
    public ulong? Amount { get; } = amount;
}

/// <summary>One leaderboard row: a community member (UUID string only -- no names) and balance.</summary>
public sealed record EconomyEntry(string User, long Balance);

/// <summary>One `(user, amount)` row of <see cref="IEconomyClient.BatchCredit"/>/<see cref="IEconomyClient.PayPot"/>.</summary>
public sealed record EconomyCredit(string User, ulong Amount);

/// <summary>
/// Typed wrapper over `waddle:bundle/economy` -- the per-community currency ("waddlebucks", display
/// alias configured on the economy service). Every user argument is a community `user-uuid` as
/// returned by <see cref="Identity.IIdentityClient"/>; the community/tenant are always host-derived.
/// Each money-moving call is idempotent per (event, call ordinal) host-side: a redelivered event that
/// replays the same calls in the same order moves nothing twice. Needs process-stage (the host does
/// not wire economy into action-stage).
/// </summary>
public interface IEconomyClient
{
    /// <summary>`user`'s community balance (0 for a member who holds nothing yet). Needs `economy.read`.</summary>
    long Balance(string user);

    /// <summary>
    /// Atomically debits <paramref name="stake"/> and credits <paramref name="payout"/> (0 for a loss);
    /// returns the NEW balance. <paramref name="user"/> MUST be the invocation's actor; the payout may be
    /// at most 100x the stake. Needs `economy.wager`.
    /// </summary>
    long Wager(string user, ulong stake, ulong payout);

    /// <summary>Moves <paramref name="amount"/> from <paramref name="from"/> (MUST be the actor) to another member. Needs `economy.transfer`.</summary>
    void Transfer(string from, string to, ulong amount);

    /// <summary>The largest stake <paramref name="user"/> may place right now (grant cap bounded by their holdings). Needs `economy.wager`.</summary>
    long MaxBet(string user);

    /// <summary>The community's top <paramref name="limit"/> (1..=100) members by balance. Needs `economy.read`.</summary>
    IReadOnlyList<EconomyEntry> Leaderboard(uint limit);

    /// <summary>MINT: adds <paramref name="amount"/> to any member; returns the NEW balance. Needs `economy.credit`.</summary>
    long Credit(string user, ulong amount);

    /// <summary>MINT for 1..=100 distinct members in one all-or-nothing transaction. Needs `economy.credit`.</summary>
    void BatchCredit(IReadOnlyList<EconomyCredit> entries);

    /// <summary>SPEND/BURN: subtracts from the actor; returns the NEW balance. Needs `economy.debit`.</summary>
    long Debit(string user, ulong amount);

    /// <summary>Like <see cref="Debit"/>, but refuses unless the balance stays at least <paramref name="minBalance"/>.</summary>
    long ConditionalDebit(string user, ulong amount, ulong minBalance);

    /// <summary>Atomic multi-winner payout from the community pot (all paid or none). Needs `economy.pot.pay`.</summary>
    void PayPot(IReadOnlyList<EconomyCredit> winners);
}
