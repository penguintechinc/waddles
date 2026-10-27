"""Tests for flask_core.sharding -- consistent hash ring + distributed channel ownership.

The ring is exercised with real hashing (determinism, balance, minimal movement on
scale events). Ownership locks run against a stateful in-memory SET NX/GET/EXPIRE/DEL
double so the claim/renew/release *decision logic* is real.
"""

import collections

import pytest

from flask_core.sharding import ChannelShardManager, ConsistentHashRing


class LockRedis:
    """Minimal Redis double honouring SET ... NX semantics."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex:
            self.ttl[key] = ex
        return True

    async def get(self, key):
        self._check()
        return self.store.get(key)

    async def expire(self, key, seconds):
        self._check()
        self.ttl[key] = seconds

    async def delete(self, key):
        self._check()
        self.store.pop(key, None)


class FakeDal:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.queries: list[tuple] = []

    def executesql(self, sql, params):
        self.queries.append((sql, params))
        if self.error:
            raise self.error
        return self.rows


class TestHashRing:
    def test_empty_ring(self):
        ring = ConsistentHashRing()
        assert ring.get_node("twitch:1") is None
        assert ring.get_all_nodes() == []
        assert ring.get_channel_distribution(["a"]) == {}

    def test_single_node_owns_everything(self):
        ring = ConsistentHashRing(virtual_nodes=10)
        ring.add_node("pod-0")
        assert {ring.get_node(f"c{i}") for i in range(50)} == {"pod-0"}
        assert len(ring.ring) == 10

    def test_assignment_is_deterministic_across_instances(self):
        a, b = ConsistentHashRing(), ConsistentHashRing()
        for n in ("pod-0", "pod-1", "pod-2"):
            a.add_node(n)
        for n in ("pod-2", "pod-0", "pod-1"):  # insertion order must not matter
            b.add_node(n)
        for i in range(200):
            assert a.get_node(f"twitch:{i}") == b.get_node(f"twitch:{i}")

    def test_duplicate_add_is_ignored(self):
        ring = ConsistentHashRing(virtual_nodes=5)
        ring.add_node("pod-0")
        before = dict(ring.ring)
        ring.add_node("pod-0")
        assert ring.ring == before and ring.get_all_nodes() == ["pod-0"]

    def test_remove_unknown_node_is_noop(self):
        ring = ConsistentHashRing(virtual_nodes=5)
        ring.add_node("pod-0")
        ring.remove_node("ghost")
        assert ring.get_all_nodes() == ["pod-0"]

    def test_remove_node_clears_its_virtual_nodes(self):
        ring = ConsistentHashRing(virtual_nodes=5)
        ring.add_node("pod-0")
        ring.add_node("pod-1")
        ring.remove_node("pod-0")
        assert set(ring.ring.values()) == {"pod-1"}
        assert ring.sorted_keys == sorted(ring.ring)
        assert ring.get_node("anything") == "pod-1"

    def test_wraps_around_past_last_key(self):
        ring = ConsistentHashRing(virtual_nodes=3)
        ring.add_node("pod-0")
        ring.add_node("pod-1")
        top = ring.sorted_keys[-1]
        # force a hash beyond the largest ring key -> must wrap to the first node
        ring._hash = lambda key: top + 1
        assert ring.get_node("whatever") == ring.ring[ring.sorted_keys[0]]

    def test_distribution_is_reasonably_balanced(self):
        ring = ConsistentHashRing()
        for i in range(4):
            ring.add_node(f"pod-{i}")
        counts = collections.Counter(ring.get_node(f"twitch:{i}") for i in range(4000))
        assert set(counts) == {f"pod-{i}" for i in range(4)}
        assert min(counts.values()) > 4000 / 4 * 0.5  # no pod starved
        assert max(counts.values()) < 4000 / 4 * 1.5  # no pod hot-spotted

    def test_adding_node_moves_only_a_minority_of_channels(self):
        ring = ConsistentHashRing()
        for i in range(4):
            ring.add_node(f"pod-{i}")
        keys = [f"twitch:{i}" for i in range(2000)]
        before = {k: ring.get_node(k) for k in keys}
        ring.add_node("pod-4")
        moved = [k for k in keys if ring.get_node(k) != before[k]]
        assert 0 < len(moved) < len(keys) * 0.4  # ~1/5 expected
        assert all(ring.get_node(k) == "pod-4" for k in moved)  # only ever move TO the new node

    def test_distribution_report(self):
        ring = ConsistentHashRing(virtual_nodes=20)
        ring.add_node("pod-0")
        ring.add_node("pod-1")
        channels = [f"c{i}" for i in range(100)]
        dist = ring.get_channel_distribution(channels)
        assert set(dist) == {"pod-0", "pod-1"}
        assert sorted(sum(dist.values(), [])) == sorted(channels)


class TestShardManager:
    def _mgr(self, pod="pod-0", pods=3, dal=None, redis=None):
        return ChannelShardManager(dal or FakeDal(), redis or LockRedis(), pod, pods)

    def test_ring_seeded_with_all_pods_and_stats(self):
        m = self._mgr(pods=3)
        assert m.hash_ring.get_all_nodes() == ["pod-0", "pod-1", "pod-2"]
        assert m.get_shard_statistics() == {
            "pod_id": "pod-0", "total_pods": 3, "nodes_in_ring": 3, "virtual_nodes_per_physical": 150,
        }

    async def test_get_my_channels_filters_by_ring_assignment(self):
        rows = [(i, f"srv{i}", f"Server {i}", 100 + i) for i in range(60)]
        dal = FakeDal(rows=rows)
        owned = {p: await self._mgr(pod=p, pods=3, dal=dal).get_my_channels("twitch") for p in ("pod-0", "pod-1", "pod-2")}
        # partition: every channel owned by exactly one pod
        all_ids = sorted(c["id"] for chans in owned.values() for c in chans)
        assert all_ids == list(range(60))
        assert all(len(chans) > 0 for chans in owned.values())
        first = owned["pod-0"][0]
        assert set(first) == {"id", "server_id", "server_name", "community_id"}
        assert dal.queries[0][1] == ["twitch"]
        assert "status = 'approved'" in dal.queries[0][0]

    async def test_get_my_channels_db_error_returns_empty(self):
        m = self._mgr(dal=FakeDal(error=RuntimeError("db down")))
        assert await m.get_my_channels("twitch") == []

    async def test_claim_is_exclusive(self):
        redis = LockRedis()
        a = self._mgr("pod-0", redis=redis)
        b = self._mgr("pod-1", redis=redis)
        assert await a.claim_channel_ownership("twitch:1", ttl_seconds=30) is True
        assert redis.ttl["waddlebot:shard:lock:twitch:1"] == 30
        assert await b.claim_channel_ownership("twitch:1") is False
        assert await b.claim_channel_ownership("twitch:2") is True

    async def test_renew_only_by_owner(self):
        redis = LockRedis()
        a = self._mgr("pod-0", redis=redis)
        b = self._mgr("pod-1", redis=redis)
        await a.claim_channel_ownership("c")
        assert await b.renew_channel_ownership("c", ttl_seconds=99) is False
        assert await a.renew_channel_ownership("c", ttl_seconds=99) is True
        assert redis.ttl["waddlebot:shard:lock:c"] == 99
        assert await a.renew_channel_ownership("never-claimed") is False

    async def test_release_only_by_owner(self):
        redis = LockRedis()
        a = self._mgr("pod-0", redis=redis)
        b = self._mgr("pod-1", redis=redis)
        await a.claim_channel_ownership("c")
        assert await b.release_channel_ownership("c") is False
        assert "waddlebot:shard:lock:c" in redis.store
        assert await a.release_channel_ownership("c") is True
        assert "waddlebot:shard:lock:c" not in redis.store
        assert await b.claim_channel_ownership("c") is True

    async def test_redis_errors_are_contained(self):
        redis = LockRedis()
        redis.fail = True
        m = self._mgr(redis=redis)
        assert await m.claim_channel_ownership("c") is False
        assert await m.renew_channel_ownership("c") is False
        assert await m.release_channel_ownership("c") is False

    async def test_rebalance_releases_channels_no_longer_ours(self):
        """A pod that owns nothing after the ring changes releases nothing (locks left intact)."""
        rows = [(i, f"srv{i}", f"S{i}", 1) for i in range(40)]
        redis = LockRedis()
        m = self._mgr("pod-0", pods=3, dal=FakeDal(rows=rows), redis=redis)
        mine_before = await m.get_my_channels("twitch")
        for ch in mine_before:
            await m.claim_channel_ownership(f"twitch:{ch['server_id']}")
        assert mine_before and len(redis.store) == len(mine_before)

        # Re-point the ring so pod-0 no longer owns anything it used to.
        m.hash_ring.remove_node("pod-0")
        # get_my_channels now yields nothing for pod-0, so rebalance has nothing to release...
        await m.rebalance_on_scale_event()
        assert len(redis.store) == len(mine_before)

    async def test_rebalance_releases_when_assignment_flips_mid_run(self, monkeypatch):
        redis = LockRedis()
        m = self._mgr("pod-0", pods=1, redis=redis)
        chans = [{"id": 1, "server_id": "a", "server_name": "A", "community_id": 1},
                 {"id": 2, "server_id": "b", "server_name": "B", "community_id": 1}]
        for c in chans:
            await m.claim_channel_ownership(f"twitch:{c['server_id']}")

        async def my_channels(platform):
            return chans

        monkeypatch.setattr(m, "get_my_channels", my_channels)
        m.hash_ring.remove_node("pod-0")
        m.hash_ring.add_node("pod-9")  # ring now assigns everything to someone else
        await m.rebalance_on_scale_event()
        assert redis.store == {}

    async def test_rebalance_swallows_errors(self, monkeypatch):
        m = self._mgr()

        async def boom(platform):
            raise RuntimeError("no db")

        monkeypatch.setattr(m, "get_my_channels", boom)
        await m.rebalance_on_scale_event()  # must not raise
