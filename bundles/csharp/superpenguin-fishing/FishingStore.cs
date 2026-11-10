using WaddleSdk.State;

namespace WaddleBundleSuperpenguinFishing;

/// <summary>
/// Persistence seam of the fishing game. Everything the rules persist goes through this interface, so the kv-backed
/// <see cref="KvFishingStore"/> can be replaced by bundle storage v2 (or a db-backed store) without touching a rule. All
/// operations are single-key reads/writes: the game never needs a multi-key transaction (see the README for the replay protocol).
/// </summary>
public interface IFishingStore
{
    /// <summary>Loads a player's state (a new empty one when none exists).</summary>
    PlayerState LoadPlayer(string user);

    /// <summary>Writes a player's whole state in one atomic value.</summary>
    void SavePlayer(string user, PlayerState state);

    /// <summary>Takes the short per-player mutation lock; false when another command holds it.</summary>
    bool TryLock(string user);

    /// <summary>Releases the per-player mutation lock.</summary>
    void Unlock(string user);

    /// <summary>Loads a participant's aggregate for a tournament window.</summary>
    TournamentAggregate LoadAggregate(string windowId, string user);

    /// <summary>Writes a participant's aggregate for a tournament window.</summary>
    void SaveAggregate(string windowId, string user, TournamentAggregate aggregate);

    /// <summary>Loads a window's board (null when no catch was ever recorded in it).</summary>
    TournamentBoard? LoadBoard(string windowId);

    /// <summary>Writes a window's board.</summary>
    void SaveBoard(string windowId, TournamentBoard board);

    /// <summary>Loads the lifetime leaderboards.</summary>
    LifetimeBoards LoadLifetime();

    /// <summary>Writes the lifetime leaderboards.</summary>
    void SaveLifetime(LifetimeBoards boards);

    /// <summary>Whether the overlay widget was already registered recently.</summary>
    bool WidgetRegistered();

    /// <summary>Remembers that the overlay widget was registered.</summary>
    void MarkWidgetRegistered();
}

/// <summary>`IFishingStore` over the bundle's kv (via <see cref="IStateStore"/>); every key is hashed per StateKey rules (no ':' ever).</summary>
public sealed class KvFishingStore(IStateStore store) : IFishingStore
{
    /// <summary>Per-player lock lifetime: long enough for one command's reads/writes, short enough to self-heal after a crash.</summary>
    internal const uint LockSeconds = 10;

    /// <summary>Tournament aggregates/boards outlive their window by a day so a late settlement still finds them.</summary>
    internal const uint TournamentTtlSeconds = 4 * 24 * 60 * 60;

    private const uint WidgetTtlSeconds = 24 * 60 * 60;

    private static string PlayerKey(string user) => StateKey.Of("p", StateKey.Hash(user));

    /// <inheritdoc />
    public PlayerState LoadPlayer(string user) => store.GetJson(PlayerKey(user), FishingJsonContext.Default.PlayerState) ?? new PlayerState();

    /// <inheritdoc />
    public void SavePlayer(string user, PlayerState state) => store.SetJson(PlayerKey(user), state, FishingJsonContext.Default.PlayerState);

    /// <inheritdoc />
    public bool TryLock(string user) => store.TryClaim(StateKey.Of("lock", StateKey.Hash(user)), LockSeconds);

    /// <inheritdoc />
    public void Unlock(string user) => store.Delete(StateKey.Of("lock", StateKey.Hash(user)));

    private static string AggregateKey(string windowId, string user) => StateKey.Of("ta", StateKey.Hash(windowId), StateKey.Hash(user));

    /// <inheritdoc />
    public TournamentAggregate LoadAggregate(string windowId, string user) =>
        store.GetJson(AggregateKey(windowId, user), FishingJsonContext.Default.TournamentAggregate) ?? new TournamentAggregate();

    /// <inheritdoc />
    public void SaveAggregate(string windowId, string user, TournamentAggregate aggregate) =>
        store.SetJson(AggregateKey(windowId, user), aggregate, FishingJsonContext.Default.TournamentAggregate, TournamentTtlSeconds);

    private static string BoardKey(string windowId) => StateKey.Of("tb", StateKey.Hash(windowId));

    /// <inheritdoc />
    public TournamentBoard? LoadBoard(string windowId) => store.GetJson(BoardKey(windowId), FishingJsonContext.Default.TournamentBoard);

    /// <inheritdoc />
    public void SaveBoard(string windowId, TournamentBoard board) =>
        store.SetJson(BoardKey(windowId), board, FishingJsonContext.Default.TournamentBoard, TournamentTtlSeconds);

    private static readonly string LifetimeKey = StateKey.Of("lb", "life");

    /// <inheritdoc />
    public LifetimeBoards LoadLifetime() => store.GetJson(LifetimeKey, FishingJsonContext.Default.LifetimeBoards) ?? new LifetimeBoards();

    /// <inheritdoc />
    public void SaveLifetime(LifetimeBoards boards) => store.SetJson(LifetimeKey, boards, FishingJsonContext.Default.LifetimeBoards);

    private static readonly string WidgetKey = StateKey.Of("ov", "widget");

    /// <inheritdoc />
    public bool WidgetRegistered() => store.Get(WidgetKey) is not null;

    /// <inheritdoc />
    public void MarkWidgetRegistered() => store.Set(WidgetKey, [1], WidgetTtlSeconds);
}
