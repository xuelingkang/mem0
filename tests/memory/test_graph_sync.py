"""Tests for graph memory (design: docs/design/graph-memory.md).

Two concerns are covered here:
  * The scope -> graph-key derivation is a pure function shared by the write dispatch
    and the retrieval query (design §4.4).
  * The dispatch path is a bounded in-memory queue whose failure modes (queue overflow,
    unreachable bridge, repeated failures) never raise into the write path and are
    observable through counters (design §5.1-5.2, §8).
"""

import queue as queue_module
import time
from types import SimpleNamespace

import httpx
import pytest

from mem0.configs.base import GraphConfig, MemoryConfig
from mem0.memory.graph_sync import (
    GraphSync,
    compute_graph_boosts,
    derive_group_id,
    sanitize_group_id,
    search_graph_facts,
    search_graph_facts_with_status,
)


class _CountingClient:
    """Stand-in for `GraphBridgeClient` that records calls and can be told to fail."""

    def __init__(self, statuses=None, failures=0):
        self.calls = []
        self._statuses = list(statuses or [])
        self._failures = failures
        self.closed = False

    def post_episode(self, payload):
        self.calls.append(payload)
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("bridge down")
        status = self._statuses.pop(0) if self._statuses else "synced"
        return {"status": status, "uuid": payload["uuid"]}

    def search(self, payload, timeout_seconds):
        self.calls.append(payload)
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("bridge down")
        return {"facts": []}

    def close(self):
        self.closed = True


class _TimeoutClient:
    """Stand-in that always exceeds the per-call budget."""

    def search(self, _payload, _timeout_seconds):
        raise httpx.ReadTimeout("budget exceeded")


def _memory(**graph_overrides):
    config = MemoryConfig(graph=GraphConfig(**graph_overrides))
    return SimpleNamespace(config=config, _graph_bridge_client=_CountingClient())


class TestGroupIdDerivation:
    """[AC-32] 图键由作用域派生，写入与检索共用同一纯函数。"""

    def test_user_id_wins_over_agent_and_run(self):
        filters = {"user_id": "test_graph_a", "agent_id": "test_graph_b", "run_id": "test_graph_c"}
        assert derive_group_id(filters) == "mem0_test_graph_a"

    def test_agent_then_run_fallback(self):
        assert derive_group_id({"agent_id": "test_graph_a"}) == "mem0_test_graph_a"
        assert derive_group_id({"run_id": "test_graph_a"}) == "mem0_test_graph_a"

    def test_nested_and_wrapper_is_unwrapped(self):
        """检索侧 filters 常被 `{\"AND\": [...]}` 包裹，作用域必须仍能取到。"""
        filters = {"AND": [{"user_id": "test_graph_a"}, {"invalid_at": None}]}
        assert derive_group_id(filters) == "mem0_test_graph_a"

    def test_no_scope_returns_none(self):
        assert derive_group_id(None) is None
        assert derive_group_id({}) is None
        assert derive_group_id({"metadata": {"x": 1}}) is None

    def test_invalid_characters_are_normalized(self):
        """graphiti 的 `validate_group_id` 只接受 [A-Za-z0-9_-]，冒号等必须归一。"""
        group_id = derive_group_id({"user_id": "test graph:1/2"})
        assert group_id == "mem0_test_graph_1_2"
        assert sanitize_group_id("a" * 200) == "a" * 96

    def test_same_scope_yields_same_key(self):
        assert derive_group_id({"user_id": "test_graph_a"}) == derive_group_id({"user_id": "test_graph_a"})


class TestGraphBoostFolding:
    """[AC-11][AC-12][AC-16][AC-17][AC-34] 图加分折算为纯函数。"""

    def test_rank_decay_and_ceiling(self):
        facts = [{"episodes": ["a"]}, {"episodes": ["b"]}]
        boosts, counts = compute_graph_boosts(facts, ["a", "b"], weight=0.5)
        assert boosts["a"] == pytest.approx(0.5)
        assert boosts["b"] == pytest.approx(0.5 / 1.5)
        assert counts == {"a": 1, "b": 1}

    def test_max_over_ranks_and_fact_count(self):
        facts = [
            {"episodes": ["b"]},
            {"episodes": ["b", "a"]},
        ]
        boosts, counts = compute_graph_boosts(facts, ["a", "b", "c"], weight=0.5)
        # b 在第 1 条命中（权重 1.0）与第 2 条命中（权重 1/1.5），取最大。
        assert boosts["b"] == pytest.approx(0.5)
        assert boosts["a"] == pytest.approx(0.5 / 1.5)
        assert counts == {"b": 2, "a": 1}

    def test_ids_outside_candidate_pool_are_ignored(self):
        facts = [{"episodes": ["outside"]}]
        boosts, counts = compute_graph_boosts(facts, ["a"], weight=0.5)
        assert boosts == {}
        assert counts == {}

    def test_invalidated_facts_are_skipped_by_default(self):
        facts = [{"episodes": ["a"], "invalid_at": "2026-01-01T00:00:00+00:00"}]
        boosts, _ = compute_graph_boosts(facts, ["a"], weight=0.5)
        assert boosts == {}
        boosts, _ = compute_graph_boosts(facts, ["a"], weight=0.5, include_invalidated=True)
        assert boosts["a"] == pytest.approx(0.5)

    def test_malformed_facts_are_ignored(self):
        facts = ["not-a-dict", {"episodes": None}, {"episodes": "a"}, {}]
        boosts, counts = compute_graph_boosts(facts, ["a"], weight=0.5)
        assert boosts == {} and counts == {}

    def test_zero_weight_disables_boosts(self):
        boosts, _ = compute_graph_boosts([{"episodes": ["a"]}], ["a"], weight=0.0)
        assert boosts == {}

    def test_pure_function_is_repeatable(self):
        facts = [{"episodes": ["a"]}, {"episodes": ["b"]}]
        assert compute_graph_boosts(facts, ["a", "b"], 0.5) == compute_graph_boosts(facts, ["a", "b"], 0.5)


class TestSearchDegradation:
    """[AC-20]-[AC-23] 图检索失败一律按「本次无图信号」处理。"""

    def test_unreachable_bridge_returns_empty(self):
        client = _CountingClient(failures=1)
        assert search_graph_facts(client, "mem0_test_graph_a", "q", 5, 0.4) == []

    def test_malformed_response_returns_empty(self):
        client = SimpleNamespace(search=lambda payload, timeout: {"facts": "nope"})
        assert search_graph_facts(client, "mem0_test_graph_a", "q", 5, 0.4) == []

    def test_no_group_or_query_skips_the_call(self):
        client = _CountingClient()
        assert search_graph_facts(client, None, "q", 5, 0.4) == []
        assert search_graph_facts(client, "mem0_test_graph_a", "", 5, 0.4) == []
        assert client.calls == []

    def test_status_is_ok_when_the_bridge_answers(self):
        """答了但没命中仍是 `ok`：与「没答上来」必须区分（设计 §8）。"""
        facts, status = search_graph_facts_with_status(_CountingClient(), "mem0_test_graph_a", "q", 5, 0.4)
        assert facts == [] and status == "ok"

    def test_status_marks_a_budget_timeout(self):
        """[AC-3] 预算内超时是显式状态，不再与「无命中」同形。"""
        facts, status = search_graph_facts_with_status(_TimeoutClient(), "mem0_test_graph_a", "q", 5, 0.4)
        assert facts == [] and status == "timeout"

    def test_status_marks_a_bridge_failure_and_a_malformed_payload(self):
        facts, status = search_graph_facts_with_status(_CountingClient(failures=1), "mem0_test_graph_a", "q", 5, 0.4)
        assert facts == [] and status == "error"

        client = SimpleNamespace(search=lambda payload, timeout: {"facts": "nope"})
        facts, status = search_graph_facts_with_status(client, "mem0_test_graph_a", "q", 5, 0.4)
        assert facts == [] and status == "error"

    def test_status_marks_a_skipped_lookup(self):
        client = _CountingClient()
        facts, status = search_graph_facts_with_status(client, None, "q", 5, 0.4)
        assert facts == [] and status == "skipped"
        assert client.calls == []


class TestDispatchQueue:
    """[AC-6][AC-9][AC-25] 派发不阻塞写入路径，队列有界且失败可观测。"""

    def test_disabled_config_never_dispatches(self):
        sync = GraphSync(GraphConfig(enabled=False), client=_CountingClient())
        assert sync.dispatch("m1", "text", None, "mem0_test_graph_a") is False
        assert sync.stats()["graph_dispatched"] == 0

    def test_dispatch_without_group_id_is_skipped(self):
        sync = GraphSync(GraphConfig(enabled=True), client=_CountingClient())
        assert sync.dispatch("m1", "text", None, None) is False

    def test_dispatch_enqueues_and_counts(self):
        client = _CountingClient()
        sync = GraphSync(GraphConfig(enabled=True, retry_backoff_seconds=0.0), client=client)
        try:
            assert sync.dispatch("m1", "text", "2026-09-17T00:00:00+00:00", "mem0_test_graph_a") is True
            assert sync.stats()["graph_dispatched"] == 1
        finally:
            sync.close()

    def test_queue_overflow_drops_oldest_and_counts(self, monkeypatch):
        client = _CountingClient()
        sync = GraphSync(GraphConfig(enabled=True, queue_size=2), client=client)
        # worker 不启动：只验证入队路径的有界性。`dispatch()` 末行会惰性启动 worker 并
        # 排空队列，「投递与排空的交错」取决于线程调度（同一命令连跑 10 次曾 5 次得到
        # dropped=2），因此必须把 `start()` 显式置为空操作，断言才只覆盖入队路径。
        monkeypatch.setattr(sync, "start", lambda: None)
        for index in range(5):
            sync.dispatch(f"m{index}", "text", None, "mem0_test_graph_a")
        stats = sync.stats()
        assert stats["graph_dispatched"] == 5
        assert stats["graph_dropped"] == 3
        assert stats["queue_size"] == 2
        sync.close()

    def test_worker_syncs_and_counts(self):
        client = _CountingClient()
        sync = GraphSync(GraphConfig(enabled=True, retry_backoff_seconds=0.0), client=client)
        try:
            sync.dispatch("m1", "text", None, "mem0_test_graph_a")
            for _ in range(50):
                if sync.stats()["graph_synced"]:
                    break
                time.sleep(0.05)
            assert sync.stats()["graph_synced"] == 1
            assert client.calls[0]["uuid"] == "m1"
            assert client.calls[0]["group_id"] == "mem0_test_graph_a"
        finally:
            sync.close()

    def test_already_synced_is_counted_separately(self):
        client = _CountingClient(statuses=["already_synced"])
        sync = GraphSync(GraphConfig(enabled=True, retry_backoff_seconds=0.0), client=client)
        try:
            sync.dispatch("m1", "text", None, "mem0_test_graph_a")
            for _ in range(50):
                if sync.stats()["graph_already_synced"]:
                    break
                time.sleep(0.05)
            assert sync.stats()["graph_already_synced"] == 1
            assert sync.stats()["graph_synced"] == 0
        finally:
            sync.close()

    def test_retry_then_failure_counts_and_opens_circuit(self):
        client = _CountingClient(failures=99)
        sync = GraphSync(
            GraphConfig(
                enabled=True,
                max_retries=2,
                retry_backoff_seconds=0.0,
                circuit_breaker_failures=1,
                circuit_cooldown_seconds=60.0,
            ),
            client=client,
        )
        try:
            import asyncio

            asyncio.run(sync._sync_one({"uuid": "m1", "group_id": "mem0_test_graph_a", "text": "t"}))
            stats = sync.stats()
            # 2 次重试 + 首次尝试 = 3 次调用；计数为 1 条失败，且熔断已打开。
            assert len(client.calls) == 3
            assert stats["graph_failed"] == 1
            assert stats["circuit_open"] is True
        finally:
            sync.close()

    def test_circuit_open_drops_without_calling(self):
        import asyncio

        client = _CountingClient()
        sync = GraphSync(
            GraphConfig(enabled=True, circuit_breaker_failures=1, circuit_cooldown_seconds=60.0),
            client=client,
        )
        try:
            sync._consecutive_failures = 5
            sync._circuit_open_until = time.monotonic() + 30
            asyncio.run(sync._sync_one({"uuid": "m1", "group_id": "mem0_test_graph_a", "text": "t"}))
            assert client.calls == []
            assert sync.stats()["graph_dropped"] == 1
        finally:
            sync.close()

    def test_dispatch_swallows_queue_errors(self):
        """派发路径的任何异常都不得外溢到写入结果。"""

        class _BrokenQueue:
            def put_nowait(self, _task):
                raise queue_module.Full()

            def get_nowait(self):
                raise queue_module.Empty()

        sync = GraphSync(GraphConfig(enabled=True), client=_CountingClient())
        sync._queue = _BrokenQueue()  # type: ignore[assignment] - 注入一个恒定溢出/为空的队列
        assert sync.dispatch("m1", "text", None, "mem0_test_graph_a") is False
