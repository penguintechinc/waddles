using WaddleSdk.Chat;
using WaddleSdk.Db;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Scheduling;
using WaddleSdk.State;
using Xunit;

namespace WaddleSdk.Tests;

public class StateKeyTests
{
    [Fact]
    public void of_joins_parts_with_dots()
    {
        Assert.Equal("cd.roll.abc", StateKey.Of("cd", "roll", "abc"));
    }

    [Theory]
    [InlineData("a:b")]
    [InlineData("has space")]
    [InlineData("star*")]
    public void of_rejects_chars_the_host_would_reject(string part)
    {
        Assert.Throws<ArgumentException>(() => StateKey.Of("x", part));
    }

    [Fact]
    public void of_rejects_empty_and_overlong_keys()
    {
        Assert.Throws<ArgumentException>(() => StateKey.Of());
        Assert.Throws<ArgumentException>(() => StateKey.Of(new string('a', StateKey.MaxLength + 1)));
    }

    [Fact]
    public void hash_is_stable_key_safe_and_never_contains_the_input()
    {
        var h = StateKey.Hash("twitch:12345");
        Assert.Equal(32, h.Length);
        Assert.Equal(h, StateKey.Hash("twitch:12345"));
        Assert.NotEqual(h, StateKey.Hash("twitch:12346"));
        Assert.DoesNotContain("12345", h);
        StateKey.Of("k", h);
    }
}

public abstract class StateStoreContract
{
    protected MutableClock Clock { get; } = new();
    protected abstract IStateStore Create();

    [Fact]
    public void get_returns_null_for_absent_key()
    {
        Assert.Null(Create().Get("missing"));
    }

    [Fact]
    public void set_then_get_round_trips_and_overwrites()
    {
        var s = Create();
        s.Set("k", [1, 2], 0);
        Assert.Equal(new byte[] { 1, 2 }, s.Get("k"));
        s.Set("k", [3], 0);
        Assert.Equal(new byte[] { 3 }, s.Get("k"));
    }

    [Fact]
    public void delete_removes_the_key_and_is_idempotent()
    {
        var s = Create();
        s.Set("k", [1], 0);
        s.Delete("k");
        Assert.Null(s.Get("k"));
        s.Delete("k");
    }

    [Fact]
    public void try_claim_is_first_wins()
    {
        var s = Create();
        Assert.True(s.TryClaim("claim", 60));
        Assert.False(s.TryClaim("claim", 60));
        Assert.True(s.TryClaim("other", 60));
    }

    [Fact]
    public void mutate_creates_updates_and_deletes()
    {
        var s = Create();
        Assert.True(s.Mutate("pot", cur => cur is null ? [10] : [(byte)(cur[0] + 5)], 0));
        Assert.Equal(new byte[] { 10 }, s.Get("pot"));
        Assert.True(s.Mutate("pot", cur => [(byte)(cur![0] + 5)], 0));
        Assert.Equal(new byte[] { 15 }, s.Get("pot"));
        Assert.True(s.Mutate("pot", _ => null, 0));
        Assert.Null(s.Get("pot"));
        Assert.True(s.Mutate("never", _ => null, 0));
    }

    [Fact]
    public void json_helpers_round_trip_and_tolerate_garbage()
    {
        var s = Create();
        var value = new ChatReplyPayload("hi", "chan");
        s.SetJson("j", value, WaddleSdkJsonContext.Default.ChatReplyPayload);
        Assert.Equal(value, s.GetJson("j", WaddleSdkJsonContext.Default.ChatReplyPayload));
        s.Set("bad", "nope"u8.ToArray(), 0);
        Assert.Null(s.GetJson("bad", WaddleSdkJsonContext.Default.ChatReplyPayload));
        Assert.Null(s.GetJson("absent", WaddleSdkJsonContext.Default.ChatReplyPayload));
    }
}

public class KvStateStoreTests : StateStoreContract
{
    private readonly FakeKvClient _kv = new();
    protected override IStateStore Create() => new KvStateStore(_kv);

    [Fact]
    public void is_not_an_atomic_mutate_store()
    {
        Assert.False(Create().AtomicMutate);
    }

    [Fact]
    public void try_claim_does_not_refresh_the_ttl_of_an_already_claimed_key()
    {
        // The host refreshes TTL on every increment; a rejected claim must not touch the key.
        var s = Create();
        Assert.True(s.TryClaim("c", 60));
        var incrementsBefore = _kv.SetCalls.Count;
        Assert.False(s.TryClaim("c", 60));
        Assert.Equal(incrementsBefore, _kv.SetCalls.Count);
    }

    [Fact]
    public void a_denying_kv_surfaces_loudly()
    {
        var s = new KvStateStore(new DenyingKvClient());
        Assert.Throws<WaddleKvException>(() => s.TryClaim("c", 60));
    }
}

public class DbStateStoreTests : StateStoreContract
{
    private readonly FakeDbClient _db = new();

    protected override IStateStore Create() => new DbStateStore(_db, Clock);

    [Fact]
    public void is_an_atomic_mutate_store()
    {
        Assert.True(Create().AtomicMutate);
    }

    [Fact]
    public void expired_rows_read_as_absent_and_are_deleted_lazily()
    {
        var s = Create();
        s.Set("k", [1], 10);
        Assert.NotNull(s.Get("k"));
        Clock.Now += 10_001;
        Assert.Null(s.Get("k"));
        Assert.Empty(_db.Query(10, 0));
    }

    [Fact]
    public void expired_claim_can_be_taken_again()
    {
        var s = Create();
        Assert.True(s.TryClaim("c", 5));
        Clock.Now += 5_001;
        Assert.True(s.TryClaim("c", 5));
    }

    [Fact]
    public void mutate_retries_on_conflict_and_gives_up_after_max_attempts()
    {
        var racing = new RacingDb(_db);
        var s = new DbStateStore(racing, Clock);
        s.Set("pot", [1], 0);

        // One concurrent writer sneaks in between read and write -> first attempt conflicts, second succeeds.
        racing.ConflictsRemaining = 1;
        var applied = 0;
        Assert.True(s.Mutate("pot", cur => { applied++; return [(byte)(cur![0] + 1)]; }, 0));
        Assert.Equal(2, applied);

        // A permanently contended row exhausts the attempts and reports failure instead of looping or lying.
        racing.ConflictsRemaining = int.MaxValue;
        Assert.False(s.Mutate("pot", cur => [(byte)(cur![0] + 1)], 0, maxAttempts: 3));
    }

    [Fact]
    public void mutate_conflict_on_delete_and_insert_paths_is_retried()
    {
        var racing = new RacingDb(_db);
        var s = new DbStateStore(racing, Clock);
        s.Set("d", [1], 0);
        racing.ConflictsRemaining = 1;
        Assert.True(s.Mutate("d", _ => null, 0));
        Assert.Null(s.Get("d"));
    }

    [Fact]
    public void try_claim_loser_removes_its_own_row_when_another_row_won()
    {
        // Simulate two concurrent first claims: the other writer's row exists by the time we re-scan.
        var other = new InsertRaceDb(_db);
        var s = new DbStateStore(other, Clock);
        Assert.False(s.TryClaim("c", 60));
        Assert.Single(_db.Query(10, 0));
    }

    [Fact]
    public void lookups_page_through_large_tables_up_to_the_scan_cap()
    {
        var s = new DbStateStore(_db, Clock, maxScanRows: 400);
        for (var i = 0; i < 250; i++)
        {
            _db.Insert([new DbColumnValue("k", DbValue.Of("filler" + i)), new DbColumnValue("v", DbValue.Of(new byte[] { 0 })), new DbColumnValue("exp", DbValue.Of(0L))]);
        }

        s.Set("target", [9], 0);
        Assert.Equal(new byte[] { 9 }, s.Get("target"));

        var capped = new DbStateStore(_db, Clock, maxScanRows: 200);
        Assert.Null(capped.Get("target"));
        Assert.Equal(200, capped.MaxScanRows);
    }

    [Fact]
    public void a_denying_db_surfaces_loudly()
    {
        var s = new DbStateStore(new DenyingDbClient(), Clock);
        Assert.Throws<WaddleDbException>(() => s.Get("k"));
    }

    private sealed class RacingDb(FakeDbClient inner) : IDbClient
    {
        public int ConflictsRemaining { get; set; }

        public DbRow Insert(IReadOnlyList<DbColumnValue> c) => inner.Insert(c);
        public DbRow Get(string rowId) => inner.Get(rowId);
        public IReadOnlyList<DbRow> Query(uint limit, uint offset, DbOrderBy? orderBy = null) => inner.Query(limit, offset, orderBy);

        public DbRow Update(string rowId, ulong expectedVersion, IReadOnlyList<DbColumnValue> c)
        {
            Race(rowId);
            return inner.Update(rowId, expectedVersion, c);
        }

        public void Delete(string rowId, ulong expectedVersion)
        {
            Race(rowId);
            inner.Delete(rowId, expectedVersion);
        }

        private void Race(string rowId)
        {
            if (ConflictsRemaining > 0)
            {
                ConflictsRemaining--;
                throw new WaddleDbException(DbErrorKind.Conflict, "version moved on " + rowId);
            }
        }
    }

    private sealed class InsertRaceDb(FakeDbClient inner) : IDbClient
    {
        private bool _raced;

        public DbRow Insert(IReadOnlyList<DbColumnValue> c)
        {
            if (!_raced)
            {
                _raced = true;
                inner.Insert(c);
            }

            return inner.Insert(c);
        }

        public DbRow Get(string rowId) => inner.Get(rowId);
        public IReadOnlyList<DbRow> Query(uint limit, uint offset, DbOrderBy? orderBy = null) => inner.Query(limit, offset, orderBy);
        public DbRow Update(string rowId, ulong v, IReadOnlyList<DbColumnValue> c) => inner.Update(rowId, v, c);
        public void Delete(string rowId, ulong v) => inner.Delete(rowId, v);
    }
}

public class IdempotentOutcomeTests
{
    private static WaddleSdk.Chat.ChatReplyPayload Payload(string text) => new(text, "c");

    private static IdempotentOutcome Make() => new(new KvStateStore(new FakeKvClient()), "game");

    [Theory]
    [InlineData("")]
    [InlineData("  ")]
    public void a_blank_message_id_is_refused(string id)
    {
        Assert.Throws<ArgumentException>(() => Make().KeyFor(id, "twitch", "a", "roll"));
    }

    [Fact]
    public void keys_are_bound_to_every_part_and_length_prefixed_against_concatenation_aliasing()
    {
        var o = Make();
        var baseKey = o.KeyFor("m", "twitch", "a", "roll");
        Assert.NotEqual(baseKey, o.KeyFor("m2", "twitch", "a", "roll"));
        Assert.NotEqual(baseKey, o.KeyFor("m", "discord", "a", "roll"));
        Assert.NotEqual(baseKey, o.KeyFor("m", "twitch", "b", "roll"));
        Assert.NotEqual(baseKey, o.KeyFor("m", "twitch", "a", "dice"));
        Assert.NotEqual(o.KeyFor("ab", "c", "d", "e"), o.KeyFor("a", "bc", "d", "e"));
        Assert.DoesNotContain(':', baseKey);
    }

    [Fact]
    public void record_replay_and_discard_round_trip_only_for_the_exact_delivery()
    {
        var o = Make();
        var info = WaddleSdk.Json.WaddleSdkJsonContext.Default.ChatReplyPayload;
        o.Record("m", "twitch", "a", "roll", Payload("x"), info);

        Assert.Equal("x", o.TryReplay("m", "twitch", "a", "roll", info, _ => true)!.Text);
        Assert.Null(o.TryReplay("m", "twitch", "b", "roll", info, _ => true));
        Assert.Null(o.TryReplay("m", "twitch", "a", "roll", info, _ => false));

        o.Discard("m", "twitch", "a", "roll");
        Assert.Null(o.TryReplay("m", "twitch", "a", "roll", info, _ => true));
    }
}

public class StateCooldownTests
{
    [Fact]
    public void acquire_release_and_independence()
    {
        var s = new KvStateStore(new FakeKvClient());
        Assert.True(StateCooldown.TryAcquire(s, "roll", "actor-1", 180));
        Assert.False(StateCooldown.TryAcquire(s, "roll", "actor-1", 180));
        Assert.True(StateCooldown.TryAcquire(s, "dice", "actor-1", 180));
        Assert.True(StateCooldown.TryAcquire(s, "roll", "actor-2", 180));
        StateCooldown.Release(s, "roll", "actor-1");
        Assert.True(StateCooldown.TryAcquire(s, "roll", "actor-1", 180));
    }

    [Fact]
    public void keys_never_contain_the_raw_actor_id_or_a_colon()
    {
        var kv = new FakeKvClient();
        StateCooldown.TryAcquire(new KvStateStore(kv), "roll", "twitch:99887766", 180);
        var key = kv.SetCalls.Count > 0 ? kv.SetCalls[0].Key : "";
        Assert.DoesNotContain(':', key);
        Assert.DoesNotContain("99887766", key);
    }

    [Fact]
    public void a_denying_store_fails_loud_unlike_the_legacy_guard()
    {
        Assert.Throws<WaddleKvException>(() => StateCooldown.TryAcquire(new KvStateStore(new DenyingKvClient()), "roll", "a", 1));
    }
}

public class TickSchedulerTests
{
    private readonly MutableClock _clock = new(1_000_000);

    private TickScheduler Make(IStateStore store, string board = "heist") => new(store, _clock, board);

    public static TheoryData<string> Stores => new() { "kv", "db" };

    private IStateStore Store(string kind) => kind == "kv" ? new KvStateStore(new FakeKvClient()) : new DbStateStore(new FakeDbClient(), _clock);

    [Theory]
    [MemberData(nameof(Stores))]
    public void nothing_is_due_before_the_deadline_and_the_task_fires_once_after(string kind)
    {
        var sched = Make(Store(kind));
        sched.Schedule(new ScheduledTask("t1", 1_060_000, "settle", "{}"));
        Assert.Empty(sched.TakeDue());
        Assert.Single(sched.Pending());

        _clock.Now = 1_060_001;
        var due = sched.TakeDue();
        Assert.Equal("t1", Assert.Single(due).Id);
        Assert.Empty(sched.TakeDue());
        Assert.Empty(sched.Pending());
    }

    [Theory]
    [MemberData(nameof(Stores))]
    public void only_due_tasks_are_taken_and_the_rest_stay_pending(string kind)
    {
        var sched = Make(Store(kind));
        sched.Schedule(new ScheduledTask("early", 1_000_100, "a", "{}"));
        sched.Schedule(new ScheduledTask("late", 2_000_000, "b", "{}"));
        _clock.Now = 1_500_000;
        Assert.Equal("early", Assert.Single(sched.TakeDue()).Id);
        Assert.Equal("late", Assert.Single(sched.Pending()).Id);
    }

    [Theory]
    [MemberData(nameof(Stores))]
    public void scheduling_the_same_id_replaces_it_and_cancel_removes_it(string kind)
    {
        var sched = Make(Store(kind));
        sched.Schedule(new ScheduledTask("t", 1_100_000, "a", "{\"v\":1}"));
        sched.Schedule(new ScheduledTask("t", 1_200_000, "a", "{\"v\":2}"));
        var only = Assert.Single(sched.Pending());
        Assert.Equal(1_200_000UL, only.DueAtMillis);
        Assert.True(sched.Cancel("t"));
        Assert.False(sched.Cancel("t"));
        Assert.Empty(sched.Pending());
    }

    [Fact]
    public void a_full_board_fails_loud_instead_of_dropping_a_task()
    {
        var sched = Make(new KvStateStore(new FakeKvClient()));
        for (var i = 0; i < TickScheduler.MaxTasks; i++)
        {
            sched.Schedule(new ScheduledTask("t" + i, 5_000_000, "k", "{}"));
        }

        Assert.Throws<InvalidOperationException>(() => sched.Schedule(new ScheduledTask("overflow", 5_000_000, "k", "{}")));
        Assert.Equal(TickScheduler.MaxTasks, sched.Pending().Count);
    }

    [Fact]
    public void two_racing_pickups_of_the_same_due_task_fire_it_exactly_once()
    {
        // Two events read the same board before either writes it back (kv Mutate is not a CAS): the per-task claim arbitrates.
        var kv = new FakeKvClient();
        var a = Make(new KvStateStore(kv));
        var b = Make(new KvStateStore(kv));
        a.Schedule(new ScheduledTask("t", 1_000_500, "settle", "{}"));
        _clock.Now = 1_001_000;
        var total = a.TakeDue().Count + b.TakeDue().Count;
        Assert.Equal(1, total);
    }

    [Fact]
    public void late_by_reports_how_stale_a_pickup_is()
    {
        var sched = Make(new KvStateStore(new FakeKvClient()));
        var task = new ScheduledTask("t", 1_000_500, "settle", "{}");
        _clock.Now = 1_000_000;
        Assert.Equal(0, sched.LateByMillis(task));
        _clock.Now = 1_090_500;
        Assert.Equal(90_000, sched.LateByMillis(task));
    }

    [Fact]
    public void a_corrupt_board_reads_as_empty_rather_than_crashing_the_bundle()
    {
        var kv = new FakeKvClient();
        kv.Set("sched.heist", "garbage"u8.ToArray(), 0);
        Assert.Empty(Make(new KvStateStore(kv)).Pending());
    }

    [Fact]
    public void a_store_that_stays_contended_fails_loud_on_schedule()
    {
        var sched = Make(new NeverAppliesStore());
        Assert.Throws<InvalidOperationException>(() => sched.Schedule(new ScheduledTask("t", 1, "k", "{}")));
        Assert.Empty(sched.TakeDue());
    }

    private sealed class NeverAppliesStore : IStateStore
    {
        public bool AtomicMutate => true;
        public byte[]? Get(string key) => null;
        public void Set(string key, byte[] value, uint ttlSeconds) { }
        public void Delete(string key) { }
        public bool TryClaim(string key, uint ttlSeconds) => true;
        public bool Mutate(string key, Func<byte[]?, byte[]?> mutate, uint ttlSeconds, int maxAttempts = 5) => false;
    }
}
